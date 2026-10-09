"""Lean client builder (Module 4).

Creates a NEW client in an existing non-production system from a reusable, approved *environment template*:

  1 plan & estimate  read-only: what a full client copy would move vs what this lean client moves (rows, bytes, duration model)
  2 shell            client entry in the host system; created only after planning succeeded
  3 customizing      configuration-only baseline (company codes, plants, number-range intervals reset) plus whatever customizing
                     the planned data depends on (pulled in automatically, reported)
  4 data             masked master data and selective transactional slices through the shared plan/conflict/mask/load/gate pipeline
  5 synthetic        generated scenario data (training clients), planned against the freshly loaded client
  6 validate         client-wide integrity, number-range protection, residual-PII scan, outbound safety, footprint
  7 finalize         protection (no overwrite), retention

Any failure removes the half-built client again (controlled rollback). Honest limits: user master/authorizations,
client-independent customizing and the repository are not modelled; nothing here calls SAP client-copy programs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import provision
from ..agents import advisors
from ..dependency.planner import Planner
from ..delta.engine import merge_plans
from ..masking.engine import MaskMode, MaskingPolicy, Rule, discover_sensitive, template as mask_template
from ..provision import ProvisionError
from ..sap.adapter import ReadOnlyView, SapSystem
from ..sap.ddic import NUMBER_RANGE_OBJECTS, TABLES
from ..sap.synthetic import SimulatedSap
from ..security.auth import Forbidden, Principal, check_separation_of_duties
from ..selective.manifest import Manifest, Scope
from ..service import Conflict, NotFound
from ..tdm.templates import TEMPLATES
from .profiles import PROFILES, PURPOSES, RESERVED_CLIENTS, WORKFLOWS

SVC = "svc.leanclient"
MASTER_TYPES = ("CUSTOMER", "VENDOR", "MATERIAL")
MAX_OBJECTS_PER_SLICE = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _size(rows: dict[str, list[dict]]) -> tuple[int, int]:
    n = b = 0
    for rs in rows.values():
        for r in rs:
            n += 1
            b += len(json.dumps(r, separators=(",", ":"), default=str))
    return n, b


@dataclass
class EnvTemplate:
    id: str
    name: str
    purpose: str
    source_id: str
    company_codes: list[str]
    masters: list[dict]
    transactions: list[dict]
    masking_policy_id: str
    masking_rules: list[dict]
    max_rows: int
    retention_days: int
    protect_after_build: bool
    created_by: str
    mask_key: bytes = field(default_factory=lambda: os.urandom(32))
    last_editor: str | None = None
    submitted_by: str | None = None
    status: str = "DRAFT"  # DRAFT PENDING_APPROVAL APPROVED
    approval: dict | None = None
    version: int = 1

    def hash(self) -> str:
        body = {"p": self.purpose, "src": self.source_id, "cc": sorted(self.company_codes), "m": self.masters, "t": self.transactions,
                "mask": self.masking_policy_id, "rules": sorted((r["table"], r["field"], r["strategy"]) for r in self.masking_rules),
                "max": self.max_rows, "ret": self.retention_days, "prot": self.protect_after_build}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "purpose": self.purpose, "role": PURPOSES[self.purpose], "source_id": self.source_id,
                "company_codes": self.company_codes, "masters": self.masters, "transactions": self.transactions,
                "masking_policy_id": self.masking_policy_id, "masking_rules": self.masking_rules, "max_rows": self.max_rows,
                "retention_days": self.retention_days, "protect_after_build": self.protect_after_build, "status": self.status,
                "approval": self.approval, "version": self.version, "hash": self.hash(), "created_by": self.created_by}


@dataclass
class Build:
    id: str
    template_id: str
    source_id: str
    host_id: str
    client: str
    name: str
    logical_system: str
    requested_by: str
    created: str
    status: str = "PLANNING"  # PLANNING BUILDING READY FAILED BLOCKED EXPIRED DECOMMISSIONED
    system_id: str | None = None
    phases: list[dict] = field(default_factory=list)
    estimate: dict | None = None
    checks: list[dict] = field(default_factory=list)
    actual: dict | None = None
    reasons: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    runs: list[str] = field(default_factory=list)

    def phase(self, name: str) -> dict:
        ph = {"name": name, "status": "RUNNING", "started": _iso(_now()), "finished": None, "detail": {}}
        self.phases.append(ph)
        return ph

    def public(self) -> dict:
        return {"id": self.id, "template_id": self.template_id, "source_id": self.source_id, "host_id": self.host_id,
                "system_id": self.system_id, "client": self.client, "name": self.name, "logical_system": self.logical_system,
                "status": self.status, "phases": self.phases, "estimate": self.estimate, "checks": self.checks, "actual": self.actual,
                "reasons": self.reasons, "expires_at": _iso(self.expires_at), "requested_by": self.requested_by,
                "created": self.created, "runs": self.runs, "simulated": True}


class LeanClientService:
    def __init__(self, svc):
        self.svc = svc
        self.templates: dict[str, EnvTemplate] = {}
        self.builds: dict[str, Build] = {}

    # ------------------------------------------------------------ catalogs
    @staticmethod
    def profiles() -> dict:
        return {"profiles": PROFILES, "workflows": WORKFLOWS, "reserved_clients": sorted(RESERVED_CLIENTS)}

    # ------------------------------------------------------------ templates
    def get_template(self, tid: str) -> EnvTemplate:
        if tid not in self.templates:
            raise NotFound(f"environment template {tid}")
        return self.templates[tid]

    def create_template(self, actor: Principal, spec: dict) -> EnvTemplate:
        svc = self.svc
        src = svc.system(spec["source_id"])
        purpose = spec.get("purpose", "sandbox")
        if purpose not in PURPOSES:
            raise Conflict(f"purpose must be one of {sorted(PURPOSES)}")
        ccs = list((spec.get("customizing") or {}).get("company_codes") or spec.get("company_codes") or [])
        if not ccs:
            raise Conflict("at least one company code is required for the configuration baseline")
        known = {c["BUKRS"] for c in svc.source_view(src.id).select("T001")}
        if set(ccs) - known:
            raise Conflict(f"unknown company code(s) in source: {sorted(set(ccs) - known)}")
        masters = [dict(m) for m in spec.get("masters", [])]
        for m in masters:
            if m.get("type") not in MASTER_TYPES or int(m.get("max", 0)) < 1:
                raise Conflict(f"master spec needs type in {MASTER_TYPES} and max >= 1")
        trans = [dict(t) for t in spec.get("transactions", [])]
        for t in trans:
            tpl = TEMPLATES.get(t.get("template_id"))
            if tpl is None:
                raise Conflict(f"scenario template '{t.get('template_id')}' is not available")
            if t.get("mode") not in tpl.modes:
                raise Conflict(f"mode {t.get('mode')} is not supported by {tpl.id} (supports {list(tpl.modes)})")
            if not 1 <= int(t.get("count", 0)) <= MAX_OBJECTS_PER_SLICE:
                raise Conflict(f"count must be between 1 and {MAX_OBJECTS_PER_SLICE}")
            if tpl.root_type in MASTER_TYPES:
                raise Conflict("master data belongs in 'masters', not 'transactions'")
        if any(t["mode"] == "synthetic" for t in trans) and not any(m["type"] == "MATERIAL" for m in masters):
            raise Conflict("synthetic scenarios need materials in the client: add a MATERIAL master spec")
        if spec.get("masking_policy_id", "gdpr-standard") not in ("gdpr-standard", "gdpr-strict"):
            raise Conflict("unknown masking policy")
        if int(spec.get("retention_days", 30)) < 1 or int(spec.get("max_rows", 1000)) < 1:
            raise Conflict("retention_days and max_rows must be >= 1")
        rules = [dict(r) for r in spec.get("masking_rules", [])]
        if spec.get("auto_masking", True):  # show the approver every rule they approve
            rows = {t: svc.source_view(src.id).select(t) for t, td in TABLES.items() if not td.config}
            have = {(r.table, r.field) for r in mask_template(spec.get("masking_policy_id", "gdpr-standard")).rules} | {(r["table"], r["field"]) for r in rules}
            rules += [{"table": d["table"], "field": d["field"], "strategy": d["strategy"], "category": d["category"]}
                      for d in discover_sensitive(rows) if (d["table"], d["field"]) not in have]
        t = EnvTemplate(f"env-{uuid.uuid4().hex[:8]}", spec["name"], purpose, src.id, ccs, masters, trans, spec.get("masking_policy_id", "gdpr-standard"),
                        rules, int(spec.get("max_rows", 1000)), int(spec.get("retention_days", 30)),
                        bool(spec.get("protect_after_build", purpose in ("training", "regression"))), actor.id)
        t.last_editor = actor.id
        errs = self._mask_policy(t).validate()
        if errs:
            raise Conflict("; ".join(errs))
        self.templates[t.id] = t
        svc.audit.append(actor.id, "lean.template.created", t.id, {"hash": t.hash(), "purpose": purpose})
        return t

    def submit_template(self, actor: Principal, tid: str) -> EnvTemplate:
        t = self.get_template(tid)
        if t.status != "DRAFT":
            raise Conflict(f"template is {t.status}")
        t.status, t.submitted_by = "PENDING_APPROVAL", actor.id
        self.svc.audit.append(actor.id, "lean.template.submitted", t.id, {"hash": t.hash()})
        return t

    def approve_template(self, actor: Principal, tid: str) -> EnvTemplate:
        if not actor.can("plan:approve"):
            raise Forbidden("plan:approve required")
        t = self.get_template(tid)
        if t.status != "PENDING_APPROVAL":
            raise Conflict(f"template is {t.status}, not PENDING_APPROVAL")
        for creator in {t.created_by, t.last_editor, t.submitted_by} - {None}:
            check_separation_of_duties(creator, actor)
        t.status, t.approval = "APPROVED", {"by": actor.id, "at": _iso(_now()), "hash": t.hash(), "version": t.version}
        self.svc.audit.append(actor.id, "lean.template.approved", t.id, t.approval)
        return t

    def _mask_policy(self, t: EnvTemplate) -> MaskingPolicy:
        pol = mask_template(t.masking_policy_id)
        mode = pol.rules[0].mode
        have = {(r.table, r.field) for r in pol.rules}
        for r in t.masking_rules:
            if (r["table"], r["field"]) not in have:
                pol.rules.append(Rule(r["table"], r["field"], r["strategy"], r.get("category", r["strategy"].lower()), mode))
        return pol

    # ------------------------------------------------------------ planning / estimate
    def _scratch(self, host: SapSystem, source_id: str, client: str = "999") -> SimulatedSap:
        sysd = SapSystem(id="scratch", sid=host.sid, client=client, role="SBX", product=host.product, release=host.release,
                         db_type=host.db_type, owner="scratch")
        s = SimulatedSap(sysd, {t: [] for t in TABLES}, outbound=[])
        s.ref_date = self.svc.adapters[source_id].reference_date()
        return s

    def _plan(self, t: EnvTemplate, host: SapSystem) -> dict:
        """Everything that does not need the new client to exist. Raises ProvisionError with reasons when the template cannot be built."""
        svc = self.svc
        src = svc.source_view(t.source_id)
        reg = svc.registries[host.family]
        scratch = self._scratch(host, t.source_id)
        planner, plans, notes = Planner(src, reg), [], []
        cc = t.company_codes
        plants = [p["WERKS"] for p in src.select("T001W") if p["BUKRS"] in cc]
        per_spec = []
        for m in t.masters:
            org = {"plants": plants} if m["type"] == "MATERIAL" else {"company_codes": cc}
            mf = Manifest(name="lean", source_system_id=t.source_id, target_system_id=host.id, scope=Scope(object_type=m["type"], **org))
            issues: list = []
            keys = sorted(planner.select_roots(mf, issues))[: int(m["max"])]
            if not keys:
                raise ProvisionError([f"no {m['type']} master data in source for company code(s) {cc}"])
            pl = planner.build(mf.model_copy(update={"scope": Scope(object_type=m["type"], explicit_keys=keys, **org)}))
            if pl.blocking:
                raise ProvisionError([i.message for i in pl.blocking][:5])
            plans.append(pl); per_spec.append({"kind": "master", "type": m["type"], "objects": len(keys)})
        taken = {i for pl in plans for i in pl.instances}
        for tr in t.transactions:
            if tr["mode"] != "subset":
                continue
            tpl = TEMPLATES[tr["template_id"]]
            params = {"company_code": tr.get("company_code", cc[0]), "days": tr.get("days")}
            pl, per_root, _ = provision.subset_plan(svc, tpl=tpl, n=int(tr["count"]), params=params, source_id=t.source_id, target_id=host.id,
                                                    taken=taken, notes=notes, check_target_config=False, label="lean",
                                                    target_reader=scratch, family=host.family)
            if len(per_root) < int(tr["count"]):
                notes.append(f"{tr['template_id']}: only {len(per_root)} of {tr['count']} matching scenarios exist in the source")
            plans.append(pl); taken |= set(pl.instances); per_spec.append({"kind": "subset", "template": tr["template_id"], "objects": len(per_root)})
        plan_a = merge_plans(plans, planner, "lean-" + t.id) if plans else None
        # customizing: baseline + whatever the planned data depends on (a plant brings its company code)
        cc_set, plant_set = set(cc), set(plants)
        pulled: list[str] = []
        if plan_a:
            for code in plan_a.config_refs.get("COMPANY_CODE", set()) - cc_set:
                cc_set.add(code); pulled.append(f"COMPANY_CODE:{code}")
            for pl_ in plan_a.config_refs.get("PLANT", set()) - plant_set:
                plant_set.add(pl_); pulled.append(f"PLANT:{pl_}")
        for p in src.select("T001W"):
            if p["WERKS"] in plant_set and p["BUKRS"] not in cc_set:
                cc_set.add(p["BUKRS"]); pulled.append(f"COMPANY_CODE:{p['BUKRS']}")
        custom = {"T001": [dict(r) for r in src.select("T001") if r["BUKRS"] in cc_set],
                  "T001W": [dict(r) for r in src.select("T001W") if r["WERKS"] in plant_set],
                  "NRIV": [{**r, "NRLEVEL": r["FROMNUMBER"]} for r in src.select("NRIV")]}  # fresh client: numbering starts at the interval start
        # synthetic slices are estimated against a scratch client that already holds customizing + planned master data
        for tbl, rows in custom.items():
            scratch.data[tbl] += [dict(r) for r in rows]
        synth_rows: dict[str, list[dict]] = {}
        if plan_a:
            for tbl, rows in provision.plan_rows(plan_a).items():
                scratch.data[tbl] += [dict(r) for r in rows]
        for tr in t.transactions:
            if tr["mode"] != "synthetic":
                continue
            tpl = TEMPLATES[tr["template_id"]]
            sp, per_root, _, _ = provision.synthetic_plan(svc, tpl=tpl, n=int(tr["count"]), params={"company_code": tr.get("company_code", cc[0])},
                                                          target_id=host.id, reader=scratch)
            for tbl, rows in provision.plan_rows(sp).items():
                scratch.data[tbl] += [dict(r) for r in rows]; synth_rows.setdefault(tbl, []).extend(rows)
            per_spec.append({"kind": "synthetic", "template": tr["template_id"], "objects": len(per_root)})
        scratch._idx.clear()
        return {"plan_a": plan_a, "custom": custom, "pulled": pulled, "synth_rows": synth_rows, "notes": notes, "specs": per_spec}

    def _estimate(self, t: EnvTemplate, host: SapSystem, p: dict) -> dict:
        svc = self.svc
        tables: dict[str, int] = {}
        data_rows, data_bytes = _size(provision.plan_rows(p["plan_a"])) if p["plan_a"] else (0, 0)
        if p["plan_a"]:
            for tbl, n in p["plan_a"].table_rows().items():
                tables[tbl] = tables.get(tbl, 0) + n
        c_rows, c_bytes = _size(p["custom"])
        s_rows, s_bytes = _size(p["synth_rows"])
        for src_rows in (p["custom"], p["synth_rows"]):
            for tbl, rs in src_rows.items():
                tables[tbl] = tables.get(tbl, 0) + len(rs)
        full = {tbl: svc.adapters[t.source_id].select(tbl) for tbl in TABLES}
        f_rows, f_bytes = _size(full)
        rows, byt = data_rows + c_rows + s_rows, data_bytes + c_bytes + s_bytes
        pct = lambda a, b: round(100 * (1 - a / b), 1) if b else 0.0
        return {"template": t.id, "source": svc.system(t.source_id).label, "host": host.label,
                "lean": {"rows": rows, "bytes": byt, "tables": dict(sorted(tables.items())), "customizing_rows": c_rows,
                         "master_and_subset_rows": data_rows, "synthetic_rows": s_rows, "specs": p["specs"]},
                "full_client_copy": {"rows": f_rows, "bytes": f_bytes, "tables": {k: len(v) for k, v in full.items() if v}},
                "savings": {"rows_pct": pct(rows, f_rows), "bytes_pct": pct(byt, f_bytes)},
                "duration": {"lean": advisors.estimate_duration(bytes_total=byt, rows_total=rows),
                             "full_client_copy": advisors.estimate_duration(bytes_total=f_bytes, rows_total=f_rows)},
                "customizing_pulled_in_by_data": p["pulled"], "notes": p["notes"], "within_max_rows": rows <= t.max_rows, "max_rows": t.max_rows,
                "workflows": WORKFLOWS,
                "caveats": ["Duration uses placeholder throughput figures, not benchmarks.",
                            "Sizes are JSON-encoded row sizes, not database page sizes.",
                            "User master / authorizations, client-independent customizing and the repository are not modelled."]}

    def estimate(self, actor: Principal, tid: str, host_id: str) -> dict:
        t, host = self.get_template(tid), self.svc.system(host_id)
        self._check_host(t, host)
        try:
            p = self._plan(t, host)
        except ProvisionError as e:
            raise Conflict("template cannot be built: " + "; ".join(e.reasons))
        est = self._estimate(t, host, p)
        self.svc.audit.append(actor.id, "lean.estimated", t.id, {"rows": est["lean"]["rows"], "savings_pct": est["savings"]["rows_pct"]})
        return est

    # ------------------------------------------------------------ build
    def _check_host(self, t: EnvTemplate, host: SapSystem) -> None:
        src = self.svc.system(t.source_id)
        if host.is_production:
            raise Forbidden("new clients are never created in a production system")
        if src.family != host.family:
            raise Conflict("ECC↔S/4HANA is a migration, not a lean client build")

    def get_build(self, bid: str) -> Build:
        if bid not in self.builds:
            raise NotFound(f"build {bid}")
        return self.builds[bid]

    def build(self, actor: Principal, spec: dict, fault_injector=None, now: datetime | None = None) -> Build:
        if not actor.can("client:build"):
            raise Forbidden("client:build required")
        svc, now = self.svc, now or _now()
        t = self.get_template(spec["template_id"])
        if t.status != "APPROVED" or not t.approval or t.approval["hash"] != t.hash():
            raise Conflict(f"template is {t.status}; it must be approved for its current definition")
        host = svc.system(spec["host_id"])
        self._check_host(t, host)
        client = str(spec["client"])
        if not re.fullmatch(r"\d{3}", client):
            raise Conflict("client must be a 3-digit number")
        if client in RESERVED_CLIENTS:
            raise Conflict(f"client {client} is a SAP delivery client and is never created or overwritten here")
        if any(s.sid == host.sid and s.client == client for s in svc.systems.values()):
            raise Conflict(f"client {client} already exists in {host.sid}")
        if any(b.host_id == host.id and b.client == client and b.status in ("PLANNING", "BUILDING") for b in self.builds.values()):
            raise Conflict(f"a build for {host.sid}/{client} is already running")
        b = Build(f"bld-{uuid.uuid4().hex[:8]}", t.id, t.source_id, host.id, client, spec.get("name") or f"{t.purpose} client {client}",
                  spec.get("logical_system") or f"{host.sid}CLNT{client}", actor.id, _iso(now))
        self.builds[b.id] = b
        svc.audit.append(actor.id, "lean.build.requested", b.id, {"template": t.id, "host": host.label, "client": client})
        try:
            ph = b.phase("PLAN")
            p = self._plan(t, host)
            b.estimate = self._estimate(t, host, p)
            ph.update(status="DONE", finished=_iso(_now()), detail={"rows": b.estimate["lean"]["rows"], "savings_pct": b.estimate["savings"]["rows_pct"]})
            if not b.estimate["within_max_rows"]:
                raise ProvisionError([f"planned {b.estimate['lean']['rows']} rows exceed the template limit of {t.max_rows}"])
        except ProvisionError as e:
            b.phases[-1].update(status="BLOCKED", finished=_iso(_now()))
            b.status, b.reasons = "BLOCKED", e.reasons
            svc.audit.append(SVC, "lean.build.blocked", b.id, {"reasons": e.reasons[:5]})
            return b
        b.status = "BUILDING"
        try:
            self._shell(b, t, host, actor)
            self._customizing(b, p)
            self._data(b, t, host, p, fault_injector)
            self._synthetic(b, t, host)
            self._validate(b, t, host)
            failed = [c for c in b.checks if c["status"] == "fail"]
            if failed:
                raise ProvisionError([f"readiness check failed: {c['id']}: {c['detail']}" for c in failed])
            self._finalize(b, t, now)
        except ProvisionError as e:
            self._abort(b, e.reasons)
        except Exception as e:  # unexpected: still leave nothing behind
            self._abort(b, [f"{type(e).__name__}: {e}"])
        return b

    def _shell(self, b: Build, t: EnvTemplate, host: SapSystem, actor: Principal) -> None:
        ph = b.phase("SHELL")
        s = SapSystem(sid=host.sid, client=b.client, role=PURPOSES[t.purpose], product=host.product, release=host.release, db_type=host.db_type,
                      db_version=host.db_version, os=host.os, app_servers=list(host.app_servers), owner=b.requested_by,
                      writable_target_allowed=True, tags=["lean-client", t.id, t.purpose, f"logical:{b.logical_system}"])
        adapter = SimulatedSap(s, {tbl: [] for tbl in TABLES}, outbound=[{"name": "EDI_PARTNER_OUT", "type": "IDoc", "active": False},
                                                                       {"name": "MAIL_RELAY", "type": "SMTP", "active": False}])
        adapter.ref_date = self.svc.adapters[b.source_id].reference_date()
        self.svc.register_system(actor, s, adapter)
        b.system_id = s.id
        ph.update(status="DONE", finished=_iso(_now()), detail={"client": b.client, "role": s.role.value, "logical_system": b.logical_system})

    def _customizing(self, b: Build, p: dict) -> None:
        ph = b.phase("CUSTOMIZING")
        tgt = self.svc.adapters[b.system_id]
        for tbl, rows in p["custom"].items():
            tgt.upsert(tbl, rows)
        ph.update(status="DONE", finished=_iso(_now()), detail={**{k: len(v) for k, v in p["custom"].items()}, "pulled_in_by_data": p["pulled"]})

    def _data(self, b: Build, t: EnvTemplate, host: SapSystem, p: dict, fault_injector) -> None:
        if not p["plan_a"]:
            return
        svc = self.svc
        ph = b.phase("DATA")
        reg = svc.registries[host.family]
        manifest = Manifest(name=b.id, source_system_id=b.source_id, target_system_id=b.system_id, scope=Scope(object_type="CUSTOMER"))
        run, report = provision.execute_plan(svc, label=b.id, plan=p["plan_a"], manifest=manifest, reg=reg, target_id=b.system_id,
                                             mask_policy=self._mask_policy(t), mask_key=t.mask_key, recon_source=svc.source_view(b.source_id),
                                             actor=SVC, fault_injector=fault_injector)
        b.runs.append(run.id)
        ph.update(status="DONE", finished=_iso(_now()), detail={"objects_loaded": len(run.loaded), "quarantined": len(run.quarantined),
                                                                 "checks_passed": sum(1 for c in run.reconciliation["checks"] if c["status"] == "pass"),
                                                                 "run": run.id})

    def _synthetic(self, b: Build, t: EnvTemplate, host: SapSystem) -> None:
        specs = [tr for tr in t.transactions if tr["mode"] == "synthetic"]
        if not specs:
            return
        svc = self.svc
        ph = b.phase("SYNTHETIC")
        reg = svc.registries[host.family]
        loaded = 0
        for tr in specs:
            tpl = TEMPLATES[tr["template_id"]]
            params = {"company_code": tr.get("company_code", t.company_codes[0])}
            plan, per_root, _, recon = provision.synthetic_plan(svc, tpl=tpl, n=int(tr["count"]), params=params, target_id=b.system_id)
            manifest = Manifest(name=b.id, source_system_id=b.source_id, target_system_id=b.system_id, scope=Scope(object_type=tpl.root_type))
            run, report = provision.execute_plan(svc, label=b.id, plan=plan, manifest=manifest, reg=reg, target_id=b.system_id,
                                                 mask_policy=self._mask_policy(t), mask_key=t.mask_key, recon_source=recon, actor=SVC)
            b.runs.append(run.id)
            loaded += len(per_root)
        ph.update(status="DONE", finished=_iso(_now()), detail={"synthetic_roots": loaded})

    def _validate(self, b: Build, t: EnvTemplate, host: SapSystem) -> None:
        svc = self.svc
        ph = b.phase("VALIDATE")
        tgt, src, reg = svc.adapters[b.system_id], svc.adapters[b.source_id], svc.registries[host.family]
        checks = []

        def chk(cid, name, ok, detail="", samples=None):
            checks.append({"id": cid, "name": name, "status": "pass" if ok else "fail", "detail": detail, "samples": (samples or [])[:6]})

        chk("L1-ROLE", "Client is registered as a non-production system", not svc.system(b.system_id).is_production,
            f"{svc.system(b.system_id).label}")
        integ = Planner(ReadOnlyView(tgt), reg).validate_relationships()
        chk("L2-INTEGRITY", "Client-wide relational integrity (no orphan items, no dangling business references)",
            integ["orphans"] == 0 and integ["dangling"] == 0, f"{integ['orphans']} orphans, {integ['dangling']} dangling",
            [str(x) for x in integ["orphan_samples"] + integ["dangling_samples"]])
        cfg_missing = []
        for tbl, ref in (("VBAK", [("BUKRS_VF", "T001")]), ("VBRK", [("BUKRS", "T001")]), ("EKKO", [("BUKRS", "T001")])):
            for r in tgt.select(tbl):
                for fld, ct in ref:
                    if tgt.get(ct, (r[fld],)) is None:
                        cfg_missing.append(f"{tbl}.{fld}={r[fld]}")
        for tbl, fld in (("VBAP", "WERKS"), ("EKPO", "WERKS"), ("MARC", "WERKS")):
            for r in tgt.select(tbl):
                if tgt.get("T001W", (r[fld],)) is None:
                    cfg_missing.append(f"{tbl}.{fld}={r[fld]}")
        chk("L3-CUSTOMIZING", "Customizing present for everything the data references (company codes, plants)", not cfg_missing,
            f"{len(cfg_missing)} references without customizing", sorted(set(cfg_missing)))
        nr_bad = []
        for tbl, (obj, fld) in NUMBER_RANGE_OBJECTS.items():
            ks = [int(r[fld]) for r in tgt.select(tbl)]
            lv = tgt.number_level(obj)
            if lv is None:
                nr_bad.append(f"{obj}: no interval")
            elif ks and lv < max(ks):
                nr_bad.append(f"{obj}: level {lv} < max document {max(ks)}")
        chk("L4-NUMBER-RANGES", "Number ranges exist and protect every document in the client", not nr_bad, f"{len(nr_bad)} problems", nr_bad)
        pol = self._mask_policy(t)
        leak = []
        for (tbl, fld) in pol.fields():
            originals = {r[fld] for r in src.select(tbl) if isinstance(r.get(fld), str) and r.get(fld)}
            leak += [f"{tbl}.{fld}" for r in tgt.select(tbl) if r.get(fld) in originals]
        chk("L5-NO-SOURCE-PII", "No original sensitive value of the source exists anywhere in the client", not leak,
            f"{len(leak)} original values found", sorted(set(leak)))
        active = [o["name"] for o in tgt.outbound_interfaces() if o.get("active")]
        chk("L6-OUTBOUND", "All outbound interfaces of the new client are inactive", not active, f"{len(active)} active", active)
        counts = {tbl: len(rows) for tbl, rows in tgt.data.items() if rows}
        total = sum(counts.values())
        chk("L7-FOOTPRINT", "Footprint within the template limit", total <= t.max_rows, f"{total} rows (limit {t.max_rows})")
        b.checks = checks
        b.actual = {"rows": total, "tables": counts, "planned_rows": b.estimate["lean"]["rows"],
                    "bytes": _size({k: v for k, v in tgt.data.items()})[1]}
        ph.update(status="DONE" if all(c["status"] == "pass" for c in checks) else "FAILED", finished=_iso(_now()),
                  detail={"passed": sum(1 for c in checks if c["status"] == "pass"), "failed": sum(1 for c in checks if c["status"] == "fail")})

    def _finalize(self, b: Build, t: EnvTemplate, now: datetime) -> None:
        ph = b.phase("FINALIZE")
        s = self.svc.system(b.system_id)
        s.writable_target_allowed = not t.protect_after_build
        b.expires_at = now + timedelta(days=t.retention_days)
        b.status = "READY"
        ph.update(status="DONE", finished=_iso(_now()), detail={"protected_against_overwrite": t.protect_after_build, "expires_at": _iso(b.expires_at)})
        self.svc.audit.append(SVC, "lean.build.ready", b.id, {"system": s.label, "rows": b.actual["rows"], "savings_pct": b.estimate["savings"]["rows_pct"]})

    def _abort(self, b: Build, reasons: list[str]) -> None:
        """Controlled rollback of a failed build: the half-built client is removed again."""
        svc = self.svc
        for ph in b.phases:
            if ph["status"] == "RUNNING":
                ph.update(status="FAILED", finished=_iso(_now()))
        removed = None
        if b.system_id:
            removed = svc.systems.pop(b.system_id, None)
            svc.adapters.pop(b.system_id, None)
        b.status, b.reasons = "FAILED", reasons
        b.reasons.append("the partially built client was removed" if removed else "no client had been created")
        svc.audit.append(SVC, "lean.build.failed", b.id, {"reasons": reasons[:5], "client_removed": bool(removed)})
        if removed:
            b.system_id = None

    # ------------------------------------------------------------ lifecycle
    def clients(self) -> list[dict]:
        out = []
        for b in self.builds.values():
            if b.status in ("READY", "EXPIRED") and b.system_id in self.svc.systems:
                s = self.svc.system(b.system_id)
                out.append({**b.public(), "label": s.label, "locked": not s.writable_target_allowed})
        return out

    def set_protection(self, actor: Principal, system_id: str, locked: bool) -> dict:
        if not actor.can("client:build"):
            raise Forbidden("client:build required")
        b = next((x for x in self.builds.values() if x.system_id == system_id and x.status in ("READY", "EXPIRED")), None)
        if b is None:
            raise Conflict("not a client built by this platform")
        if b.status == "EXPIRED" and not locked:
            raise Conflict("expired clients stay locked: decommission or rebuild them")
        self.svc.system(system_id).writable_target_allowed = not locked
        self.svc.audit.append(actor.id, "lean.protection", system_id, {"locked": locked})
        return {"system_id": system_id, "locked": locked}

    def sweep(self, actor: Principal, now: datetime | None = None) -> dict:
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        now, expired = now or _now(), []
        for b in self.builds.values():
            if b.status == "READY" and b.expires_at and b.expires_at < now and b.system_id in self.svc.systems:
                b.status = "EXPIRED"
                self.svc.system(b.system_id).writable_target_allowed = False
                expired.append(b.id)
        self.svc.audit.append(actor.id, "lean.sweep", "lean", {"expired": expired})
        return {"expired": expired}

    def decommission(self, actor: Principal, bid: str) -> dict:
        """Remove a lean-built client and everything in it. Human approver only; blocked while anything still uses it."""
        if not actor.can("plan:approve") or actor.kind != "human":
            raise Forbidden("decommissioning needs a human approver (plan:approve)")
        svc, b = self.svc, self.get_build(bid)
        if b.status not in ("READY", "EXPIRED") or b.system_id not in svc.systems:
            raise Conflict(f"build is {b.status}: nothing to decommission")
        sid = b.system_id
        uses = [f"project {p.id}" for p in svc.projects.values() if sid in (p.source_id, p.target_id)]
        uses += [f"delta scenario {d.id}" for d in svc.delta.scenarios.values() if sid in (d.source_id, d.target_id)]
        uses += [f"TDM policy {p.id}" for p in svc.tdm.policies.values() if sid in (p.source_id, p.target_id) and p.status in ("ACTIVE", "PENDING_APPROVAL")]
        uses += [f"TDM dataset {d.id}" for d in svc.tdm.datasets.values() if d.target_id == sid and d.state in ("AVAILABLE", "RESERVED", "CONSUMED")]
        if uses:
            raise Conflict("client is still referenced: " + ", ".join(uses[:6]))
        label = svc.system(sid).label
        svc.systems.pop(sid); svc.adapters.pop(sid)
        b.status = "DECOMMISSIONED"
        svc.audit.append(actor.id, "lean.client.decommissioned", b.id, {"client": label})
        return {"build": b.id, "removed": label}
