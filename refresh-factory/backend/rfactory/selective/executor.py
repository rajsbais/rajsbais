"""Checkpointed, restartable, idempotent selective-load executor (runs against SIMULATED adapters in the MVP).

Steps: PREFLIGHT -> EXTRACT_MASK_STAGE -> LOAD (checkpoint per business object) -> NUMBER_RANGES.
Masking is applied in-flight: only masked rows are staged or written to the target.
Every write records the prior image so the run can be rolled back.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..dependency.planner import Plan
from ..dependency.registry import Registry
from ..masking.engine import MaskingEngine
from ..sap.adapter import TargetAdapter, TransientError
from ..sap.ddic import TABLES
from ..security.audit import AuditLog
from .conflicts import Action, ConflictReport


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_hash(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass
class Run:
    id: str
    project_id: str
    manifest_hash: str
    status: str = "PENDING"  # PENDING | RUNNING | FAILED | COMPLETED | ROLLED_BACK
    steps: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    checkpoint: int = 0
    order: list[str] = field(default_factory=list)
    staged: dict[str, dict[str, list[dict]]] = field(default_factory=dict)
    expected: dict[str, str] = field(default_factory=dict)  # "TABLE/key" -> hash of masked row
    undo: list[tuple[str, tuple, dict | None]] = field(default_factory=list)
    undo_seen: set = field(default_factory=set)
    level_undo: dict[str, int] = field(default_factory=dict)
    loaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    staging_hashes: dict[str, str] = field(default_factory=dict)
    attempts: int = 0
    error: str | None = None
    started: str | None = None
    finished: str | None = None
    reconciliation: dict | None = None
    release: str = "NOT_EVALUATED"  # RELEASED | HELD | NOT_EVALUATED

    def log(self, level: str, msg: str, **kw):
        self.events.append({"ts": now(), "level": level, "msg": msg, **kw})

    def step(self, name: str) -> dict:
        for s in self.steps:
            if s["name"] == name:
                return s
        s = {"name": name, "status": "PENDING", "started": None, "finished": None, "detail": {}}
        self.steps.append(s)
        return s

    def public(self) -> dict:
        return {"id": self.id, "project_id": self.project_id, "manifest_hash": self.manifest_hash, "status": self.status,
                "steps": self.steps, "checkpoint": self.checkpoint, "total_objects": len(self.order),
                "loaded": len(self.loaded), "skipped": len(self.skipped), "quarantined": len(self.quarantined),
                "attempts": self.attempts, "error": self.error, "started": self.started, "finished": self.finished,
                "release": self.release, "events": self.events[-200:], "simulated": True}


class Executor:
    def __init__(self, registry: Registry, staging_root: Path, audit: AuditLog, retries: int = 3, backoff: float = 0.0):
        self.reg, self.root, self.audit, self.retries, self.backoff = registry, staging_root, audit, retries, backoff

    # ------------------------------------------------------------------
    def new_run(self, project_id: str, plan: Plan, report: ConflictReport) -> Run:
        run = Run(f"run-{uuid.uuid4().hex[:8]}", project_id, plan.manifest_hash)
        run.order = [i for i in plan.order if report.decisions.get(i) in (Action.LOAD, Action.UPDATE, Action.REPLACE)]
        run.skipped = [i for i, a in report.decisions.items() if a == Action.SKIP]
        run.quarantined = [i for i, a in report.decisions.items() if a == Action.QUARANTINE]
        return run

    def execute(self, run: Run, plan: Plan, report: ConflictReport, masking: MaskingEngine,
                target: TargetAdapter, actor: str) -> Run:
        """Execute or resume. Safe to call again after a failure: continues from `run.checkpoint`."""
        run.status, run.started = "RUNNING", run.started or now()
        run.attempts += 1
        run.error = None
        try:
            self._preflight(run, plan, report, target)
            if run.step("EXTRACT_MASK_STAGE")["status"] != "DONE":
                self._stage(run, plan, report, masking)
            self._load(run, plan, report, target)
            self._number_ranges(run, report, target)
            run.status, run.finished = "COMPLETED", now()
            run.log("info", "run completed")
            self.audit.append(actor, "run.completed", run.id, {"loaded": len(run.loaded), "skipped": len(run.skipped),
                                                               "quarantined": len(run.quarantined)})
        except Exception as e:  # permanent failure: keep checkpoint so the run can be resumed or rolled back
            for st in run.steps:
                if st["status"] == "RUNNING":
                    st["status"] = "FAILED"
            run.status, run.error = "FAILED", f"{type(e).__name__}: {e}"
            run.log("error", run.error)
            self.audit.append(actor, "run.failed", run.id, {"error": run.error, "checkpoint": run.checkpoint})
        if run.status == "COMPLETED":
            masking.destroy_ephemeral_key()  # per-run anonymization key is never persisted
        return run

    # ------------------------------------------------------------------
    def _start(self, run, name):
        s = run.step(name)
        s["status"], s["started"] = "RUNNING", s["started"] or now()
        return s

    def _done(self, s, **detail):
        s["status"], s["finished"] = "DONE", now()
        s["detail"].update(detail)

    def _preflight(self, run, plan, report, target):
        s = self._start(run, "PREFLIGHT")
        target.assert_writable()  # raises ProductionWriteBlocked for PRD or locked targets
        if plan.manifest_hash != run.manifest_hash:
            raise RuntimeError("plan no longer matches the approved manifest hash")
        if plan.blocking or report.blocking:
            raise RuntimeError("plan has blocking issues")
        self._done(s, target=target.system.label)

    def _stage(self, run, plan, report, masking):
        s = self._start(run, "EXTRACT_MASK_STAGE")
        d = self.root / run.id
        d.mkdir(parents=True, exist_ok=True)
        files: dict[str, list[str]] = {}
        for iid in run.order:
            inst = plan.instances[iid]
            drop = {t: set(k) for t, k in report.row_exclusions.get(iid, {}).items()}
            masked = {t: [masking.mask_row(t, r) for r in rows if tuple(r[k] for k in TABLES[t].keys) not in drop.get(t, set())]
                      for t, rows in inst.rows.items()}
            run.staged[iid] = masked
            for t, rows in masked.items():
                for r in rows:
                    run.expected[f"{t}/" + "/".join(str(r[k]) for k in TABLES[t].keys)] = row_hash(r)
                    files.setdefault(t, []).append(json.dumps(r, sort_keys=True, default=str))
        for t, lines in files.items():
            p = d / f"{t}.jsonl"
            p.write_text("\n".join(lines) + "\n")
            run.staging_hashes[t] = hashlib.sha256(p.read_bytes()).hexdigest()
        self._done(s, tables=len(files), rows=sum(len(v) for v in files.values()))
        run.log("info", "staged masked data", tables=len(files))

    def _retry(self, fn, run, what):
        for attempt in range(1, self.retries + 1):
            try:
                return fn()
            except TransientError as e:
                run.log("warn", f"transient error during {what} (attempt {attempt}/{self.retries}): {e}")
                if attempt == self.retries:
                    raise
                time.sleep(self.backoff * attempt)

    def _remember(self, run, target, table, row_or_key, is_key=False):
        key = row_or_key if is_key else tuple(row_or_key[k] for k in TABLES[table].keys)
        tag = (table, key)
        if tag in run.undo_seen:
            return
        run.undo_seen.add(tag)
        prior = target.get(table, key)
        run.undo.append((table, key, dict(prior) if prior else None))

    def _target_children(self, target, inst, ot):
        """Existing target rows belonging to the instance (for REPLACE). Shared ADRC rows are never deleted."""
        hdr = inst.rows[ot.header][0]
        t_hdr = target.get(ot.header, tuple(hdr[k] for k in TABLES[ot.header].keys))
        found = {ot.header: [t_hdr] if t_hdr else []}
        for link in ot.tables[1:]:
            if link.table in ("ADRC",):
                continue
            rows = []
            for pr in found.get(link.parent, []):
                (cf, pf), rest = link.join[0], link.join[1:]
                rows += [c for c in target.lookup(link.table, cf, pr.get(pf)) if all(c.get(a) == pr.get(b) for a, b in rest)]
            found.setdefault(link.table, [])
            found[link.table] += rows
        return found

    def _load(self, run, plan, report, target):
        s = self._start(run, "LOAD")
        while run.checkpoint < len(run.order):
            iid = run.order[run.checkpoint]
            inst, action = plan.instances[iid], report.decisions[iid]
            ot = self.reg.types[inst.type]

            def work():
                if action == Action.REPLACE:
                    for t, rows in reversed(list(self._target_children(target, inst, ot).items())):
                        for r in rows:
                            self._remember(run, target, t, r)
                            target.delete(t, tuple(r[k] for k in TABLES[t].keys))
                for t, rows in run.staged[iid].items():
                    for r in rows:
                        self._remember(run, target, t, r)
                    target.upsert(t, rows)

            self._retry(work, run, iid)
            run.loaded.append(iid)
            run.checkpoint += 1
            s["detail"] = {"objects_loaded": run.checkpoint, "of": len(run.order)}
        self._done(s, objects_loaded=len(run.order), of=len(run.order))
        run.log("info", f"loaded {len(run.order)} business objects")

    def _number_ranges(self, run, report, target):
        s = self._start(run, "NUMBER_RANGES")
        for obj, level in report.number_range_adjustments.items():
            run.level_undo.setdefault(obj, target.number_level(obj))
            target.set_number_level(obj, max(level, target.number_level(obj) or 0))
        self._done(s, adjusted=report.number_range_adjustments)

    # ------------------------------------------------------------------
    def rollback(self, run: Run, target: TargetAdapter, actor: str) -> Run:
        target.assert_writable()
        for table, key, prior in reversed(run.undo):
            if prior is None:
                target.delete(table, key)
            else:
                target.upsert(table, [prior])
        for obj, lvl in run.level_undo.items():
            if lvl is not None:
                target.set_number_level(obj, lvl)
        run.status, run.release = "ROLLED_BACK", "HELD"
        run.log("info", "rolled back from undo log", restored=len(run.undo))
        self.audit.append(actor, "run.rolled_back", run.id, {"restored_rows": len(run.undo)})
        return run
