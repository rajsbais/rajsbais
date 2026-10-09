"""Test Data Management factory (Module 9).

Self-service provisioning of *business scenarios* into a non-production target under an approved policy:

  catalog        reservable datasets (a root business object plus its dependency closure). Found by scanning the
                 target, or created by provisioning.
  provisioning   `subset`    : real, masked data copied from the source (same planner / conflict analysis / masking /
                               executor / reconciliation gate as every other refresh)
                 `synthetic` : consistent generated data, no source involved
                 `catalog`   : an existing AVAILABLE dataset is reserved instead of creating data
  governance     a policy (targets, templates, quotas, masking rules, TTLs) is approved once by an approver (separation of
                 duties); requests inside it run without further approval, requests above the object quota need an approver;
                 AI agents can request data but never approve
  lifecycle      exclusive reservations with TTL, usage history, consume/restore, golden datasets (verified snapshot that can
                 be restored), expiry sweep, retire, and purge (approver only, only objects this platform created, never
                 shared objects still used by other datasets)
Integration: REST API for CI/CD pipelines (service accounts). SAP Cloud ALM / test-management connectors are NOT implemented;
test cases are linked by free-form external reference.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..dependency.planner import Plan, PlanInstance, Planner
from ..dependency.registry import CONFIG_TYPES
from ..delta.engine import merge_plans
from ..masking.engine import MaskingEngine, MaskingPolicy, Rule, MaskMode, discover_sensitive, template as mask_template
from ..reconcile.validator import reconcile
from ..sap.adapter import ReadOnlyView, SapSystem
from ..sap.ddic import TABLES
from ..sap.synthetic import SimulatedSap
from ..security.auth import Forbidden, Principal, check_separation_of_duties
from ..selective.conflicts import Action, ConflictReport, analyze
from ..selective.executor import row_hash
from ..selective.manifest import Manifest, Scope
from ..service import Conflict, NotFound
from .. import provision
from ..provision import ProvisionError
from .templates import PLANNED, TEMPLATES, describe, find_candidates

SVC = "svc.tdm"
LIVE = {"AVAILABLE", "RESERVED", "CONSUMED"}
HANDLE_KEYS = {"SALES_ORDER": "sales_order", "DELIVERY": "deliveries", "BILLING": "billing_documents",
               "FI_DOCUMENT": "accounting_documents", "CUSTOMER": "customer", "PURCHASE_ORDER": "purchase_order",
               "VENDOR": "vendor", "MATERIAL": "materials"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


@dataclass
class Policy:
    id: str
    name: str
    source_id: str
    target_id: str
    allowed_templates: list[str]
    max_objects: int
    max_active_reservations: int
    max_ttl_days: int
    retention_days: int
    allow_subset: bool
    allow_synthetic: bool
    masking_rules: list[dict]
    created_by: str
    mask_key: bytes = field(default_factory=lambda: os.urandom(32))  # stable so one person masks identically in every dataset
    last_editor: str | None = None
    submitted_by: str | None = None
    status: str = "DRAFT"  # DRAFT PENDING_APPROVAL ACTIVE SUSPENDED
    approval: dict | None = None
    version: int = 1

    def hash(self) -> str:
        body = {"src": self.source_id, "tgt": self.target_id, "tpl": sorted(self.allowed_templates), "max": self.max_objects,
                "res": self.max_active_reservations, "ttl": self.max_ttl_days, "ret": self.retention_days,
                "sub": self.allow_subset, "syn": self.allow_synthetic,
                "rules": sorted((r["table"], r["field"], r["strategy"]) for r in self.masking_rules)}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "source_id": self.source_id, "target_id": self.target_id,
                "allowed_templates": self.allowed_templates, "max_objects_per_request": self.max_objects,
                "max_active_reservations_per_user": self.max_active_reservations, "max_ttl_days": self.max_ttl_days,
                "retention_days": self.retention_days, "allow_subset": self.allow_subset, "allow_synthetic": self.allow_synthetic,
                "masking_rules": self.masking_rules, "status": self.status, "approval": self.approval, "version": self.version,
                "hash": self.hash(), "created_by": self.created_by}


@dataclass
class Dataset:
    id: str
    name: str
    template_id: str
    process: str
    target_id: str
    provenance: str  # subset | synthetic | discovered
    root: str
    objects: list[str]
    owned: list[str]
    handles: dict
    attrs: dict
    created_by: str
    created: str
    expires_at: datetime | None
    baseline: dict[str, str]  # "TABLE/key" -> row hash as provisioned (drift reference)
    owned_rows: dict[str, list[tuple[str, tuple]]]
    request_id: str | None = None
    source_id: str | None = None
    state: str = "AVAILABLE"  # AVAILABLE RESERVED CONSUMED EXPIRED RETIRED PURGED
    reserved_by: str | None = None
    reserved_until: datetime | None = None
    reservation_reason: str | None = None
    test_cases: list[dict] = field(default_factory=list)
    usage: list[dict] = field(default_factory=list)
    golden: bool = False
    snapshot: dict | None = None
    history: list[dict] = field(default_factory=list)

    def event(self, actor: str, what: str, **kw):
        self.history.append({"ts": _iso(_now()), "actor": actor, "event": what, **kw})

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "template_id": self.template_id, "process": self.process, "target_id": self.target_id,
                "provenance": self.provenance, "synthetic": self.provenance == "synthetic", "root": self.root,
                "objects": len(self.objects), "owned_objects": len(self.owned), "handles": self.handles, "attrs": self.attrs,
                "state": self.state, "reserved_by": self.reserved_by, "reserved_until": _iso(self.reserved_until),
                "reservation_reason": self.reservation_reason, "golden": self.golden, "expires_at": _iso(self.expires_at),
                "test_cases": self.test_cases, "usage": self.usage, "history": self.history[-20:], "created_by": self.created_by,
                "created": self.created, "request_id": self.request_id, "simulated": True}


class TdmService:
    def __init__(self, svc):
        self.svc = svc
        self.policies: dict[str, Policy] = {}
        self.datasets: dict[str, Dataset] = {}
        self.requests: dict[str, dict] = {}

    # ------------------------------------------------------------ templates
    def templates(self) -> dict:
        return {"available": [describe(t) for t in TEMPLATES.values()], "planned": PLANNED}

    def candidates(self, source_id: str, template_id: str, params: dict) -> list[dict]:
        tpl = self._tpl(template_id)
        return find_candidates(tpl, self.svc.source_view(source_id), params)

    @staticmethod
    def _tpl(tid: str):
        if tid not in TEMPLATES:
            planned = next((p for p in PLANNED if p["id"] == tid), None)
            raise Conflict(f"template '{tid}' is not available" + (f": {planned['reason']}" if planned else ""))
        return TEMPLATES[tid]

    # ------------------------------------------------------------ policies
    def get_policy(self, pid: str) -> Policy:
        if pid not in self.policies:
            raise NotFound(f"TDM policy {pid}")
        return self.policies[pid]

    def active_policy(self, target_id: str) -> Policy | None:
        return next((p for p in self.policies.values() if p.target_id == target_id and p.status == "ACTIVE"), None)

    def create_policy(self, actor: Principal, spec: dict) -> Policy:
        svc = self.svc
        src, tgt = svc.system(spec["source_id"]), svc.system(spec["target_id"])
        from ..security import authz
        authz.require_systems(svc, actor, src.id, tgt.id)
        if tgt.is_production or not tgt.can_be_write_target:
            raise Forbidden(f"{tgt.label} cannot host self-service test data")
        if src.id == tgt.id or src.family != tgt.family:
            raise Conflict("source and target must be different systems of the same product family")
        tpls = spec.get("allowed_templates") or list(TEMPLATES)
        for t in tpls:
            self._tpl(t)
        rules = [dict(r) for r in spec.get("masking_rules", [])]
        if spec.get("auto_masking", True):  # discover over the whole source so approvers see every rule they approve
            rows = {t: svc.source_view(src.id).select(t) for t, td in TABLES.items() if not td.config}
            have = {(r.table, r.field) for r in mask_template("gdpr-standard").rules} | {(r["table"], r["field"]) for r in rules}
            rules += [{"table": d["table"], "field": d["field"], "strategy": d["strategy"], "category": d["category"]}
                      for d in discover_sensitive(rows) if (d["table"], d["field"]) not in have]
        p = Policy(f"tdp-{uuid.uuid4().hex[:8]}", spec["name"], src.id, tgt.id, tpls, int(spec.get("max_objects", 10)),
                   int(spec.get("max_active_reservations", 5)), int(spec.get("max_ttl_days", 14)), int(spec.get("retention_days", 30)),
                   bool(spec.get("allow_subset", True)), bool(spec.get("allow_synthetic", True)), rules, actor.id)
        p.last_editor = actor.id
        self._check_policy(p)
        self.policies[p.id] = p
        svc.audit.append(actor.id, "tdm.policy.created", p.id, {"hash": p.hash(), "extra_masking_rules": len(rules)})
        return p

    @staticmethod
    def _check_policy(p: Policy) -> None:
        pol = MaskingPolicy("x", "x", [Rule(r["table"], r["field"], r["strategy"], r.get("category", r["strategy"].lower())) for r in p.masking_rules])
        errs = pol.validate()
        if errs:
            raise Conflict("; ".join(errs))
        if min(p.max_objects, p.max_active_reservations, p.max_ttl_days, p.retention_days) < 1:
            raise Conflict("quotas must be >= 1")

    def submit_policy(self, actor: Principal, pid: str) -> Policy:
        p = self.get_policy(pid)
        if p.status != "DRAFT":
            raise Conflict(f"policy is {p.status}")
        p.status, p.submitted_by = "PENDING_APPROVAL", actor.id
        self.svc.audit.append(actor.id, "tdm.policy.submitted", p.id, {"hash": p.hash()})
        return p

    def approve_policy(self, actor: Principal, pid: str) -> Policy:
        if not actor.can("plan:approve"):
            raise Forbidden("plan:approve required")
        p = self.get_policy(pid)
        if p.status != "PENDING_APPROVAL":
            raise Conflict(f"policy is {p.status}, not PENDING_APPROVAL")
        for creator in {p.created_by, p.last_editor, p.submitted_by} - {None}:
            check_separation_of_duties(creator, actor)
        for o in self.policies.values():  # one active policy per target
            if o.target_id == p.target_id and o.status == "ACTIVE":
                o.status = "SUSPENDED"
        p.status, p.approval = "ACTIVE", {"by": actor.id, "at": _iso(_now()), "hash": p.hash(), "version": p.version}
        self.svc.audit.append(actor.id, "tdm.policy.approved", p.id, p.approval)
        return p

    def suspend_policy(self, actor: Principal, pid: str) -> Policy:
        if not actor.can("plan:approve") or actor.kind == "agent":
            raise Forbidden("plan:approve (human) required")
        p = self.get_policy(pid)
        p.status = "SUSPENDED"
        self.svc.audit.append(actor.id, "tdm.policy.suspended", p.id, {})
        return p

    def _mask_policy(self, p: Policy) -> MaskingPolicy:
        pol = mask_template("gdpr-standard")
        have = {(r.table, r.field) for r in pol.rules}
        for r in p.masking_rules:
            if (r["table"], r["field"]) not in have:
                pol.rules.append(Rule(r["table"], r["field"], r["strategy"], r.get("category", r["strategy"].lower()), MaskMode.PSEUDONYMIZE))
        return pol

    # ------------------------------------------------------------ requests
    def request(self, actor: Principal, spec: dict, now: datetime | None = None) -> dict:
        if not actor.can("tdm:request"):
            raise Forbidden("tdm:request required")
        from ..security import authz
        authz.require_systems(self.svc, actor, spec.get("target_id"))
        if authz.restricted(actor):
            authz.require_companies(actor, [str((spec.get("params") or {}).get("company_code", ""))] if (spec.get("params") or {}).get("company_code") else [], "the request's company_code")
        now = now or _now()
        req = {"id": f"req-{uuid.uuid4().hex[:8]}", "requester": actor.id, "created": _iso(now), "status": "SUBMITTED",
               "spec": {k: spec.get(k) for k in ("target_id", "template_id", "mode", "count", "params", "purpose", "test_cases", "ttl_days", "reserve")},
               "notes": [], "datasets": [], "reasons": [], "approval": None}
        sp = req["spec"]
        sp["mode"], sp["count"] = sp["mode"] or "auto", int(sp["count"] or 1)
        sp["params"], sp["test_cases"] = sp["params"] or {}, sp["test_cases"] or []
        sp["reserve"] = True if sp["reserve"] is None else bool(sp["reserve"])
        self.requests[req["id"]] = req
        self.svc.audit.append(actor.id, "tdm.request.submitted", req["id"], {"template": sp["template_id"], "count": sp["count"], "mode": sp["mode"]})
        pol = self.active_policy(sp["target_id"]) if sp["target_id"] in self.svc.systems else None
        reasons = []
        if sp["target_id"] not in self.svc.systems:
            reasons.append("unknown target system")
        elif pol is None:
            reasons.append("no approved TDM policy is active for this target")
        if not reasons:
            try:
                tpl = self._tpl(sp["template_id"])
                if sp["template_id"] not in pol.allowed_templates:
                    reasons.append(f"template {sp['template_id']} is not allowed by the policy")
                if sp["mode"] not in ("auto", "catalog", "subset", "synthetic"):
                    reasons.append(f"unknown mode {sp['mode']}")
                if sp["mode"] == "subset" and not (pol.allow_subset and "subset" in tpl.modes):
                    reasons.append("subset provisioning is not available for this template/policy")
                if sp["mode"] == "synthetic" and not (pol.allow_synthetic and "synthetic" in tpl.modes):
                    reasons.append("synthetic generation is not available for this template/policy")
            except Conflict as e:
                reasons.append(str(e))
            ttl = int(sp["ttl_days"] or min(7, pol.max_ttl_days))
            sp["ttl_days"] = ttl
            if ttl > pol.max_ttl_days or ttl < 1:
                reasons.append(f"ttl_days must be between 1 and {pol.max_ttl_days}")
            if sp["count"] < 1:
                reasons.append("count must be >= 1")
            if sp["reserve"] and not reasons:
                active = sum(1 for d in self.datasets.values() if d.reserved_by == actor.id and d.state == "RESERVED" and d.target_id == pol.target_id)
                if active + sp["count"] > pol.max_active_reservations:
                    reasons.append(f"reservation quota exceeded ({active} active + {sp['count']} requested > {pol.max_active_reservations})")
        if reasons:
            req["status"], req["reasons"] = "REJECTED", reasons
            self.svc.audit.append(SVC, "tdm.request.rejected", req["id"], {"reasons": reasons})
            return req
        if sp["count"] > pol.max_objects:
            req["status"] = "PENDING_APPROVAL"
            req["notes"].append(f"{sp['count']} objects exceeds the self-service limit of {pol.max_objects}: an approver must approve")
            return req
        req["approval"] = {"by": f"policy:{pol.id}", "hash": pol.hash()}
        return self._fulfil(req, pol, now)

    def approve_request(self, actor: Principal, rid: str, now: datetime | None = None) -> dict:
        if not actor.can("plan:approve"):
            raise Forbidden("plan:approve required")
        req = self.get_request(rid)
        if req["status"] != "PENDING_APPROVAL":
            raise Conflict(f"request is {req['status']}")
        if actor.id == req["requester"] or actor.kind == "agent":
            raise Forbidden("separation of duties: the requester cannot approve their own request; agents cannot approve")
        pol = self.active_policy(req["spec"]["target_id"])
        if pol is None:
            raise Conflict("policy no longer active")
        req["approval"] = {"by": actor.id, "at": _iso(_now())}
        self.svc.audit.append(actor.id, "tdm.request.approved", rid, {})
        return self._fulfil(req, pol, now or _now())

    def reject_request(self, actor: Principal, rid: str, reason: str) -> dict:
        if not actor.can("plan:approve"):
            raise Forbidden("plan:approve required")
        req = self.get_request(rid)
        if req["status"] != "PENDING_APPROVAL":
            raise Conflict(f"request is {req['status']}")
        req["status"], req["reasons"] = "REJECTED", [reason or "rejected by approver"]
        self.svc.audit.append(actor.id, "tdm.request.rejected", rid, {"by": actor.id, "reason": reason})
        return req

    def get_request(self, rid: str) -> dict:
        if rid not in self.requests:
            raise NotFound(f"request {rid}")
        return self.requests[rid]

    # ------------------------------------------------------------ fulfilment
    def _fulfil(self, req: dict, pol: Policy, now: datetime) -> dict:
        sp, tpl = req["spec"], TEMPLATES[req["spec"]["template_id"]]
        remaining, mode = sp["count"], sp["mode"]
        req["status"] = "PROVISIONING"
        got: list[tuple[str, str]] = []
        if mode in ("auto", "catalog"):
            for d in self._match(pol.target_id, tpl, sp["params"], remaining):
                got.append((d.id, "catalog")); remaining -= 1
            if got:
                req["notes"].append(f"{len(got)} matching dataset(s) already in the catalog")
        if remaining and mode in ("auto", "subset") and pol.allow_subset and "subset" in tpl.modes:
            try:
                new = self._provision(req, pol, tpl, remaining, "subset", now)
                got += [(d, "subset") for d in new]; remaining -= len(new)
            except ProvisionError as e:
                req["notes"].append(f"subset provisioning not possible: {'; '.join(e.reasons)[:300]}")
        if remaining and mode in ("auto", "synthetic") and pol.allow_synthetic and "synthetic" in tpl.modes:
            try:
                new = self._provision(req, pol, tpl, remaining, "synthetic", now)
                got += [(d, "synthetic") for d in new]; remaining -= len(new)
            except ProvisionError as e:
                req["notes"].append(f"synthetic generation not possible: {'; '.join(e.reasons)[:300]}")
        if sp["reserve"]:
            for did, _ in got:
                self._reserve(self.datasets[did], req["requester"], now + timedelta(days=sp["ttl_days"]), sp.get("purpose") or "", sp["test_cases"])
        elif sp["test_cases"]:
            for did, _ in got:
                self.datasets[did].test_cases += [tc for tc in sp["test_cases"] if tc not in self.datasets[did].test_cases]
        req["datasets"] = [{"id": d, "source": s, "handles": self.datasets[d].handles} for d, s in got]
        req["status"] = "FULFILLED" if remaining == 0 else ("PARTIAL" if got else "FAILED")
        if remaining:
            req["notes"].append(f"{remaining} of {sp['count']} could not be provided")
        self.svc.audit.append(SVC, "tdm.request.completed", req["id"], {"status": req["status"], "datasets": [d for d, _ in got],
                                                                         "on_behalf_of": req["requester"]})
        return req

    def _match(self, target_id: str, tpl, params: dict, k: int) -> list[Dataset]:
        cc, cust = params.get("company_code", "1000"), params.get("customer")
        out = [d for d in sorted(self.datasets.values(), key=lambda d: d.created)
               if d.target_id == target_id and d.template_id == tpl.id and d.state == "AVAILABLE"
               and d.attrs.get("company_code") == cc and (not cust or d.attrs.get("customer") == cust)]
        return out[:k]

    # ------------------------------------------------------------ provisioning
    def _provision(self, req: dict, pol: Policy, tpl, n: int, kind: str, now: datetime) -> list[str]:
        svc = self.svc
        reg = svc.registries[svc.system(pol.target_id).family]
        params = req["spec"]["params"]
        manifest = Manifest(name=req["id"], source_system_id=pol.source_id, target_system_id=pol.target_id,
                            scope=Scope(object_type=tpl.root_type), conflict_policy={"DUPLICATE_DIFFERENT": "SKIP", "DUPLICATE_IDENTICAL": "SKIP"})
        if kind == "subset":
            taken = {d.root for d in self.datasets.values() if d.target_id == pol.target_id and d.state in LIVE}
            plan, per_root, attrs = provision.subset_plan(svc, tpl=tpl, n=n, params=params, source_id=pol.source_id, target_id=pol.target_id,
                                                          taken=taken, notes=req["notes"], label="tdm-" + req["id"])
            recon_source = svc.source_view(pol.source_id)
        else:
            plan, per_root, attrs, recon_source = provision.synthetic_plan(svc, tpl=tpl, n=n, params=params, target_id=pol.target_id)
        run, report = provision.execute_plan(svc, label=req["id"], plan=plan, manifest=manifest, reg=reg, target_id=pol.target_id,
                                             mask_policy=self._mask_policy(pol), mask_key=pol.mask_key, recon_source=recon_source, actor=SVC)
        req["run_id"] = run.id
        ids = []
        for root, closure in per_root.items():
            if report.decisions.get(root) not in (Action.LOAD, Action.UPDATE, Action.REPLACE) or root not in run.loaded:
                req["notes"].append(f"{root} was not loaded ({report.decisions.get(root, Action.SKIP).value})")
                continue
            ids.append(self._register(req, pol, tpl, kind, root, sorted(closure, key=plan.order.index), plan, run, attrs.get(root, {}), now))
        return ids

    def _register(self, req, pol, tpl, kind, root, closure, plan, run, attrs, now) -> str:
        owned = [i for i in closure if i in run.loaded]
        owned_rows, baseline = {}, {}
        for i in owned:
            ks = []
            for t, rows in run.staged[i].items():
                for r in rows:
                    key = tuple(r[k] for k in TABLES[t].keys)
                    ks.append((t, key))
                    baseline[f"{t}/" + "/".join(key)] = run.expected[f"{t}/" + "/".join(key)]
            owned_rows[i] = ks
        d = Dataset(f"ds-{uuid.uuid4().hex[:8]}", f"{tpl.name} · {root.split(':', 1)[1]}", tpl.id, tpl.process, pol.target_id, kind, root,
                    closure, owned, self._handles(plan, closure), attrs, req["requester"], _iso(now),
                    now + timedelta(days=pol.retention_days), baseline, owned_rows, req["id"], pol.source_id if kind == "subset" else None)
        d.event(SVC, "provisioned", mode=kind, request=req["id"], objects=len(closure), owned=len(owned))
        self.datasets[d.id] = d
        self.svc.audit.append(SVC, "tdm.dataset.provisioned", d.id, {"template": tpl.id, "mode": kind, "root": root, "owned": len(owned)})
        return d.id

    @staticmethod
    def _handles(plan, closure: list[str]) -> dict:
        h: dict = {}
        for i in closure:
            inst = plan.instances.get(i)
            if inst is None or inst.type not in HANDLE_KEYS:
                continue
            k = HANDLE_KEYS[inst.type]
            if k in ("customer", "sales_order", "purchase_order", "vendor"):
                h.setdefault(k, inst.key)
            else:
                h.setdefault(k, []).append(inst.key)
        return h

    # ------------------------------------------------------------ catalog
    def get(self, did: str) -> Dataset:
        if did not in self.datasets:
            raise NotFound(f"dataset {did}")
        return self.datasets[did]

    def catalog(self, **f) -> list[dict]:
        out = []
        q = (f.get("q") or "").lower()
        for d in sorted(self.datasets.values(), key=lambda d: d.created, reverse=True):
            if f.get("target_id") and d.target_id != f["target_id"]:
                continue
            if f.get("template_id") and d.template_id != f["template_id"]:
                continue
            if f.get("state") and d.state != f["state"]:
                continue
            if not f.get("state") and not f.get("include_inactive") and d.state in ("EXPIRED", "RETIRED", "PURGED"):
                continue
            if f.get("company_code") and d.attrs.get("company_code") != f["company_code"]:
                continue
            if f.get("provenance") and d.provenance != f["provenance"]:
                continue
            if f.get("test_case") and not any(tc.get("id") == f["test_case"] for tc in d.test_cases):
                continue
            if f.get("reserved_by") and d.reserved_by != f["reserved_by"]:
                continue
            if q and q not in json.dumps([d.name, d.handles, d.attrs], default=str).lower():
                continue
            out.append(d.public())
        return out

    def scan(self, actor: Principal, target_id: str, company_code: str = "1000", limit: int = 25, now: datetime | None = None) -> dict:
        """Catalog what already exists in the target (e.g. from selective/delta refreshes). Tester-owned objects are skipped."""
        if not actor.can("tdm:curate"):
            raise Forbidden("tdm:curate required")
        svc, now = self.svc, now or _now()
        from ..security import authz
        authz.require_systems(svc, actor, target_id)
        tgt = svc.adapters[svc.system(target_id).id]
        reg = svc.registries[svc.system(target_id).family]
        view = ReadOnlyView(tgt)
        pol = self.active_policy(target_id)
        retention = pol.retention_days if pol else 30
        taken = {d.root for d in self.datasets.values() if d.target_id == target_id and d.state in LIVE}
        added, skipped_owned = [], 0
        for tpl in TEMPLATES.values():
            hdr = reg.types[tpl.root_type].header
            n = 0
            for c in find_candidates(tpl, view, {"company_code": company_code}):
                root = f"{tpl.root_type}:{c['key']}"
                if root in taken or n >= limit:
                    continue
                if tgt.owner_of(hdr, c["key"]):
                    skipped_owned += 1
                    continue
                m = Manifest(name="scan", source_system_id=target_id, target_system_id=target_id,
                             scope=Scope(object_type=tpl.root_type, explicit_keys=[c["key"]]), include_downstream=list(tpl.include_downstream))
                pl = Planner(view, reg).build(m)
                if pl.blocking:
                    continue
                baseline = {}
                for i in pl.instances.values():
                    for t, rows in i.rows.items():
                        for r in rows:
                            baseline[f"{t}/" + "/".join(str(r[k]) for k in TABLES[t].keys)] = row_hash(r)
                d = Dataset(f"ds-{uuid.uuid4().hex[:8]}", f"{tpl.name} · {c['key']}", tpl.id, tpl.process, target_id, "discovered", root,
                            list(pl.order), [], self._handles(pl, pl.order), c["attrs"], actor.id, _iso(now),
                            now + timedelta(days=retention), baseline, {})
                d.event(actor.id, "discovered")
                self.datasets[d.id] = d; taken.add(root); n += 1; added.append(d.id)
        svc.audit.append(actor.id, "tdm.catalog.scanned", target_id, {"added": len(added), "skipped_tester_owned": skipped_owned})
        return {"added": len(added), "datasets": added, "skipped_tester_owned": skipped_owned}

    # ------------------------------------------------------------ reservations / usage
    def _reserve(self, d: Dataset, user: str, until: datetime, reason: str, test_cases: list[dict]) -> None:
        d.state, d.reserved_by, d.reserved_until, d.reservation_reason = "RESERVED", user, until, reason
        d.test_cases += [tc for tc in test_cases if tc not in d.test_cases]
        d.event(user, "reserved", until=_iso(until), reason=reason)
        self.svc.audit.append(user, "tdm.dataset.reserved", d.id, {"until": _iso(until), "reason": reason})

    def reserve(self, actor: Principal, did: str, ttl_days: int = 7, reason: str = "", test_cases: list[dict] | None = None,
                now: datetime | None = None) -> Dataset:
        if not actor.can("tdm:request"):
            raise Forbidden("tdm:request required")
        d, now = self.get(did), now or _now()
        pol = self.active_policy(d.target_id)
        if pol is None:
            raise Conflict("no active policy for the dataset's target")
        if d.state != "AVAILABLE":
            raise Conflict(f"dataset is {d.state}" + (f" (reserved by {d.reserved_by})" if d.reserved_by else ""))
        if not 1 <= ttl_days <= pol.max_ttl_days:
            raise Conflict(f"ttl_days must be between 1 and {pol.max_ttl_days}")
        active = sum(1 for x in self.datasets.values() if x.reserved_by == actor.id and x.state == "RESERVED" and x.target_id == d.target_id)
        if active + 1 > pol.max_active_reservations:
            raise Conflict(f"reservation quota reached ({pol.max_active_reservations})")
        self._reserve(d, actor.id, now + timedelta(days=ttl_days), reason, test_cases or [])
        return d

    def release(self, actor: Principal, did: str, consumed: bool = False) -> Dataset:
        d = self.get(did)
        if d.state != "RESERVED":
            raise Conflict(f"dataset is {d.state}, not RESERVED")
        if d.reserved_by != actor.id and not actor.can("tdm:curate"):
            raise Forbidden("only the reserving user or a curator can release")
        d.state = "CONSUMED" if consumed else "AVAILABLE"
        d.event(actor.id, "released", consumed=consumed, was_reserved_by=d.reserved_by)
        d.reserved_by = d.reserved_until = d.reservation_reason = None
        self.svc.audit.append(actor.id, "tdm.dataset.released", d.id, {"consumed": consumed})
        return d

    def record_usage(self, actor: Principal, did: str, test_case: dict, outcome: str, consumed: bool = False,
                     note: str = "") -> Dataset:
        if not actor.can("tdm:request"):
            raise Forbidden("tdm:request required")
        d = self.get(did)
        if d.state != "RESERVED" or (d.reserved_by != actor.id and not actor.can("tdm:curate")):
            raise Conflict("usage can only be recorded by the user holding the reservation")
        if outcome not in ("passed", "failed", "blocked"):
            raise Conflict("outcome must be passed, failed or blocked")
        d.usage.append({"ts": _iso(_now()), "user": actor.id, "test_case": test_case, "outcome": outcome, "consumed": consumed, "note": note})
        if test_case not in d.test_cases:
            d.test_cases.append(test_case)
        if consumed:
            d.state = "CONSUMED"
            d.reserved_by = d.reserved_until = d.reservation_reason = None
        d.event(actor.id, "usage", outcome=outcome, test_case=test_case.get("id"), consumed=consumed)
        self.svc.audit.append(actor.id, "tdm.dataset.usage", d.id, {"test_case": test_case.get("id"), "outcome": outcome, "consumed": consumed})
        return d

    def link_test_case(self, actor: Principal, did: str, tc: dict, unlink: bool = False) -> Dataset:
        if not actor.can("tdm:request"):
            raise Forbidden("tdm:request required")
        d = self.get(did)
        if unlink:
            d.test_cases = [x for x in d.test_cases if x.get("id") != tc.get("id")]
        elif tc.get("id") and tc not in d.test_cases:
            d.test_cases.append(tc)
        d.event(actor.id, "unlinked" if unlink else "linked", test_case=tc.get("id"))
        return d

    # ------------------------------------------------------------ integrity / golden / lifecycle
    def verify(self, did: str) -> dict:
        d = self.get(did)
        tgt = self.svc.adapters[d.target_id]
        drift = []
        for k, h in d.baseline.items():
            t, *key = k.split("/")
            r = tgt.get(t, tuple(key))
            if r is None:
                drift.append({"row": k, "problem": "missing"})
            elif row_hash(r) != h:
                drift.append({"row": k, "problem": "changed"})
        return {"dataset": d.id, "intact": not drift, "rows_checked": len(d.baseline), "drift": drift[:50], "drift_count": len(drift)}

    def promote_golden(self, actor: Principal, did: str) -> Dataset:
        if not actor.can("tdm:curate"):
            raise Forbidden("tdm:curate required")
        d = self.get(did)
        if d.provenance == "discovered":
            raise Conflict("only datasets this platform provisioned can be golden (it must own the rows it restores)")
        if d.state not in ("AVAILABLE", "RESERVED"):
            raise Conflict(f"dataset is {d.state}")
        v = self.verify(did)
        if not v["intact"]:
            raise Conflict(f"dataset has drifted ({v['drift_count']} rows); cannot snapshot a modified dataset")
        tgt = self.svc.adapters[d.target_id]
        d.snapshot = {i: {} for i in d.owned}
        for i, keys in d.owned_rows.items():
            for t, key in keys:
                d.snapshot[i].setdefault(t, []).append(copy.deepcopy(tgt.get(t, key)))
        d.golden = True
        d.event(actor.id, "promoted_golden", rows=len(d.baseline))
        self.svc.audit.append(actor.id, "tdm.dataset.golden", d.id, {"rows": len(d.baseline)})
        return d

    def restore(self, actor: Principal, did: str) -> Dataset:
        """Return a golden dataset to its verified snapshot (REPLACE of owned objects, undo-logged and gated)."""
        if not actor.can("tdm:curate"):
            raise Forbidden("tdm:curate required")
        d = self.get(did)
        if not d.golden or not d.snapshot:
            raise Conflict("dataset is not golden")
        if d.state == "RESERVED" and d.reserved_by != actor.id:
            raise Conflict(f"dataset is reserved by {d.reserved_by}")
        svc = self.svc
        tgt, reg = svc.adapters[d.target_id], svc.registries[svc.system(d.target_id).family]
        order = [i for i in d.owned]
        inst = {}
        for i in order:
            t, key = i.split(":", 1)
            inst[i] = PlanInstance(i, t, key, "ROOT", None, copy.deepcopy(d.snapshot[i]))
        plan = Plan("tdm-restore", inst, order, {}, [], [])
        report = ConflictReport([], {i: Action.REPLACE for i in order}, {}, set())
        run = svc.executor.new_run(f"restore-{d.id}", plan, report)
        run.staged = copy.deepcopy(d.snapshot)
        run.expected = {f"{t}/" + "/".join(str(r[k]) for k in TABLES[t].keys): row_hash(r)
                        for rows in d.snapshot.values() for t, rs in rows.items() for r in rs}
        run.step("EXTRACT_MASK_STAGE")["status"] = "DONE"
        svc.executor.reg = reg
        svc.runs[run.id] = run
        eng = MaskingEngine(MaskingPolicy("none", "none", []))
        svc.executor.execute(run, plan, report, eng, tgt, actor.id)
        if run.status != "COMPLETED":
            svc.executor.rollback(run, tgt, actor.id)
            raise Conflict(f"restore failed and was rolled back: {run.error}")
        v = self.verify(did)
        if not v["intact"]:
            svc.executor.rollback(run, tgt, actor.id)
            raise Conflict(f"restore did not reproduce the snapshot ({v['drift_count']} rows differ); rolled back")
        d.state = "RESERVED" if d.reserved_by else "AVAILABLE"
        d.event(actor.id, "restored", rows=len(d.baseline))
        svc.audit.append(actor.id, "tdm.dataset.restored", d.id, {"rows": len(d.baseline)})
        return d

    def retire(self, actor: Principal, did: str) -> Dataset:
        if not actor.can("tdm:curate"):
            raise Forbidden("tdm:curate required")
        d = self.get(did)
        if d.state == "RESERVED":
            raise Conflict(f"dataset is reserved by {d.reserved_by}")
        d.state = "RETIRED"
        d.event(actor.id, "retired")
        self.svc.audit.append(actor.id, "tdm.dataset.retired", d.id, {})
        return d

    def sweep(self, actor: Principal, now: datetime | None = None) -> dict:
        """Lifecycle job (external scheduler): release expired reservations, expire datasets past retention."""
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        now, released, expired = now or _now(), [], []
        for d in self.datasets.values():
            if d.state == "RESERVED" and d.reserved_until and d.reserved_until < now:
                d.event(actor.id, "reservation_expired", was=d.reserved_by)
                d.state, d.reserved_by, d.reserved_until, d.reservation_reason = "AVAILABLE", None, None, None
                released.append(d.id)
            if d.state in ("AVAILABLE", "CONSUMED") and d.expires_at and d.expires_at < now:
                d.state = "EXPIRED"; d.event(actor.id, "expired"); expired.append(d.id)
        self.svc.audit.append(actor.id, "tdm.sweep", "tdm", {"released": len(released), "expired": len(expired)})
        return {"reservations_released": released, "datasets_expired": expired}

    def purge(self, actor: Principal, did: str, force: bool = False) -> dict:
        """Delete the target rows this platform created for an expired/retired dataset. Approver only; human only."""
        if not actor.can("plan:approve") or actor.kind != "human":
            raise Forbidden("purge needs a human approver (plan:approve)")
        d = self.get(did)
        if d.state not in ("EXPIRED", "RETIRED"):
            raise Conflict(f"only EXPIRED or RETIRED datasets can be purged (dataset is {d.state})")
        if d.provenance == "discovered" or not d.owned:
            raise Conflict("this dataset was not created by the platform: nothing it owns can be purged")
        shared = {i for o in self.datasets.values() if o.id != d.id and o.state in LIVE and o.target_id == d.target_id for i in o.objects}
        victims = [i for i in d.owned if i not in shared]
        kept = [i for i in d.owned if i in shared]
        tgt = self.svc.adapters[d.target_id]
        tgt.assert_writable()
        drift = []
        for i in victims:
            for t, key in d.owned_rows[i]:
                r = tgt.get(t, key)
                if r is not None and row_hash(r) != d.baseline.get(f"{t}/" + "/".join(key)):
                    drift.append(f"{t}/{'/'.join(key)}")
        if drift and not force:
            raise Conflict(f"{len(drift)} row(s) were changed since provisioning (e.g. {drift[0]}); review or purge with force")
        n = 0
        for i in reversed(victims):
            for t, key in reversed(d.owned_rows[i]):
                if tgt.get(t, key) is not None:
                    tgt.delete(t, key); n += 1
        d.state = "PURGED"
        d.event(actor.id, "purged", rows=n, kept_shared=len(kept), forced=bool(drift and force))
        self.svc.audit.append(actor.id, "tdm.dataset.purged", d.id, {"rows_deleted": n, "objects_kept_shared": kept, "forced": bool(drift and force)})
        return {"dataset": d.id, "rows_deleted": n, "objects_deleted": len(victims), "objects_kept_because_shared": kept}
