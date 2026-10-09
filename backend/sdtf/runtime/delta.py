"""Delta capture and replay (near-zero-downtime stages 4-10 of docs/07, simulated).

A delta cycle is a MigrationRun of kind DELTA bound to a completed baseline run. It polls the source add-on for
change events after the last watermark (Z_SDTF_CDC_POLL through the RFC transport), filters them against the
baseline's approved manifest, transforms them with the baseline's approved ruleset, applies them to the target in
dependency order with idempotency and conflict detection, keeps the baseline's staging in step so the baseline
reconciliation stays meaningful, and reconciles what it applied. A final cycle (after the business freeze) ends
with the full three-layer reconciliation of the baseline.

Honest scope: the target is the simulated record store; no SAP system is written. Event ordering and
idempotency are the engine's; the source's own semantics are the add-on's (sap-abap/README.md).
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import observability as obs
from ..audit.service import record_event
from ..catalog.business_objects import BUSINESS_OBJECTS, RELATIONSHIPS, instance_status
from ..catalog.store import RecordStore, delete_records, upsert_records
from ..catalog.tables import TABLES
from ..models import (
    DeltaEvent,
    MigrationRun,
    ReconciliationResult,
    RuleSet,
    RunStage,
    SapSystem,
    ScopeManifest,
    TransformationException,
)
from ..reconciliation.service import reconcile_run, summarize
from ..rules.engine import RuleError, SkipRecord, parse_ruleset, target_key, transform_record
from ..staging import StagedRow, get_backend
from .activity import load_change_log
from .extraction import TRANSFER_CLASSES
from .pipeline import RunPrecondition
from .rfc import AbapAddonClient, make_transport, predicate

DELTA_STAGES = ["PRECHECK", "CAPTURE", "TRANSFORM", "APPLY", "RECONCILE", "REPORT"]
_TABLE_BO: dict[str, str] = {}
for _bo in BUSINESS_OBJECTS.values():
    _TABLE_BO.setdefault(_bo.header_table, _bo.id)
    for _t in _bo.item_tables:
        _TABLE_BO.setdefault(_t, _bo.id)


def _now():
    return datetime.now(timezone.utc)


def _lag(changed_at: str) -> int | None:
    """Seconds between a change's timestamp (YYYYMMDDHHMMSS UTC) and now."""
    try:
        return max(0, int(time.time() - datetime.strptime(changed_at, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()))
    except ValueError:
        return None


def _r(run_id, layer, name, status, subject="", src="", tgt="", variance="", explanation="", evidence=None):
    return ReconciliationResult(run_id=run_id, layer=layer, check_name=name, subject=subject, status=status, source_value=str(src), target_value=str(tgt), variance=str(variance), explanation=explanation, evidence=evidence or {})


# ------------------------------------------------------------------------------------------ ordering
def type_order() -> dict[str, int]:
    """Apply order: configuration, then masters, then documents in DOC_FLOW / ACCOUNTING_REF topological order."""
    rank: dict[str, int] = {}
    docs = [b.id for b in BUSINESS_OBJECTS.values() if b.kind == "TRANSACTIONAL"]
    for b in BUSINESS_OBJECTS.values():
        if b.kind == "CONFIG":
            rank[b.id] = 0
        elif b.kind == "MASTER":
            rank[b.id] = 1
    edges = [(r.from_type, r.to_type) for r in RELATIONSHIPS if r.edge_type in ("DOC_FLOW", "ACCOUNTING_REF") and r.from_type in docs and r.to_type in docs and r.from_type != r.to_type]
    indeg = {d: 0 for d in docs}
    out: dict[str, list[str]] = defaultdict(list)
    for a, b in edges:
        indeg[b] += 1
        out[a].append(b)
    level = 2
    frontier = sorted(d for d in docs if indeg[d] == 0)
    seen = set()
    while frontier:
        nxt = []
        for d in frontier:
            if d in seen:
                continue
            seen.add(d)
            rank[d] = level
            for b in out[d]:
                indeg[b] -= 1
                if indeg[b] == 0:
                    nxt.append(b)
        frontier = sorted(nxt)
        level += 1
    for d in docs:  # cycles (none today) fall back to the end
        rank.setdefault(d, level)
    return rank


# ------------------------------------------------------------------------------------- the engine
class DeltaEngine:
    def __init__(self, session: Session, run: MigrationRun):
        self.session = session
        self.run = run
        self.base: MigrationRun = session.get(MigrationRun, run.metrics["baseline_run_id"])
        self.manifest: ScopeManifest = session.get(ScopeManifest, run.manifest_id)
        self.rs = parse_ruleset(session.get(RuleSet, run.ruleset_id).source_yaml)
        self.src: SapSystem = session.get(SapSystem, run.source_system_id)
        self.tgt: SapSystem = session.get(SapSystem, run.target_system_id)
        self.cls = self.manifest.selection.get("classification", {})
        self.scope_ccs = set(self.manifest.definition["company_codes"])
        self.backend = get_backend(self.base.metrics.get("staging_backend"), session=session)
        self._client: AbapAddonClient | None = None
        self._source_store: RecordStore | None = None
        self._header_cache: dict[str, bool] = {}
        self._staged_keys: dict[str, dict[str, str]] = {}

    # -- plumbing
    @property
    def client(self) -> AbapAddonClient:
        if self._client is None:
            t = make_transport(self.src.sid, self.src.meta, store_loader=lambda: self.source_store, change_log_loader=lambda: load_change_log(self.session, self.src.id))
            self._client = AbapAddonClient(t)
        return self._client

    @property
    def source_store(self) -> RecordStore:
        if self._source_store is None:
            self._source_store = RecordStore.load(self.session, self.src.id)
        return self._source_store

    def cdc_objects(self) -> list[dict]:
        """What to subscribe to: every business object type the manifest transfers, header predicates pushed down
        for company-code-owned tables, item tables unfiltered (scope decided per header by the engine)."""
        types = sorted({c["type"] for c in self.cls.values() if c["classification"] in TRANSFER_CLASSES and c["type"] in BUSINESS_OBJECTS})
        objs: list[dict] = []
        for t in types:
            bo = BUSINESS_OBJECTS[t]
            htd = TABLES.get(bo.header_table)
            if htd is not None and htd.org_field and htd.org_field.startswith("BUKRS"):
                for cc in sorted(self.scope_ccs):
                    objs.append({"OBJECT_TYPE": t, "TABNAME": bo.header_table, **predicate(htd.org_field, "EQ", cc)})
            else:
                objs.append({"OBJECT_TYPE": t, "TABNAME": bo.header_table, "FIELD": "", "OP": "", "LOW": "", "HIGH": ""})
            for it in bo.item_tables:
                if it in TABLES:
                    objs.append({"OBJECT_TYPE": t, "TABNAME": it, "FIELD": "", "OP": "", "LOW": "", "HIGH": ""})
        return objs

    # -- scope
    @staticmethod
    def object_key(table: str, record_key: str) -> tuple[str, str]:
        bo_id = _TABLE_BO.get(table, "")
        if not bo_id:
            return "", record_key
        bo = BUSINESS_OBJECTS[bo_id]
        return bo_id, "|".join(record_key.split("|")[: len(bo.key_fields)])

    def _row_in_scope(self, table: str, row: dict | None) -> bool | None:
        """Organisational test on a header image: True/False, or None when the table is not company-code-owned."""
        td = TABLES.get(table)
        if row is None or td is None or not td.org_field or not td.org_field.startswith("BUKRS"):
            return None
        return row.get(td.org_field) in self.scope_ccs

    def _header_in_scope(self, bo_id: str, okey: str, batch_headers: dict[tuple[str, str], dict | None]) -> tuple[bool, str]:
        node = f"{bo_id}:{okey}"
        c = self.cls.get(node)
        if c is not None:
            return (c["classification"] in TRANSFER_CLASSES, "" if c["classification"] in TRANSFER_CLASSES else f"excluded_by_manifest:{c['classification']}")
        if node in self._header_cache:
            return self._header_cache[node], "" if self._header_cache[node] else "out_of_scope"
        bo = BUSINESS_OBJECTS[bo_id]
        hdr = batch_headers.get((bo.header_table, okey))
        if hdr is None:
            hdr = self.source_store.by_key(bo.header_table, okey)
        verdict = self._row_in_scope(bo.header_table, hdr)
        if verdict is None:  # client-level master created after the snapshot: in scope only with an org view of ours
            verdict = any(self._row_in_scope(it, batch_headers.get((it, k))) for (it, k) in batch_headers if it in bo.item_tables and k.startswith(okey + "|")) if hdr is not None else False
            reason = "" if verdict else ("new_master_unreferenced" if hdr is not None else "header_unknown")
        else:
            reason = "" if verdict else "out_of_scope"
        self._header_cache[node] = bool(verdict)
        return bool(verdict), reason

    # -- stages
    def capture(self) -> dict:
        wm_from = str(self.run.metrics.get("watermark_from", "0"))
        objects = self.cdc_objects()
        events = list(self.client.cdc_events(wm_from, objects))
        wm_to = self.client.last_watermark or wm_from
        batch_headers: dict[tuple[str, str], dict | None] = {(e["TABNAME"], e["KEY"]): e.get("row") for e in events}
        rows: list[DeltaEvent] = []
        existing = {s for (s,) in self.session.execute(select(DeltaEvent.seq).where(DeltaEvent.baseline_run_id == self.base.id))}
        m = {"captured": len(events), "in_scope": 0, "filtered": 0, "duplicates_ignored": 0, "by_table": Counter(), "by_op": Counter(), "filtered_by_reason": Counter(), "rfc_calls": self.client.calls, "packages": self.client.packages, "watermark_from": wm_from, "watermark_to": wm_to}
        oldest = None
        for e in events:
            seq = int(e["SEQ"])
            if seq in existing:
                m["duplicates_ignored"] += 1  # a cycle re-run after a crash: the ledger already knows this event
                continue
            bo_id, okey = self.object_key(e["TABNAME"], e["KEY"])
            ok, reason = (self._header_in_scope(bo_id, okey, batch_headers) if bo_id else (False, "table_not_in_catalog"))
            de = DeltaEvent(run_id=self.run.id, baseline_run_id=self.base.id, seq=seq, changenr=e.get("CHANGENR", ""), object_type=bo_id, object_key=okey, table_name=e["TABNAME"], record_key=e["KEY"], op=e["OP"], changed_at=e.get("CHANGED_AT", ""), changed_by=e.get("CHANGED_BY", ""), source_payload=e.get("row"), status="CAPTURED" if ok else "FILTERED", message="" if ok else reason)
            rows.append(de)
            m["by_table"][e["TABNAME"]] += 1
            m["by_op"][e["OP"]] += 1
            if ok:
                m["in_scope"] += 1
                oldest = min(oldest, e.get("CHANGED_AT") or "") if oldest else (e.get("CHANGED_AT") or None)
            else:
                m["filtered"] += 1
                m["filtered_by_reason"][reason] += 1
        self.session.add_all(rows)
        self.session.flush()
        if oldest:
            m["lag_seconds"] = _lag(oldest)
        m["by_table"], m["by_op"], m["filtered_by_reason"] = dict(m["by_table"]), dict(m["by_op"]), dict(m["filtered_by_reason"])
        self.run.metrics = {**self.run.metrics, "watermark_to": wm_to}
        return m

    def _staged_target_key(self, table: str, record_key: str) -> str | None:
        last = self.session.execute(select(DeltaEvent.target_key).where(DeltaEvent.baseline_run_id == self.base.id, DeltaEvent.table_name == table, DeltaEvent.record_key == record_key, DeltaEvent.status == "APPLIED", DeltaEvent.target_key.isnot(None)).order_by(DeltaEvent.seq.desc()).limit(1)).scalar()
        if last:
            return last
        if table not in self._staged_keys:
            self._staged_keys[table] = {s.record_key: s.target_key for s in self.backend.iter_records(self.base.id, table=table) if s.target_key}
        return self._staged_keys[table].get(record_key)

    def transform(self) -> dict:
        evs = self.session.execute(select(DeltaEvent).where(DeltaEvent.run_id == self.run.id, DeltaEvent.status == "CAPTURED").order_by(DeltaEvent.seq)).scalars().all()
        m = {"events": len(evs), "transformed": 0, "deletes_resolved": 0, "rejected": 0, "skipped": 0, "change_sets_rejected": 0, "by_rule": Counter()}
        groups: dict[str, list[DeltaEvent]] = defaultdict(list)
        for e in evs:
            groups[e.changenr or f"seq{e.seq}"].append(e)
        exceptions = []
        for cs, members in groups.items():
            failed = None
            for e in members:
                if e.op == "D":
                    tk = self._staged_target_key(e.table_name, e.record_key)
                    if tk is None:
                        e.status, e.message = "SKIPPED_MISSING", "deleted row was never loaded into the target"
                        continue
                    e.target_key = tk
                    m["deletes_resolved"] += 1
                    continue
                try:
                    out, lineage = transform_record(self.rs, e.table_name, e.source_payload or {})
                except SkipRecord as ex:
                    e.status, e.message = "SKIPPED", f"rule {ex.rule_id} skips this record"
                    m["skipped"] += 1
                    continue
                except RuleError as ex:
                    failed = (e, ex)
                    break
                e.target_payload, e.target_key = out, target_key(e.table_name, out)
                for l in lineage:
                    m["by_rule"][l["rule"]] += 1
                m["transformed"] += 1
            if failed:
                e0, ex = failed
                m["change_sets_rejected"] += 1
                for e in members:
                    if e.status == "CAPTURED":
                        e.status = "REJECTED"
                        e.message = f"change set {cs} rejected atomically: {ex.rule_id}: {ex.message} ({e0.table_name} {e0.record_key})"
                        m["rejected"] += 1
                exceptions.append(TransformationException(run_id=self.run.id, stage="DELTA_TRANSFORM", table_name=e0.table_name, record_key=e0.record_key, rule_id=ex.rule_id, severity="ERROR", message=f"change set {cs}: {ex.message}"))
        self.session.add_all(exceptions)
        self.session.flush()
        m["by_rule"] = dict(m["by_rule"])
        return m

    def apply(self) -> dict:
        evs = self.session.execute(select(DeltaEvent).where(DeltaEvent.run_id == self.run.id, DeltaEvent.status == "CAPTURED", DeltaEvent.target_key.isnot(None))).scalars().all()
        rank = type_order()
        evs.sort(key=lambda e: (rank.get(e.object_type, 99), e.changed_at, e.seq))
        m = {"events": len(evs), "inserted": 0, "updated": 0, "deleted": 0, "skipped_duplicate": 0, "skipped_missing": 0, "conflicts": 0, "by_table": Counter()}
        tables = sorted({e.table_name for e in evs})
        target = RecordStore.load(self.session, self.tgt.id, tables=tables) if tables else RecordStore(self.tgt.id)
        last_applied: dict[tuple[str, str], int] = {}
        for (t, k, s) in self.session.execute(select(DeltaEvent.table_name, DeltaEvent.target_key, func.max(DeltaEvent.seq)).where(DeltaEvent.baseline_run_id == self.base.id, DeltaEvent.status == "APPLIED").group_by(DeltaEvent.table_name, DeltaEvent.target_key)):
            last_applied[(t, k)] = s
        staged_new: list[StagedRow] = []
        staged_upd: list[StagedRow] = []
        for e in evs:
            key = (e.table_name, e.target_key)
            existing = target.by_key(e.table_name, e.target_key)
            if e.op == "D":
                if existing is None:
                    e.status, e.message = "SKIPPED_MISSING", "already absent from target"
                    m["skipped_missing"] += 1
                    continue
                delete_records(self.session, self.tgt.id, e.table_name, [e.target_key])
                target._by_key[e.table_name].pop(e.target_key, None)
                e.status, e.action = "APPLIED", "DELETED"
                m["deleted"] += 1
                staged_upd.append(StagedRow("DELTA", e.table_name, e.record_key, e.source_payload or {}, None, e.target_key, [{"rule": "delta", "field": "*", "from": "record", "to": "deleted"}], "DELETED"))
            elif existing is None:
                upsert_records(self.session, self.tgt.id, e.table_name, [e.target_payload])
                target._by_key[e.table_name][e.target_key] = e.target_payload
                e.status, e.action = "APPLIED", "INSERTED"
                m["inserted"] += 1
                staged_new.append(StagedRow(f"DELTA:{self.run.metrics.get('cycle', 0)}", e.table_name, e.record_key, e.source_payload or {}, e.target_payload, e.target_key, [{"rule": "delta", "field": "*", "from": "cdc", "to": "inserted"}], "LOADED"))
            elif existing == e.target_payload:
                e.status, e.message = "SKIPPED_DUPLICATE", "target already holds this content"
                m["skipped_duplicate"] += 1
            elif e.seq > last_applied.get(key, -1):
                upsert_records(self.session, self.tgt.id, e.table_name, [e.target_payload])
                target._by_key[e.table_name][e.target_key] = e.target_payload
                e.status, e.action = "APPLIED", "UPDATED"
                m["updated"] += 1
                staged_new.append(StagedRow(f"DELTA:{self.run.metrics.get('cycle', 0)}", e.table_name, e.record_key, e.source_payload or {}, e.target_payload, e.target_key, [], "LOADED"))
                staged_upd.append(StagedRow("DELTA", e.table_name, e.record_key, e.source_payload or {}, e.target_payload, e.target_key, [{"rule": "delta", "field": "*", "from": "cdc", "to": "updated"}], "LOADED"))
            else:
                e.status, e.message = "CONFLICT", f"stale event: target was updated by sequence {last_applied[key]} after this change ({e.seq}); target content differs"
                m["conflicts"] += 1
                continue
            last_applied[key] = e.seq
            m["by_table"][e.table_name] += 1
        # keep the baseline's staging in step so its three-layer reconciliation stays meaningful after deltas
        if staged_new:
            self.backend.write_partition(self.base.id, f"DELTA:{self.run.metrics.get('cycle', 0)}", staged_new)
        if staged_upd:
            self.backend.update_records(self.base.id, staged_upd)
        self.session.flush()
        m["by_table"] = dict(m["by_table"])
        return m

    def reconcile(self) -> dict:
        applied = self.session.execute(select(DeltaEvent).where(DeltaEvent.run_id == self.run.id, DeltaEvent.status == "APPLIED")).scalars().all()
        tables = sorted({e.table_name for e in applied})
        target = RecordStore.load(self.session, self.tgt.id, tables=tables) if tables else RecordStore(self.tgt.id)
        results: list[ReconciliationResult] = []
        by_table: dict[str, list[DeltaEvent]] = defaultdict(list)
        for e in applied:
            by_table[e.table_name].append(e)
        for t, evs in sorted(by_table.items()):
            bad = []
            for e in evs:
                row = target.by_key(t, e.target_key)
                if e.action == "DELETED":
                    if row is not None:
                        bad.append({"key": e.target_key, "reason": "still present after delete"})
                elif row != e.target_payload:
                    bad.append({"key": e.target_key, "reason": "target content differs from transformed event"})
            results.append(_r(self.run.id, "TECHNICAL", "delta_apply", "PASS" if not bad else "FAIL", t, len(evs), len(evs) - len(bad), len(bad), "Every applied event re-read from the target equals its transformed image" if not bad else "Applied events whose target image differs", {"samples": bad[:10]}))
        counts = Counter(s for (s,) in self.session.execute(select(DeltaEvent.status).where(DeltaEvent.run_id == self.run.id)))
        status = "PASS" if not counts.get("CONFLICT") and not counts.get("REJECTED") else ("FAIL" if counts.get("CONFLICT") else "WARN")
        results.append(_r(self.run.id, "FUNCTIONAL", "delta_scope", status, "events", counts.get("CAPTURED", 0) + sum(v for k, v in counts.items() if k not in ("CAPTURED", "FILTERED")), counts.get("APPLIED", 0), counts.get("CONFLICT", 0) + counts.get("REJECTED", 0), f"{counts.get('FILTERED', 0)} event(s) outside the approved scope ignored; {counts.get('REJECTED', 0)} rejected by rules; {counts.get('CONFLICT', 0)} stale conflicts", dict(counts)))
        self.session.add_all(results)
        self.session.flush()
        summary = summarize(results)
        if self.run.metrics.get("final"):
            summary["final_reconciliation"] = self.final_reconciliation()
            if summary["final_reconciliation"]["overall"] == "FAIL":
                summary["overall"] = "FAIL"
            elif summary["final_reconciliation"]["overall"] == "WARN" and summary["overall"] == "PASS":
                summary["overall"] = "WARN"
        return summary

    def final_reconciliation(self) -> dict:
        """Stage 10: the baseline's full three-layer reconciliation against the current source and target, with the
        manifest's document statuses refreshed from the source (they legitimately moved during the delta window)."""
        source = RecordStore.load(self.session, self.src.id)
        target = RecordStore.load(self.session, self.tgt.id)
        cls = {}
        for n, c in self.cls.items():
            bo = BUSINESS_OBJECTS.get(c["type"])
            if bo is not None and bo.kind == "TRANSACTIONAL" and c["classification"] in TRANSFER_CLASSES:
                row = source.by_key(bo.header_table, n.split(":", 1)[1])
                cls[n] = {**c, "status": instance_status(bo, row, source) if row else c.get("status")}
            else:
                cls[n] = c
        view = SimpleNamespace(definition=self.manifest.definition, selection={**self.manifest.selection, "classification": cls})
        return reconcile_run(self.session, self.base, view, source, target, financial=True, backend=self.backend, result_run_id=self.run.id)


# ----------------------------------------------------------------------------------------- lifecycle
def _stage(run: MigrationRun, name: str) -> RunStage:
    return next(s for s in run.stages if s.name == name)


def delta_cycles(session: Session, baseline_run_id: str) -> list[MigrationRun]:
    runs = session.execute(select(MigrationRun).where(MigrationRun.manifest_id == session.get(MigrationRun, baseline_run_id).manifest_id).order_by(MigrationRun.created_at)).scalars().all()
    return [r for r in runs if r.metrics.get("kind") == "DELTA" and r.metrics.get("baseline_run_id") == baseline_run_id]


def current_watermark(session: Session, base: MigrationRun) -> str:
    done = [r for r in delta_cycles(session, base.id) if r.status == "COMPLETED" and r.metrics.get("watermark_to")]
    if done:
        return str(done[-1].metrics["watermark_to"])
    ex = next((s for s in base.stages if s.name == "EXTRACT"), None)
    return str(((ex.metrics if ex else {}) or {}).get("adapter_stats", {}).get("cdc_watermark") or "0")


def declare_freeze(session: Session, base: MigrationRun, actor: str, note: str = "") -> dict:
    if base.metrics.get("freeze"):
        raise RunPrecondition("business freeze already declared")
    freeze = {"declared_at": _now().isoformat(), "declared_by": actor, "note": note, "company_codes": sorted(session.get(ScopeManifest, base.manifest_id).definition["company_codes"])}
    base.metrics = {**base.metrics, "freeze": freeze}
    session.flush()
    record_event(session, actor, "BUSINESS_FREEZE_DECLARED", "RUN", base.id, freeze)
    return freeze


def frozen_company_codes(session: Session, project_id: str) -> set[str]:
    out: set[str] = set()
    for r in session.execute(select(MigrationRun).where(MigrationRun.project_id == project_id)).scalars():
        if r.metrics.get("freeze"):
            out |= set(r.metrics["freeze"].get("company_codes", []))
    return out


def start_delta_cycle(session: Session, baseline_run_id: str, actor: str, final: bool = False) -> MigrationRun:
    base = session.get(MigrationRun, baseline_run_id)
    if base is None:
        raise RunPrecondition("baseline run not found")
    if base.metrics.get("kind") == "DELTA":
        raise RunPrecondition("delta cycles are started from the baseline (initial load) run, not from another cycle")
    if base.status != "COMPLETED":
        raise RunPrecondition(f"baseline run is {base.status}; delta capture needs a completed initial load")
    src = session.get(SapSystem, base.source_system_id)
    if src.connector != "RFC":
        raise RunPrecondition(f"delta capture needs the RFC connector (Z_SDTF_CDC_POLL on the SAP add-on); source {src.sid} uses {src.connector}, which has no change log")
    cycles = delta_cycles(session, base.id)
    if any(c.status == "RUNNING" for c in cycles):
        raise RunPrecondition("a delta cycle is already running for this baseline")
    if any(c.metrics.get("final") and c.status == "COMPLETED" for c in cycles):
        raise RunPrecondition("the final delta has been synchronised; the baseline is in cutover state and accepts no further cycles")
    if final and not base.metrics.get("freeze"):
        raise RunPrecondition("the final delta requires a declared business freeze (POST .../delta/freeze)")
    run = MigrationRun(project_id=base.project_id, manifest_id=base.manifest_id, ruleset_id=base.ruleset_id, source_system_id=base.source_system_id, target_system_id=base.target_system_id, mode=base.mode, status="RUNNING", started_by=actor, started_at=_now(), snapshot_id=base.snapshot_id, metrics={"kind": "DELTA", "baseline_run_id": base.id, "cycle": len(cycles) + 1, "final": bool(final), "watermark_from": current_watermark(session, base), "execution": "INLINE", "staging_backend": base.metrics.get("staging_backend")})
    session.add(run)
    session.flush()
    for i, name in enumerate(DELTA_STAGES):
        session.add(RunStage(run_id=run.id, sequence=i, name=name))
    session.flush()
    record_event(session, actor, "DELTA_CYCLE_STARTED", "RUN", run.id, {"baseline": base.id, "cycle": run.metrics["cycle"], "final": bool(final), "watermark_from": run.metrics["watermark_from"]})
    return execute_delta_cycle(session, run, actor)


def execute_delta_cycle(session: Session, run: MigrationRun, actor: str) -> MigrationRun:
    eng = DeltaEngine(session, run)
    span = obs.span("sdtf.delta_cycle", run_id=run.id, baseline=eng.base.id, cycle=run.metrics.get("cycle"), final=run.metrics.get("final", False))
    span.__enter__()
    try:
        for name in DELTA_STAGES:
            st = _stage(run, name)
            st.status, st.started_at = "RUNNING", _now()
            session.flush()
            t0 = time.monotonic()
            if name == "PRECHECK":
                st.metrics = {"baseline": eng.base.id, "manifest_hash": eng.manifest.content_hash, "ruleset_hash": session.get(RuleSet, run.ruleset_id).content_hash, "watermark_from": run.metrics["watermark_from"], "freeze": bool(eng.base.metrics.get("freeze")), "final": run.metrics.get("final", False), "source": f"{eng.src.sid}/{eng.src.client}", "connector": eng.src.connector}
            elif name == "CAPTURE":
                st.metrics = eng.capture()
            elif name == "TRANSFORM":
                st.metrics = eng.transform()
            elif name == "APPLY":
                st.metrics = eng.apply()
            elif name == "RECONCILE":
                st.metrics = eng.reconcile()
            elif name == "REPORT":
                st.status, st.finished_at = "DONE", _now()
                st.duration_ms = round((time.monotonic() - t0) * 1000, 1)
                run.report = build_delta_report(session, run)
                st.metrics = {"events": run.report["events"]["total"]}
            st.status, st.finished_at = "DONE", _now()
            st.duration_ms = max(st.duration_ms, round((time.monotonic() - t0) * 1000, 1))
            session.flush()
        run.status, run.finished_at = "COMPLETED", _now()
        if run.metrics.get("final"):
            eng.base.metrics = {**eng.base.metrics, "final_delta": {"run_id": run.id, "at": _now().isoformat(), "reconciliation": run.report["reconciliation"].get("overall")}}
        session.flush()
        record_event(session, actor, "DELTA_CYCLE_COMPLETED", "RUN", run.id, {"baseline": eng.base.id, "cycle": run.metrics.get("cycle"), "final": run.metrics.get("final", False), "reconciliation": run.report["reconciliation"].get("overall"), "watermark_to": run.metrics.get("watermark_to")})
        span.__exit__(None, None, None)
    except Exception as e:  # noqa: BLE001 - persist the failure, then re-raise
        for s in run.stages:
            if s.status == "RUNNING":
                s.status = "FAILED"
                s.metrics = {**(s.metrics or {}), "error": str(e)}
        run.status, run.finished_at = "FAILED", _now()
        session.flush()
        record_event(session, actor, "DELTA_CYCLE_FAILED", "RUN", run.id, {"error": str(e)})
        import sys

        span.__exit__(*sys.exc_info())
        raise
    return run


def build_delta_report(session: Session, run: MigrationRun) -> dict:
    stages = {s.name: s.metrics for s in run.stages}
    counts = Counter(s for (s,) in session.execute(select(DeltaEvent.status).where(DeltaEvent.run_id == run.id)))
    recon = stages.get("RECONCILE", {}) or {}
    md = [f"# Delta cycle {run.metrics.get('cycle')} of baseline {run.metrics.get('baseline_run_id', '')[:8]} ({'FINAL' if run.metrics.get('final') else 'incremental'})", "", "> Simulated: changes come from the simulated SAP add-on's change log; the target is the simulated record store. No SAP system was read or written.", "", f"Watermark {run.metrics.get('watermark_from')} → {run.metrics.get('watermark_to')}", "", "| Status | Events |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in sorted(counts.items())]
    md += ["", f"Apply: {stages.get('APPLY', {})}", "", f"Reconciliation: **{recon.get('overall', '-')}**"]
    return {"kind": "DELTA", "run_id": run.id, "baseline_run_id": run.metrics.get("baseline_run_id"), "cycle": run.metrics.get("cycle"), "final": run.metrics.get("final", False), "watermark_from": run.metrics.get("watermark_from"), "watermark_to": run.metrics.get("watermark_to"), "events": {"total": sum(counts.values()), **dict(counts)}, "stages": {k: v for k, v in stages.items()}, "reconciliation": recon, "markdown": "\n".join(md), "disclaimer": "Simulated delta synchronisation on synthetic data; no downtime figure is derived from it."}


def delta_state(session: Session, base: MigrationRun, with_backlog: bool = True) -> dict:
    src = session.get(SapSystem, base.source_system_id)
    cycles = delta_cycles(session, base.id)
    wm = current_watermark(session, base)
    out = {"baseline_run_id": base.id, "baseline_status": base.status, "source": {"sid": src.sid, "connector": src.connector, "transport": (src.meta.get("rfc") or {}).get("transport", "auto") if src.connector == "RFC" else None}, "cdc_supported": src.connector == "RFC", "watermark": wm, "freeze": base.metrics.get("freeze"), "final_delta": base.metrics.get("final_delta"), "cutover_ready": bool(base.metrics.get("final_delta") and base.metrics["final_delta"].get("reconciliation") == "PASS"), "cycles": []}
    for c in cycles:
        st = {s.name: s for s in c.stages}
        cap, app, rec = (st.get("CAPTURE").metrics if st.get("CAPTURE") else {}) or {}, (st.get("APPLY").metrics if st.get("APPLY") else {}) or {}, (st.get("RECONCILE").metrics if st.get("RECONCILE") else {}) or {}
        out["cycles"].append({"id": c.id, "cycle": c.metrics.get("cycle"), "final": c.metrics.get("final", False), "status": c.status, "started_at": c.started_at.isoformat() if c.started_at else None, "duration_ms": round(sum(s.duration_ms for s in c.stages), 1), "watermark_from": c.metrics.get("watermark_from"), "watermark_to": c.metrics.get("watermark_to"), "captured": cap.get("captured", 0), "in_scope": cap.get("in_scope", 0), "filtered": cap.get("filtered", 0), "lag_seconds": cap.get("lag_seconds"), "applied": app.get("inserted", 0) + app.get("updated", 0) + app.get("deleted", 0), "conflicts": app.get("conflicts", 0), "rejected": (st.get("TRANSFORM").metrics if st.get("TRANSFORM") else {} or {}).get("rejected", 0), "reconciliation": rec.get("overall"), "final_reconciliation": (rec.get("final_reconciliation") or {}).get("overall"), "error": next((s.metrics.get("error") for s in c.stages if s.status == "FAILED"), None)})
    if with_backlog and out["cdc_supported"] and base.status == "COMPLETED":
        try:
            probe = MigrationRun(project_id=base.project_id, manifest_id=base.manifest_id, ruleset_id=base.ruleset_id, source_system_id=base.source_system_id, target_system_id=base.target_system_id, started_by="probe", metrics={"kind": "DELTA", "baseline_run_id": base.id, "watermark_from": wm, "staging_backend": base.metrics.get("staging_backend")})
            eng = DeltaEngine(session, probe)
            events = list(eng.client.cdc_events(wm, eng.cdc_objects(), max_packages=10))
            batch_headers = {(e["TABNAME"], e["KEY"]): e.get("row") for e in events}
            in_scope = 0
            oldest = None
            for e in events:
                bo_id, okey = eng.object_key(e["TABNAME"], e["KEY"])
                if bo_id and eng._header_in_scope(bo_id, okey, batch_headers)[0]:
                    in_scope += 1
                    oldest = min(oldest, e.get("CHANGED_AT") or "") if oldest else (e.get("CHANGED_AT") or None)
            out["backlog"] = {"events": len(events), "in_scope": in_scope, "lag_seconds": _lag(oldest) if oldest else None, "truncated": len(events) >= 10 * eng.client.package_size}
        except Exception as e:  # noqa: BLE001 - the monitor must render even when the source is unreachable
            out["backlog"] = {"error": str(e)}
    return out
