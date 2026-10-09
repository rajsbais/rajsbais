"""Automated delta refresh (Module 8).

A *scenario* is a standing, approved, repeatable refresh: one or more rolling scopes (e.g. customers, vendors,
materials, sales orders and purchase orders of company 1000) kept in sync into one non-production target.

Honest mechanics (not every object supports the same delta mechanism):
  change_documents : changes are found through the source change log (CDHDR/CDPOS stand-in)
  created_only     : immutable documents - only *new* documents are picked up from the change log; modifications are
                     not trusted to the log and are caught by the periodic full sweep
  full_compare     : no usable change log - every tracked object is compared by content hash on every run
A *full sweep* (initial run, every N runs, on demand, or forced by a change-log gap) re-compares every object by hash.

Safety properties: standing approval bound to a configuration hash with separation of duties; non-production targets only;
watermarks advance only after a run completed AND reconciled clean; target drift is detected before a previously loaded
object is overwritten; number ranges and duplicate keys go through the same conflict analysis as the initial copy;
failed runs keep a checkpoint and can be resumed or rolled back; nothing is ever deleted from the target automatically.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..dependency.planner import Issue, Plan, Planner
from ..dependency.registry import CONFIG_TYPES
from ..masking.engine import MaskingEngine, MaskingPolicy, MaskMode, discover_sensitive, template
from ..reconcile.validator import reconcile
from ..sap.adapter import ChangeLogGap
from ..sap.ddic import TABLES
from ..security.auth import Forbidden, Principal, check_separation_of_duties
from ..selective.conflicts import Action, analyze, validate_policy
from ..selective.executor import Run, row_hash
from ..selective.manifest import Manifest, Scope
from ..service import Conflict, NotFound

PROFILES = {
    "CUSTOMER": "change_documents", "VENDOR": "change_documents", "MATERIAL": "change_documents",
    "SALES_ORDER": "change_documents", "DELIVERY": "change_documents", "PURCHASE_ORDER": "change_documents",
    "BILLING": "created_only", "FI_DOCUMENT": "created_only",
}
DEFAULT_PROFILE = "full_compare"  # custom Z objects etc.


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


@dataclass
class Tracked:
    src_hash: str  # hash of the unmasked source rows when last loaded
    tgt_hash: str  # hash of the masked rows we wrote
    keys: list[tuple[str, tuple]]
    run_id: str
    run_no: int


@dataclass
class Scenario:
    id: str
    name: str
    source_id: str
    target_id: str
    scopes: list[dict]  # [{scope: Scope, rolling_days: int|None}]
    include_downstream: list[str]
    masking_policy: MaskingPolicy
    conflict_policy: dict[str, str]
    schedule: dict | None
    full_sweep_every: int
    max_objects: int
    created_by: str
    mask_key: bytes = field(default_factory=lambda: os.urandom(32))  # stable across runs; KMS-held in production
    last_editor: str | None = None
    submitted_by: str | None = None
    config_version: int = 1
    status: str = "DRAFT"  # DRAFT PENDING_APPROVAL APPROVED RUNNING FAILED ATTENTION
    approval: dict | None = None
    watermark: int | None = None
    tracked: dict[str, Tracked] = field(default_factory=dict)
    deferred: dict[str, str] = field(default_factory=dict)  # skipped/quarantined earlier: re-evaluated every run
    stale: set[str] = field(default_factory=set)  # tracked objects whose source change was NOT applied (skipped/quarantined)
    history: list[dict] = field(default_factory=list)
    next_due: datetime | None = None
    pending: dict | None = None  # failed run context (resume / rollback)
    created: str = field(default_factory=lambda: _iso(_now()))

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "source_id": self.source_id, "target_id": self.target_id,
                "scopes": [{"scope": s["scope"].model_dump(mode="json", exclude_defaults=True), "rolling_days": s.get("rolling_days")}
                           for s in self.scopes],
                "include_downstream": self.include_downstream, "masking_policy": self.masking_policy.id,
                "conflict_policy": self.conflict_policy, "schedule": self.schedule, "full_sweep_every": self.full_sweep_every,
                "max_objects": self.max_objects, "created_by": self.created_by, "status": self.status,
                "config_version": self.config_version, "config_hash": config_hash(self), "approval": self.approval,
                "watermark": self.watermark, "tracked_objects": len(self.tracked), "deferred_objects": len(self.deferred), "stale_objects": len(self.stale), "next_due": _iso(self.next_due),
                "runs": len(self.history), "last_run": self.history[-1] if self.history else None,
                "pending_run": self.pending["run"].id if self.pending else None, "simulated": True}


def config_hash(sc: Scenario) -> str:
    body = {"scopes": [{"scope": s["scope"].model_dump(mode="json"), "rolling_days": s.get("rolling_days")} for s in sc.scopes],
            "down": sorted(sc.include_downstream), "mask": sc.masking_policy.id, "rules": sorted((r.table, r.field, r.strategy, r.mode.value) for r in sc.masking_policy.rules),
            "policy": sc.conflict_policy,
            "sweep": sc.full_sweep_every, "max": sc.max_objects, "source": sc.source_id, "target": sc.target_id}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- schedule helpers
def next_occurrence(schedule: dict, after: datetime) -> datetime:
    if schedule["kind"] == "interval":
        return after + timedelta(hours=schedule["every_hours"])
    # weekly: weekday 0=Mon..6=Sun at hour (UTC)
    cand = after.replace(hour=schedule["hour"], minute=0, second=0, microsecond=0)
    cand += timedelta(days=(schedule["weekday"] - cand.weekday()) % 7)
    if cand <= after:
        cand += timedelta(days=7)
    return cand


def validate_schedule(s: dict | None) -> list[str]:
    if s is None:
        return []
    errs = []
    if s.get("kind") == "interval":
        if not isinstance(s.get("every_hours"), int) or s["every_hours"] < 1:
            errs.append("interval schedule needs every_hours >= 1")
    elif s.get("kind") == "weekly":
        if s.get("weekday") not in range(7) or s.get("hour") not in range(24):
            errs.append("weekly schedule needs weekday 0-6 and hour 0-23")
    else:
        errs.append("schedule kind must be 'weekly' or 'interval'")
    if s.get("window_hours", 4) < 1:
        errs.append("window_hours must be >= 1")
    return errs


# ---------------------------------------------------------------- hashing / mapping
def src_hash(inst) -> str:
    body = {t: sorted(json.dumps(r, sort_keys=True, default=str) for r in rows) for t, rows in sorted(inst.rows.items())}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def rows_hash(hashes: list[str]) -> str:
    return hashlib.sha256("".join(sorted(hashes)).encode()).hexdigest()


def merge_plans(plans: list[Plan], planner: Planner, cfg_hash: str) -> Plan:
    inst, issues, cfg = {}, [], {}
    for p in plans:
        for iid, i in p.instances.items():
            inst.setdefault(iid, i)
            if i.origin == "ROOT":
                inst[iid].origin = "ROOT"
        issues += p.issues
        for k, v in p.config_refs.items():
            cfg.setdefault(k, set()).update(v)
    seen, uniq = set(), []
    for i in issues:
        key = (i.code, i.message)
        if key not in seen:
            seen.add(key); uniq.append(i)
    from ..dependency.registry import find_cycles
    edges = {i: {r for r in x.requires if r in inst} for i, x in inst.items()}
    cycles = find_cycles(edges)
    return Plan(cfg_hash, inst, planner._topo(inst, edges), cfg, uniq, cycles)


def headers_for(reg, source, table: str, key: dict) -> list[str]:
    """Map a changed row (table + key) to the business objects (instance ids) that contain it."""
    out: list[str] = []
    for ot in reg.types.values():
        if table not in {l.table for l in ot.tables}:
            continue
        if table == "ADRC":
            for t, fld in (("KNA1", "KUNNR"), ("LFA1", "LIFNR")):
                for r in source.lookup(t, "ADRNR", key["ADDRNUMBER"]):
                    for o in reg.types.values():
                        if o.header == t:
                            out.append(f"{o.name}:{r[fld]}")
        elif table == "BUT000":
            out += [f"{ot.name}:{key['PARTNER']}"]
        elif table == "ACDOCA":
            out.append(f"{ot.name}:{key['RBUKRS']}/{key['BELNR']}/{key['GJAHR']}")
        elif all(h in key for h in ot.header_keys):
            out.append(f"{ot.name}:" + "/".join(str(key[h]) for h in ot.header_keys))
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- service
class DeltaService:
    def __init__(self, svc):
        self.svc = svc
        self.scenarios: dict[str, Scenario] = {}

    # ---- lifecycle -----------------------------------------------------
    def get(self, sid: str) -> Scenario:
        if sid not in self.scenarios:
            raise NotFound(f"delta scenario {sid}")
        return self.scenarios[sid]

    def _validate(self, spec: dict, src, tgt) -> MaskingPolicy:
        if tgt.is_production or not tgt.can_be_write_target:
            raise Forbidden(f"{tgt.label} cannot be a delta refresh target")
        if src.id == tgt.id:
            raise Conflict("source and target must differ")
        if src.family != tgt.family:
            raise Conflict("ECC↔S/4HANA is a migration, not a refresh")
        errs = validate_policy(spec.get("conflict_policy", {})) + validate_schedule(spec.get("schedule"))
        if errs:
            raise Conflict("; ".join(errs))
        try:
            pol = template(spec.get("masking_policy_id", "gdpr-standard"))
        except KeyError:
            raise Conflict("unknown masking policy")
        if any(r.mode == MaskMode.ANONYMIZE for r in pol.rules):
            raise Conflict("delta refresh needs stable masking (PSEUDONYMIZE/TOKENIZE): per-run ANONYMIZE keys would make "
                           "names inconsistent between objects loaded in different runs")
        if not spec.get("scopes"):
            raise Conflict("at least one scope is required")
        return pol

    def create(self, actor: Principal, spec: dict) -> Scenario:
        src, tgt = self.svc.system(spec["source_id"]), self.svc.system(spec["target_id"])
        pol = self._validate(spec, src, tgt)
        sc = Scenario(f"dlt-{uuid.uuid4().hex[:8]}", spec["name"], src.id, tgt.id,
                      [{"scope": s["scope"] if isinstance(s["scope"], Scope) else Scope(**s["scope"]),
                        "rolling_days": s.get("rolling_days")} for s in spec["scopes"]],
                      list(spec.get("include_downstream", [])), pol, dict(spec.get("conflict_policy", {})),
                      spec.get("schedule"), int(spec.get("full_sweep_every", 4)), int(spec.get("max_objects", 10000)), actor.id)
        sc.last_editor = actor.id
        self.scenarios[sc.id] = sc
        self.svc.audit.append(actor.id, "delta.created", sc.id, {"hash": config_hash(sc)})
        return sc

    def update(self, actor: Principal, sid: str, spec: dict) -> Scenario:
        sc = self.get(sid)
        if sc.status == "RUNNING" or sc.pending:
            raise Conflict("scenario has a run in progress or an unresolved failed run")
        src, tgt = self.svc.system(sc.source_id), self.svc.system(sc.target_id)
        merged = {"name": sc.name, "scopes": sc.scopes, "include_downstream": sc.include_downstream,
                  "conflict_policy": sc.conflict_policy, "masking_policy_id": sc.masking_policy.id,
                  "schedule": sc.schedule, "source_id": sc.source_id, "target_id": sc.target_id, **spec}
        pol = self._validate(merged, src, tgt)
        before = config_hash(sc)
        sc.name, sc.include_downstream = merged["name"], list(merged["include_downstream"])
        sc.scopes = [{"scope": s["scope"] if isinstance(s["scope"], Scope) else Scope(**s["scope"]),
                      "rolling_days": s.get("rolling_days")} for s in merged["scopes"]]
        sc.conflict_policy, sc.schedule, sc.masking_policy = dict(merged["conflict_policy"]), merged["schedule"], pol
        sc.full_sweep_every = int(spec.get("full_sweep_every", sc.full_sweep_every))
        sc.max_objects = int(spec.get("max_objects", sc.max_objects))
        sc.last_editor = actor.id
        if config_hash(sc) != before:
            sc.config_version += 1
            sc.approval, sc.submitted_by, sc.status = None, None, "DRAFT"
            sc.next_due = None
        self.svc.audit.append(actor.id, "delta.updated", sc.id, {"hash": config_hash(sc), "reapproval": sc.approval is None})
        return sc

    def recommended_rules(self, sid: str) -> list[dict]:
        """Masking rules for sensitive fields discovered in the current delta that the policy does not cover yet."""
        sc = self.get(sid)
        ctx = self._prepare(sc, False)
        have = sc.masking_policy.fields()
        return [{"table": d["table"], "field": d["field"], "strategy": d["strategy"], "category": d["category"],
                 "reason": f"{d['source']} discovery, confidence {d['confidence']}"}
                for d in ctx["discovered"] if (d["table"], d["field"]) not in have]

    def add_masking_rules(self, actor: Principal, sid: str, rules: list[dict]) -> Scenario:
        if not actor.can("masking:write"):
            raise Forbidden("masking:write required")
        sc = self.get(sid)
        if sc.status == "RUNNING" or sc.pending:
            raise Conflict("scenario has a run in progress or an unresolved failed run")
        from ..masking.engine import Rule
        pol = copy.deepcopy(sc.masking_policy)
        for r in rules:
            pol.rules = [x for x in pol.rules if (x.table, x.field) != (r["table"], r["field"])]
            mode = MaskMode(r.get("mode", "PSEUDONYMIZE"))
            if mode == MaskMode.ANONYMIZE:
                raise Conflict("delta refresh needs stable masking (PSEUDONYMIZE/TOKENIZE)")
            pol.rules.append(Rule(r["table"], r["field"], r["strategy"], r.get("category", r["strategy"].lower()), mode))
        errs = pol.validate()
        if errs:
            raise Conflict("; ".join(errs))
        before = config_hash(sc)
        sc.masking_policy = pol
        sc.last_editor = actor.id
        if config_hash(sc) != before:
            sc.config_version += 1
            sc.approval, sc.submitted_by, sc.status, sc.next_due = None, None, "DRAFT", None
        self.svc.audit.append(actor.id, "delta.masking_rules_updated", sc.id, {"rules": len(rules), "reapproval": True})
        return sc

    def submit(self, actor: Principal, sid: str) -> Scenario:
        sc = self.get(sid)
        if sc.status != "DRAFT":
            raise Conflict(f"scenario is {sc.status}")
        sc.status, sc.submitted_by = "PENDING_APPROVAL", actor.id
        self.svc.audit.append(actor.id, "delta.submitted", sc.id, {"hash": config_hash(sc)})
        return sc

    def approve(self, actor: Principal, sid: str, now: datetime | None = None) -> Scenario:
        if not actor.can("plan:approve"):
            raise Forbidden("plan:approve required")
        sc = self.get(sid)
        if sc.status != "PENDING_APPROVAL":
            raise Conflict(f"scenario is {sc.status}, not PENDING_APPROVAL")
        for creator in {sc.created_by, sc.last_editor, sc.submitted_by} - {None}:
            check_separation_of_duties(creator, actor)
        now = now or _now()
        sc.approval = {"by": actor.id, "at": _iso(now), "config_hash": config_hash(sc), "version": sc.config_version}
        sc.status = "APPROVED"
        sc.next_due = next_occurrence(sc.schedule, now) if sc.schedule else None
        self.svc.audit.append(actor.id, "delta.approved", sc.id, sc.approval)
        return sc

    # ---- planning ------------------------------------------------------
    def _manifest(self, sc: Scenario, scope: Scope) -> Manifest:
        return Manifest(name=sc.name, source_system_id=sc.source_id, target_system_id=sc.target_id, scope=scope,
                        include_downstream=sc.include_downstream, masking_policy_id=sc.masking_policy.id,
                        conflict_policy=sc.conflict_policy)

    def _prepare(self, sc: Scenario, full_sweep: bool) -> dict:
        svc = self.svc
        src, tgt = svc.source_view(sc.source_id), svc.adapters[sc.target_id]
        reg = svc.registries[svc.system(sc.source_id).family]
        ref = src.reference_date()
        cfg = config_hash(sc)
        to_seq, from_seq = src.change_seq(), sc.watermark  # position is captured BEFORE reading the source
        initial, notes, ignored = from_seq is None, [], 0
        run_no = len(sc.history) + 1
        sweep = full_sweep or initial or bool(sc.full_sweep_every and run_no % sc.full_sweep_every == 0)
        if sweep and not initial:
            notes.append("full sweep: every tracked object is compared by content hash")
        changes: list[dict] = []
        if not initial:
            try:
                changes = src.changes_since(from_seq)
            except ChangeLogGap as e:
                sweep = True
                notes.append(f"change log gap ({e}); fell back to a full sweep")

        planner = Planner(src, reg)
        plans = []
        for s in sc.scopes:
            scope = s["scope"]
            if s.get("rolling_days"):
                scope = scope.model_copy(update={"date_from": ref - timedelta(days=s["rolling_days"]), "date_to": ref})
            plans.append(planner.build(self._manifest(sc, scope)))
        plan = merge_plans(plans, planner, cfg)
        cur = {iid: src_hash(i) for iid, i in plan.instances.items()}

        # candidates
        cand: set[str] = set()
        by_inst: dict[str, set[str]] = {}
        for c in changes:
            for iid in headers_for(reg, src, c["table"], c["key"]):
                by_inst.setdefault(iid, set()).add(c["op"] + ":" + c["table"])
        for iid, ops in by_inst.items():
            if iid not in plan.instances:
                continue
            ot = reg.types[plan.instances[iid].type]
            if PROFILES.get(ot.name, DEFAULT_PROFILE) == "created_only":
                if "I:" + ot.header in ops:
                    cand.add(iid)
                else:
                    ignored += 1
            else:
                cand.add(iid)
        cand |= {i for i in sc.stale if i in plan.instances}  # changes that were skipped earlier stay pending
        for iid, i in plan.instances.items():
            if sweep or PROFILES.get(i.type, DEFAULT_PROFILE) == "full_compare":
                cand.add(iid)
        if ignored and not sweep:
            notes.append(f"{ignored} modification(s) to immutable documents ignored (picked up by the next full sweep)")

        untracked = [i for i in plan.order if i not in sc.tracked]
        new = [i for i in untracked if i not in sc.deferred]
        retried = [i for i in untracked if i in sc.deferred]
        changed, drifted, drift_info = [], [], {}
        for iid in plan.order:
            t = sc.tracked.get(iid)
            if not t or iid not in cand or cur[iid] == t.src_hash:
                continue
            changed.append(iid)
            now_hashes = []
            for table, key in t.keys:
                r = tgt.get(table, key)
                now_hashes.append(row_hash(r) if r else "MISSING")
            if rows_hash(now_hashes) != t.tgt_hash:
                drifted.append(iid)
                drift_info[iid] = {"missing_rows": sum(1 for h in now_hashes if h == "MISSING"), "last_run": t.run_no}
        retained = [i for i in sc.tracked if i not in plan.instances]
        deleted = []
        for iid in retained:
            tname, key = iid.split(":", 1)
            if src.get(reg.types[tname].header, tuple(key.split("/"))) is None:
                deleted.append(iid)

        delta_ids = [i for i in plan.order if i in set(new) | set(retried) | set(changed)]
        sub = Plan(cfg, {i: plan.instances[i] for i in delta_ids}, delta_ids, plan.config_refs,
                   [x for x in plan.issues], [])
        owned = {i for i in changed if i not in drift_info}
        rows = {t: [r for i in sub.instances.values() for r in i.rows.get(t, [])] for t in
                {t for i in sub.instances.values() for t in i.rows}}
        discovered = discover_sensitive(rows)
        sens = sc.masking_policy.fields() | {(d["table"], d["field"]) for d in discovered}
        report = analyze(sub, tgt, self._manifest(sc, sc.scopes[0]["scope"]), reg, sens, owned=owned, drift=drift_info)
        blocking = [i.message for i in plan.blocking]
        if len(delta_ids) > sc.max_objects:
            blocking.append(f"delta of {len(delta_ids)} objects exceeds max_objects={sc.max_objects} (workload throttle)")
        cov = MaskingEngine(sc.masking_policy, persistent_key=sc.mask_key).coverage(discovered)
        if not cov["complete"]:
            blocking.append(f"{len(cov['missing'])} newly discovered sensitive field(s) have no masking rule: "
                            + ", ".join(f"{m['table']}.{m['field']}" for m in cov["missing"]))
        blocking += [f.message for f in report.blocking]
        summary = {
            "kind": "initial" if initial else "delta", "mode": "full_sweep" if sweep else "change_log",
            "from_seq": from_seq, "to_seq": to_seq, "scope_objects": len(plan.instances),
            "new": len(new), "retried_deferred": len(retried), "changed": len(changed), "target_drift": len(drifted),
            "loaded_planned": len(report.executable),
            "skipped": sum(1 for a in report.decisions.values() if a == Action.SKIP),
            "quarantined": sum(1 for a in report.decisions.values() if a == Action.QUARANTINE),
            "retained_out_of_scope": len(retained) - len(deleted), "deleted_in_source": len(deleted),
            "ignored_modifications": ignored, "changes_read": len(changes), "notes": notes, "blocking": blocking,
            "findings": [f.to_dict() for f in report.findings],
        }
        by_mech: dict[str, int] = {}
        for i in delta_ids:
            m = PROFILES.get(plan.instances[i].type, DEFAULT_PROFILE)
            by_mech[m] = by_mech.get(m, 0) + 1
        summary["by_mechanism"] = by_mech
        return {"summary": summary, "plan": sub, "full_plan": plan, "report": report, "discovered": discovered,
                "cur": cur, "to_seq": to_seq, "changed_ids": changed, "reg": reg, "run_no": run_no, "blocking": blocking,
                "deleted": deleted, "retained": retained}

    def preview(self, actor: Principal, sid: str, full_sweep: bool = False) -> dict:
        sc = self.get(sid)
        ctx = self._prepare(sc, full_sweep)
        self.svc.audit.append(actor.id, "delta.previewed", sc.id, {k: ctx["summary"][k] for k in ("new", "changed", "target_drift")})
        return ctx["summary"]

    # ---- execution -----------------------------------------------------
    def run(self, actor: Principal, sid: str, trigger: str = "manual", full_sweep: bool = False,
            fault_injector=None, now: datetime | None = None) -> dict:
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        sc = self.get(sid)
        if sc.pending:
            raise Conflict("an earlier run failed or is held: resume, roll back or acknowledge it first")
        if sc.status != "APPROVED" or not sc.approval or sc.approval["config_hash"] != config_hash(sc):
            raise Conflict(f"scenario is {sc.status}; it must be approved for its current configuration")
        now = now or _now()
        ctx = self._prepare(sc, full_sweep)
        rec = {"version": ctx["run_no"], **{k: ctx["summary"][k] for k in (
            "kind", "mode", "from_seq", "to_seq", "scope_objects", "new", "retried_deferred", "changed", "target_drift", "skipped", "quarantined",
            "retained_out_of_scope", "deleted_in_source", "ignored_modifications", "notes")},
            "trigger": trigger, "actor": actor.id, "started": _iso(now), "finished": None, "status": "BLOCKED", "run_id": None,
            "release": "NOT_EVALUATED", "loaded": 0, "blocking": ctx["blocking"]}
        sc.history.append(rec)
        if sc.schedule:
            sc.next_due = next_occurrence(sc.schedule, now)
        if ctx["blocking"]:
            self.svc.audit.append(actor.id, "delta.blocked", sc.id, {"version": rec["version"], "blocking": ctx["blocking"][:5]})
            return rec
        svc = self.svc
        target = svc.adapters[sc.target_id]
        svc.executor.reg = ctx["reg"]
        engine = MaskingEngine(sc.masking_policy, persistent_key=sc.mask_key)
        run = svc.executor.new_run(sc.id, ctx["plan"], ctx["report"])
        svc.runs[run.id] = run
        rec["run_id"], sc.status = run.id, "RUNNING"
        sc.pending = {"run": run, "ctx": ctx, "engine": engine, "rec": rec, "tracked_before": copy.deepcopy(sc.tracked),
                      "deferred_before": dict(sc.deferred), "stale_before": set(sc.stale)}
        svc.audit.append(actor.id, "delta.run.started", run.id, {"scenario": sc.id, "version": rec["version"], "trigger": trigger,
                                                                   "objects": len(run.order)})
        target.fault_injector = fault_injector
        try:
            svc.executor.execute(run, ctx["plan"], ctx["report"], engine, target, actor.id)
        finally:
            target.fault_injector = None
        return self._finish(sc, actor)

    def resume(self, actor: Principal, sid: str, fault_injector=None) -> dict:
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        sc = self.get(sid)
        if not sc.pending or sc.pending["run"].status != "FAILED":
            raise Conflict("no failed run to resume")
        p, svc = sc.pending, self.svc
        target = svc.adapters[sc.target_id]
        svc.executor.reg = p["ctx"]["reg"]
        sc.status = "RUNNING"
        svc.audit.append(actor.id, "delta.run.resumed", p["run"].id, {"checkpoint": p["run"].checkpoint})
        target.fault_injector = fault_injector
        try:
            svc.executor.execute(p["run"], p["ctx"]["plan"], p["ctx"]["report"], p["engine"], target, actor.id)
        finally:
            target.fault_injector = None
        return self._finish(sc, actor)

    def _finish(self, sc: Scenario, actor: Principal) -> dict:
        svc, p = self.svc, sc.pending
        run, ctx, rec = p["run"], p["ctx"], p["rec"]
        rec["status"], rec["finished"] = run.status, _iso(_now())
        if run.status != "COMPLETED":
            sc.status = "FAILED"
            return rec
        run.reconciliation = reconcile(run, ctx["plan"], svc.source_view(sc.source_id), svc.adapters[sc.target_id],
                                       p["engine"], ctx["reg"], ctx["discovered"], ctx["report"].row_exclusions)
        run.release = rec["release"] = run.reconciliation["release"]
        rec["loaded"], rec["failed_checks"] = len(run.loaded), run.reconciliation["failed"]
        for iid in run.loaded:  # what is now in the target is tracked, whether or not the gate passed
            staged = run.staged[iid]
            keys = [(t, tuple(r[k] for k in TABLES[t].keys)) for t, rows in staged.items() for r in rows]
            sc.tracked[iid] = Tracked(ctx["cur"][iid], rows_hash([row_hash(r) for rows in staged.values() for r in rows]),
                                      keys, run.id, rec["version"])
        for iid in ctx["changed_ids"]:
            if ctx["report"].decisions.get(iid) in (Action.SKIP, Action.QUARANTINE):
                sc.stale.add(iid)
        sc.stale -= set(run.loaded)
        for iid, a in ctx["report"].decisions.items():
            if a in (Action.SKIP, Action.QUARANTINE) and iid not in sc.tracked:
                sc.deferred[iid] = a.value
            elif iid in sc.tracked:
                sc.deferred.pop(iid, None)
        svc.audit.append("system", "delta.reconciliation", run.id, {"release": run.release, "failed": rec["failed_checks"]})
        if run.release == "RELEASED":
            sc.watermark, sc.status, sc.pending = ctx["to_seq"], "APPROVED", None
            rec["watermark_after"] = sc.watermark
        else:
            sc.status = "ATTENTION"  # target holds the data but the gate failed: needs rollback or acknowledgement
        return rec

    def rollback(self, actor: Principal, sid: str) -> dict:
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        sc = self.get(sid)
        if not sc.pending:
            raise Conflict("nothing to roll back (only the latest failed/held run can be rolled back)")
        p, svc = sc.pending, self.svc
        svc.executor.reg = p["ctx"]["reg"]
        svc.executor.rollback(p["run"], svc.adapters[sc.target_id], actor.id)
        sc.tracked, sc.deferred, sc.stale, sc.pending, sc.status = (p["tracked_before"], p["deferred_before"], p["stale_before"],
                                                                  None, "APPROVED")
        p["rec"]["status"], p["rec"]["release"] = "ROLLED_BACK", "HELD"
        svc.audit.append(actor.id, "delta.rolled_back", sc.id, {"run": p["run"].id})
        return p["rec"]

    def acknowledge(self, actor: Principal, sid: str, note: str) -> dict:
        """Accept a held run (gate failed) knowingly; advances the watermark. Audited, needs a note."""
        if not actor.can("run:execute") or actor.kind == "agent":
            raise Forbidden("run:execute (human) required")
        sc = self.get(sid)
        if sc.status != "ATTENTION" or not sc.pending:
            raise Conflict("no held run to acknowledge")
        if not note.strip():
            raise Conflict("a justification note is required")
        sc.watermark, sc.status = sc.pending["ctx"]["to_seq"], "APPROVED"
        sc.pending["rec"]["acknowledged"] = {"by": actor.id, "note": note}
        self.svc.audit.append(actor.id, "delta.acknowledged", sc.id, {"note": note, "failed": sc.pending["rec"].get("failed_checks")})
        sc.pending = None
        return sc.history[-1]

    # ---- scheduling ----------------------------------------------------
    def tick(self, actor: Principal, now: datetime | None = None) -> list[dict]:
        """Called by an external scheduler (cron/Control-M/Argo). Runs approved scenarios that are due and inside their window."""
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        now, out = now or _now(), []
        for sc in self.scenarios.values():
            if not sc.schedule or sc.status != "APPROVED" or not sc.next_due or now < sc.next_due:
                if sc.schedule and sc.status in ("ATTENTION", "FAILED") and sc.next_due and now >= sc.next_due:
                    out.append({"scenario": sc.id, "action": "skipped", "reason": f"status {sc.status}: needs operator action"})
                continue
            window = timedelta(hours=sc.schedule.get("window_hours", 4))
            if now > sc.next_due + window:
                missed = sc.next_due
                sc.next_due = next_occurrence(sc.schedule, now)
                self.svc.audit.append(actor.id, "delta.window_missed", sc.id, {"due": _iso(missed)})
                out.append({"scenario": sc.id, "action": "missed_window", "due": _iso(missed), "next_due": _iso(sc.next_due)})
                continue
            rec = self.run(actor, sc.id, trigger="schedule", now=now)
            out.append({"scenario": sc.id, "action": "ran", "version": rec["version"], "status": rec["status"],
                        "release": rec["release"], "new": rec["new"], "changed": rec["changed"]})
        return out
