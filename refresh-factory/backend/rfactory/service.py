"""Application service: projects, workflow state machine, governance gates and report/evidence generation."""
from __future__ import annotations

import time

import copy
import io
import json
import os
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .agents import advisors
from .dependency.planner import Plan, Planner
from .dependency.registry import Registry
from .fullrefresh import runbook
from .masking.engine import MaskingEngine, MaskingPolicy, Rule, MaskMode, STRATEGIES, TEMPLATES, discover_sensitive, template
from .reconcile.validator import reconcile
from .sap.adapter import ProductionWriteBlocked, ReadOnlyView, SapSystem, SystemRole
from .sap.synthetic import SimulatedSap, make_demo_pair
from .security.audit import AuditLog, load_audit_key
from .security import authz
from .security.auth import Forbidden, Principal, check_separation_of_duties
from .selective.conflicts import Action, ConflictReport, analyze
from .selective.executor import Executor, Run
from .selective.manifest import Manifest, Scope


def family_is_s4(system) -> bool:
    return system.family == "S4"


class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    """Workflow precondition not met (HTTP 409)."""


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Project:
    id: str
    name: str
    source_id: str
    target_id: str
    created_by: str
    status: str = "DRAFT"  # DRAFT PLANNED CONFLICTS_ANALYZED PENDING_APPROVAL APPROVED RUNNING COMPLETED HELD FAILED ROLLED_BACK
    manifests: list[Manifest] = field(default_factory=list)
    plan: Plan | None = None
    report: ConflictReport | None = None
    masking_policy: MaskingPolicy | None = None
    approval: dict | None = None
    submitted_by: str | None = None
    last_editor: str | None = None
    runs: list[str] = field(default_factory=list)
    created: str = field(default_factory=_now)

    @property
    def manifest(self) -> Manifest | None:
        return self.manifests[-1] if self.manifests else None


class RefreshService:
    def __init__(self, data_dir: Path | None = None, persist: bool = False):
        self.data_dir = data_dir or Path(tempfile.mkdtemp(prefix="rfactory-"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(self.data_dir / "audit.jsonl", load_audit_key(self.data_dir))
        self.registries = {"ECC": Registry("ECC"), "S4": Registry("S4")}
        self.registry = self.registries["ECC"]  # default / ECC
        self.systems: dict[str, SapSystem] = {}
        self.adapters: dict[str, SimulatedSap] = {}
        self.projects: dict[str, Project] = {}
        self.runs: dict[str, Run] = {}
        self.executor = Executor(self.registry, self.data_dir / "staging", self.audit)
        self.engines: dict[str, MaskingEngine] = {}
        self.required_sensitive: dict[str, list[dict]] = {}
        from .delta.engine import DeltaService  # lazy: delta imports this module
        self.delta = DeltaService(self)
        from .tdm.engine import TdmService
        self.tdm = TdmService(self)
        from .leanclient.engine import LeanClientService
        self.lean = LeanClientService(self)
        from .postcopy.engine import PostCopyService
        self.postcopy = PostCopyService(self)
        from .agents.service import AgentService
        self.agents = AgentService(self)
        from .fullrefresh.engine import FullRefreshService
        self.full = FullRefreshService(self)
        from .orchestration.engine import OrchestrationService
        self.orch = OrchestrationService(self)
        from .benchmark.service import BenchmarkService
        self.bench = BenchmarkService(self)
        self.remote_profiles: dict = {}
        self.plan_extract_s: dict = {}
        self.write_requests: dict = {}
        from .security.revocation import RevocationList
        self.revocations = RevocationList()
        self.store = None
        if persist:
            from .persistence.store import StateStore
            self.store = StateStore(self, self.data_dir / "state.db")
            self.store.load()

    def checkpoint(self) -> dict | None:
        """Persist everything that changed (a no-op for an ephemeral service). The API calls this after every mutating request."""
        return self.store.save() if self.store else None

    # ---------------- landscape ----------------
    def is_local(self, sid: str) -> bool:
        """True for systems the platform fully models (simulated: data, technical state, writable). Remote systems are read-only sources."""
        a = self.adapters.get(sid)
        return a is not None and hasattr(a, "data") and hasattr(a, "tech")

    def require_local(self, sid: str, what: str) -> None:
        if not self.is_local(sid):
            raise Conflict(f"{what} is not available for the remote read-only system {self.system(sid).label}: it needs the platform to model or write the system")

    def register_system(self, actor: Principal, system: SapSystem, adapter: SimulatedSap | None = None) -> SapSystem:
        system.id = system.id or f"sys-{uuid.uuid4().hex[:8]}"
        if adapter is None:
            raise Conflict("pass an adapter: a SimulatedSap, or a remote read-only adapter via connect_remote()")
        adapter.system = system
        self.systems[system.id] = system
        self.adapters[system.id] = adapter
        self.audit.append(actor.id, "system.registered", system.id, {"label": system.label, "adapter": system.adapter})
        return system

    def connect_remote(self, actor: Principal, system: SapSystem, profile, transport=None, reference=None) -> SapSystem:
        """Register a REMOTE system as a read-only source. It can never be a refresh target and no write path to it exists."""
        from .sap.connectors.profile import ConnectionProfile, ProfileError
        from .sap.connectors.odata import ODataSourceAdapter
        from .sap.connectors.rfc import RemoteError, RfcSourceAdapter
        if actor.kind != "human" or not actor.can("system:write"):
            raise Forbidden("a human with system:write is required to connect a system")
        if not authz.system_ok(actor, system):
            raise Forbidden(f"outside your scope: you may not connect system {system.label}")
        try:
            profile.validate()
        except ProfileError as e:
            raise Conflict(str(e))
        system.id = system.id or f"sys-{uuid.uuid4().hex[:8]}"
        system.writable_target_allowed = False  # a remote source is never a write target
        system.adapter = profile.kind
        adapter = self._remote_adapter(system, profile, transport, reference)
        try:
            if profile.kind == "rfc":
                adapter._call("RFC_PING")
            else:
                adapter.ping()
            drift = adapter.schema_drift()
        except RemoteError as e:
            raise Conflict(f"connection check failed: {e}")
        self.systems[system.id], self.adapters[system.id] = system, adapter
        self.remote_profiles[system.id] = profile
        self.audit.append(actor.id, "system.connected", system.id, {"label": system.label, "kind": profile.kind, "profile": profile.public(), "schema_drift": {k: v[:3] for k, v in drift.items()}})
        return system

    def _remote_adapter(self, system: SapSystem, profile, transport=None, reference=None):
        """Builds the read-only adapter for a connection profile (real transport unless one is given)."""
        from .sap.connectors.odata import ODataSourceAdapter
        from .sap.connectors.rfc import RfcSourceAdapter
        if transport is None and os.environ.get("RFACTORY_ALLOW_FAKE_ENDPOINTS") == "1":
            host = (profile.ashost if profile.kind == "rfc" else profile.base_url.split("//")[-1].split(":")[0].split("/")[0]) or ""
            if host.endswith(".invalid"):  # demo and test hook: a host that cannot exist is served by a FAKE endpoint, never by the network
                from .sap.connectors.fake_odata import FakeODataTransport
                from .sap.connectors.fake_rfc import FakeRfcTransport
                from .sap.synthetic import make_demo_pair
                sim, tsim = make_demo_pair()
                if profile.kind == "rfc" and host.startswith("target."):  # a FAKE loader on a writable simulated sandbox (for tests of the write path)
                    from .sap.connectors.fake_loader import FakeLoaderTransport
                    transport, sim = FakeLoaderTransport(tsim, sid=system.sid, client=system.client), tsim
                else:
                    transport = FakeRfcTransport(sim) if profile.kind == "rfc" else FakeODataTransport(sim)
                reference = sim.reference_date
        if transport is None:
            try:
                if profile.kind == "rfc":
                    from .sap.connectors.rfc import PyRfcTransport
                    transport = PyRfcTransport(profile)
                else:
                    from .sap.connectors.odata import HttpODataTransport
                    transport = HttpODataTransport(profile)
            except ImportError:
                raise Conflict("pyrfc and the SAP NetWeaver RFC SDK are not installed: a real RFC connection is not possible here")
            except Exception as e:  # noqa: BLE001 - credentials, network
                raise Conflict(f"could not connect: {type(e).__name__}: {e}")
        kw = {"reference": reference} if reference else {}
        return RfcSourceAdapter(system, transport, profile, **kw) if profile.kind == "rfc" else ODataSourceAdapter(system, transport, profile, **kw)

    def smoke_remote(self, actor: Principal, system: SapSystem, profile, tables: list[str] | None = None, max_rows: int = 500, transport=None) -> dict:
        """Read-only first-contact test of a connection profile WITHOUT registering the system. The report holds no row values."""
        from .sap.connectors.profile import ProfileError
        from .sap.connectors.smoke import run_smoke
        if actor.kind != "human" or not actor.can("system:write"):
            raise Forbidden("a human with system:write is required to test a connection")
        if not authz.system_ok(actor, system):
            raise Forbidden(f"outside your scope: you may not test system {system.label}")
        try:
            profile.validate()
            if profile.password_ref:
                from .sap.connectors.profile import resolve_secret
                resolve_secret(profile.password_ref)  # fails with a clear message if the variable / file is missing; the value is never shown
        except ProfileError as e:
            raise Conflict(str(e))
        if not 1 <= max_rows <= 5000:
            raise Conflict("max_rows must be between 1 and 5000 for a smoke test")
        system.id = system.id or "smoke-" + uuid.uuid4().hex[:6]
        system.adapter = profile.kind
        adapter = self._remote_adapter(system, profile, transport)
        rep = run_smoke(adapter, tables=tables or None, max_rows=max_rows, probe_change_documents=bool(profile.options.get("change_documents")))
        levels = {lv: sum(1 for v in rep["verdict"] if v["level"] == lv) for lv in ("BLOCKER", "ATTENTION", "INFO")}
        self.audit.append(actor.id, "system.smoke", system.label, {"kind": profile.kind, "profile": profile.public(), "connected": rep["connection"].get("ok"),
                                                                    "verdict": levels, "max_rows": max_rows})
        return rep

    # ---------------- write access to a remote non-production system (two people) ----------------
    def _remote_rfc(self, sid: str):
        from .sap.connectors.rfc import RfcSourceAdapter
        s, a = self.system(sid), self.adapters.get(sid)
        if self.is_local(sid) or not isinstance(a, RfcSourceAdapter):
            raise Conflict("only a connected RFC system can be enabled as a write target")
        return s, a

    def request_remote_write(self, actor: Principal, sid: str, note: str = "", attest_outbound_inactive: bool = False) -> dict:
        """Step 1 of 2: a person with system:write asks to let the platform WRITE to a connected non-production system. Nothing is enabled yet."""
        from .sap.connectors.rfc_target import RfcTargetAdapter
        from .sap.connectors.rfc import RemoteError
        from .sap.adapter import ProductionWriteBlocked
        if actor.kind != "human" or not actor.can("system:write"):
            raise Forbidden("a human with system:write is required")
        authz.require_systems(self, actor, sid)
        s, a = self._remote_rfc(sid)
        if s.is_production:
            raise Forbidden(f"{s.label} is a production system: the platform never writes to production")
        if isinstance(a, RfcTargetAdapter):
            raise Conflict("this system is already enabled as a write target")
        probe = RfcTargetAdapter(s, a._t._inner, a.profile, sleep=a._sleep, reference=a._reference)
        try:
            info = probe.handshake()
        except (RemoteError, ProductionWriteBlocked) as e:
            raise Conflict(f"the loader on {s.label} is not ready: {e}")
        req = {"id": f"wr-{uuid.uuid4().hex[:8]}", "system_id": sid, "requested_by": actor.id, "at": datetime.now(timezone.utc).isoformat(), "note": note[:300],
               "outbound_inactive_attested": bool(attest_outbound_inactive), "handshake": {k: info[k] for k in ("version", "sid", "client", "category", "writes_enabled", "allowed_tables")},
               "status": "PENDING"}
        self.write_requests[sid] = req
        self.audit.append(actor.id, "target.write_requested", sid, {"request": req["id"], "attested": req["outbound_inactive_attested"], "allowed_tables": len(info["allowed_tables"])})
        return req

    def approve_remote_write(self, approver: Principal, sid: str) -> dict:
        """Step 2 of 2: a DIFFERENT person with target:approve enables writing. The platform's other gates (plan approval, separation of duties,
        reconciliation, release) still apply to every refresh."""
        from .sap.connectors.rfc_target import RfcTargetAdapter
        from .sap.connectors.rfc import RemoteError
        from .sap.adapter import ProductionWriteBlocked
        if approver.kind != "human" or not approver.can("target:approve"):
            raise Forbidden("a human with target:approve (security officer) is required")
        authz.require_systems(self, approver, sid)
        s, a = self._remote_rfc(sid)
        req = self.write_requests.get(sid)
        if not req or req["status"] != "PENDING":
            raise Conflict("there is no pending write request for this system")
        if approver.id == req["requested_by"]:
            raise Forbidden("separation of duties: the person who asked for write access cannot approve it")
        if s.is_production:
            raise Forbidden("production systems are never write targets")
        s.writable_target_allowed = True  # the adapter checks this on every write
        target = RfcTargetAdapter(s, a._t._inner, a.profile, sleep=a._sleep, reference=a._reference)
        try:
            info = target.handshake()  # re-checked now: the SAP side may have changed since the request
        except (RemoteError, ProductionWriteBlocked) as e:
            s.writable_target_allowed = False
            raise Conflict(f"the loader on {s.label} is no longer ready: {e}")
        a.profile.options["write"] = {"requested_by": req["requested_by"], "approved_by": approver.id, "approved_at": datetime.now(timezone.utc).isoformat(),
                                      "outbound_attested_by": approver.id if req["outbound_inactive_attested"] else None}
        self.adapters[sid] = target
        req["status"] = "APPROVED"
        req["approved_by"] = approver.id
        self.audit.append(approver.id, "target.write_approved", sid, {"request": req["id"], "requested_by": req["requested_by"], "outbound_attested": req["outbound_inactive_attested"],
                                                                      "tables": info["allowed_tables"]})
        return req

    def revoke_remote_write(self, actor: Principal, sid: str) -> dict:
        """Anyone responsible may switch writing off again at any time."""
        from .sap.connectors.rfc import RfcSourceAdapter
        from .sap.connectors.rfc_target import RfcTargetAdapter
        if actor.kind != "human" or not (actor.can("system:write") or actor.can("target:approve")):
            raise Forbidden("a human with system:write or target:approve is required")
        authz.require_systems(self, actor, sid)
        s, a = self._remote_rfc(sid)
        s.writable_target_allowed = False
        if isinstance(a, RfcTargetAdapter):
            self.adapters[sid] = RfcSourceAdapter(s, a._t._inner, a.profile, sleep=a._sleep, reference=a._reference)
        a.profile.options.pop("write", None)
        req = self.write_requests.pop(sid, None)
        self.audit.append(actor.id, "target.write_revoked", sid, {"request": (req or {}).get("id")})
        return {"system_id": sid, "writable": False}

    def change_doc_info(self, sid: str) -> dict:
        """What the change-document reader of a remote system covers, and what it did last."""
        a = self.adapters[sid]
        r = getattr(a, "cdr", None)
        if getattr(a, "kind", "") == "odata":
            return {"enabled": False, "note": "Not available through released OData APIs: every scoped object is compared by content."}
        if r is None:
            return {"enabled": False, "note": "Off: the delta engine compares every scoped object by content (set options.change_documents on the connection profile to read CDHDR)."}
        cov = r.coverage()
        from .sap.connectors.changedocs import HEADER_CLASS
        return {"enabled": True, "covered": sorted(cov), "not_logged": sorted(set(HEADER_CLASS) - set(cov)), "lag_seconds": r.lag, "overlap_seconds": r.overlap,
                "retention_days": r.retention, "last_read": r.last, "error": r.unreadable or getattr(a, "cd_error", None)}

    def rebuild_remote(self, system: SapSystem, profile):
        """Re-establish a remote connection after a restart; if that is impossible the system stays registered but disconnected."""
        from .sap.connectors.odata import HttpODataTransport, ODataSourceAdapter
        from .sap.connectors.rfc import DisconnectedAdapter, PyRfcTransport, RfcSourceAdapter
        try:
            if profile.kind == "odata":
                return ODataSourceAdapter(system, HttpODataTransport(profile), profile)
            if (profile.options.get("write") or {}).get("approved_by") and system.writable_target_allowed:
                from .sap.connectors.rfc_target import RfcTargetAdapter
                return RfcTargetAdapter(system, PyRfcTransport(profile), profile)  # approved earlier; the handshake runs again before every run
            return RfcSourceAdapter(system, PyRfcTransport(profile), profile)
        except Exception as e:  # noqa: BLE001 - no SDK, no network, no credentials
            return DisconnectedAdapter(system, profile, f"{type(e).__name__}: {e}"[:160])

    def bootstrap_demo(self, actor: Principal) -> dict:
        src, tgt = make_demo_pair()
        s = self.register_system(actor, src.system, src)
        t = self.register_system(actor, tgt.system, tgt)
        s4s, s4t = make_demo_pair("S4")
        a = self.register_system(actor, s4s.system, s4s)
        b = self.register_system(actor, s4t.system, s4t)
        return {"source": s.model_dump(mode="json"), "target": t.model_dump(mode="json"),
                "s4_source": a.model_dump(mode="json"), "s4_target": b.model_dump(mode="json"), "simulated": True}

    def reg(self, p: Project) -> Registry:
        r = self.registries[self.system(p.source_id).family]
        self.executor.reg = r
        return r

    def system(self, sid: str) -> SapSystem:
        if sid not in self.systems:
            raise NotFound(f"system {sid}")
        return self.systems[sid]

    def source_view(self, sid: str) -> ReadOnlyView:
        self.system(sid)
        return ReadOnlyView(self.adapters[sid])

    def discover(self, sid: str) -> dict:
        d = self.adapters[self.system(sid).id].discover()
        if not self.is_local(sid):
            d["business_objects"] = None
            d["note"] = "row counts need full scans and are not read from remote systems"
            return d
        cust = [r for r in self.adapters[sid].data["KNA1"]]
        d["business_objects"] = {
            "customers": len(cust), "vendors": self.adapters[sid].count("LFA1"), "materials": self.adapters[sid].count("MARA"),
            "sales_orders": self.adapters[sid].count("VBAK"), "deliveries": self.adapters[sid].count("LIKP"),
            "billing_documents": self.adapters[sid].count("VBRK"), "accounting_documents": self.adapters[sid].count("BKPF"),
            "purchase_orders": self.adapters[sid].count("EKKO"), "boms": self.adapters[sid].count("STKO"),
            "production_orders": sum(1 for o in self.adapters[sid].data["AUFK"] if str(o["AUART"]).startswith("PP")),
            "maintenance_orders": sum(1 for o in self.adapters[sid].data["AUFK"] if str(o["AUART"]).startswith("PM")),
            "material_documents": len({(r["MBLNR"], r["MJAHR"]) for r in self.adapters[sid].data["MATDOC" if family_is_s4(self.system(sid)) else "MKPF"]})}
        return d

    def readiness(self, sid: str) -> dict:
        s, a = self.system(sid), self.adapters[sid]
        if not self.is_local(sid):
            drift = getattr(a, "drift", {})
            return {"system": s.label, "ready": not drift, "simulated": False, "checks": [
                {"id": "R1", "name": "Remote read-only source (no write path exists)", "ok": True},
                {"id": "R2", "name": "Modelled DDIC fields and keys exist in the remote system", "ok": not drift, "detail": drift},
                {"id": "R3", "name": "Owner registered", "ok": bool(s.owner)}]}
        checks = [
            {"id": "R1", "name": "Connectivity (simulated adapter)", "ok": True},
            {"id": "R2", "name": "Read-only discovery", "ok": True},
            {"id": "R3", "name": "Owner registered", "ok": bool(s.owner)},
        ]
        if not s.is_production:
            checks += [
                {"id": "T1", "name": "Allowed as write target", "ok": s.can_be_write_target},
                {"id": "T2", "name": "Outbound interfaces inactive", "ok": not any(o.get("active") for o in a.outbound_interfaces())},
                {"id": "T3", "name": "Number range intervals present", "ok": a.number_level("SD_ORDER") is not None},
            ]
        else:
            checks.append({"id": "S1", "name": "Production: extraction only, writes structurally blocked", "ok": True})
        return {"system": s.label, "ready": all(c["ok"] for c in checks), "checks": checks, "simulated": True}

    def refresh_combinations(self) -> list[dict]:
        out = []
        for s in self.systems.values():
            for t in self.systems.values():
                if s.id == t.id:
                    continue
                v = runbook.validate_pair(s, t)
                out.append({"source": s.id, "target": t.id, "label": f"{s.label} → {t.label}",
                            "selective": v["ok"] or (not t.is_production and s.product == t.product),
                            "note": "ECC↔S/4HANA is a migration/conversion, not a refresh" if s.family != t.family else "",
                            "full_system_refresh": v["ok"], "blockers": v["blockers"], "warnings": v["warnings"]})
        return out

    # ---------------- projects ----------------
    def project(self, pid: str) -> Project:
        if pid not in self.projects:
            raise NotFound(f"project {pid}")
        return self.projects[pid]

    def create_project(self, actor: Principal, name: str, source_id: str, target_id: str) -> Project:
        s, t = self.system(source_id), self.system(target_id)
        authz.require_systems(self, actor, source_id, target_id)
        if t.is_production:
            raise Forbidden("production systems cannot be selected as refresh targets")
        if not t.can_be_write_target:
            raise Forbidden(f"{t.label} is locked against being overwritten")
        if s.id == t.id:
            raise Conflict("source and target must differ")
        if s.product != t.product:
            raise Conflict(f"product mismatch: {s.product} vs {t.product}"
                           + (" (ECC↔S/4HANA is a migration, not a refresh)" if s.family != t.family else ""))
        p = Project(f"prj-{uuid.uuid4().hex[:8]}", name, source_id, target_id, actor.id)
        self.projects[p.id] = p
        self.audit.append(actor.id, "project.created", p.id, {"source": s.label, "target": t.label})
        return p

    def _reset_downstream(self, p: Project):
        p.plan, p.report, p.approval, p.submitted_by = None, None, None, None
        p.status = "DRAFT"

    def set_manifest(self, actor: Principal, pid: str, scope: Scope, include_downstream: list[str],
                     masking_policy_id: str, conflict_policy: dict[str, str], instance_overrides: dict[str, str] | None = None,
                     approved_exceptions: dict[str, str] | None = None, editor: Principal | None = None) -> Manifest:
        p = self.project(pid)
        if p.status in ("RUNNING",):
            raise Conflict("project is running")
        prev = p.manifest
        authz.require_systems(self, actor, p.source_id, p.target_id)
        authz.require_hr(actor, scope.object_type)
        if authz.restricted(actor):
            if actor.attrs.get("company_codes") is not None:
                authz.require_companies(actor, scope.company_codes, "the manifest scope")
            authz.require_named(actor, scope, "the manifest scope")
        m = Manifest(name=p.name, version=(prev.version + 1) if prev else 1, source_system_id=p.source_id,
                     target_system_id=p.target_id, scope=scope, include_downstream=include_downstream,
                     masking_policy_id=masking_policy_id, conflict_policy=conflict_policy,
                     instance_overrides=instance_overrides or {},
                     approved_exceptions=dict(approved_exceptions if approved_exceptions is not None
                                              else (prev.approved_exceptions if prev else {})))
        p.manifests.append(m)
        self._reset_downstream(p)
        p.last_editor = (editor or actor).id
        if masking_policy_id in TEMPLATES and (p.masking_policy is None or p.masking_policy.id != masking_policy_id):
            p.masking_policy = template(masking_policy_id)
        self.audit.append(actor.id, "manifest.created", pid, {"version": m.version, "hash": m.content_hash()})
        return m

    def build_plan(self, actor: Principal, pid: str) -> dict:
        p = self.project(pid)
        if not p.manifest:
            raise Conflict("no manifest")
        t0 = time.perf_counter()
        plan = Planner(self.source_view(p.source_id), self.reg(p)).build(p.manifest)
        extract_s = time.perf_counter() - t0
        authz.require_hr_plan(actor, plan)
        authz.require_plan(actor, plan, {r["WERKS"]: r["BUKRS"] for r in self.source_view(p.source_id).select("T001W")})
        p.plan, p.report, p.approval = plan, None, None
        p.status = "PLANNED"
        self.plan_extract_s[pid] = extract_s
        if not plan.blocking:
            self.bench.record_plan(p, extract_s)
        rows = {t: [r for i in plan.instances.values() for r in i.rows.get(t, [])] for t in
                {t for i in plan.instances.values() for t in i.rows}}
        self.required_sensitive[pid] = discover_sensitive(rows)
        self.audit.append(actor.id, "plan.built", pid, {"hash": plan.manifest_hash, "instances": len(plan.instances),
                                                         "blocking": len(plan.blocking)})
        return self.plan_summary(pid)

    def plan_summary(self, pid: str) -> dict:
        p = self.project(pid)
        if not p.plan:
            raise Conflict("plan not built")
        s = p.plan.summary()
        s["estimate"] = self.bench.estimate_for_project(p)
        s["simulated"] = True
        s["source_total_rows"] = sum(self.adapters[p.source_id].table_counts().values()) if self.is_local(p.source_id) else None
        return s

    def instances(self, pid: str, type_: str | None, origin: str | None, offset: int, limit: int) -> dict:
        p = self.project(pid)
        if not p.plan:
            raise Conflict("plan not built")
        items = [i for i in p.plan.order if (not type_ or p.plan.instances[i].type == type_)
                 and (not origin or p.plan.instances[i].origin == origin)]
        page = [{"id": i, "type": p.plan.instances[i].type, "origin": p.plan.instances[i].origin,
                 "parent": p.plan.instances[i].parent, "requires": p.plan.instances[i].requires,
                 "configs": p.plan.instances[i].configs, "rows": p.plan.instances[i].row_counts(),
                 "decision": p.report.decisions[i].value if p.report and i in p.report.decisions else None}
                for i in items[offset:offset + limit]]
        return {"total": len(items), "offset": offset, "items": page}

    # ---------------- masking ----------------
    def masking_state(self, pid: str) -> dict:
        p = self.project(pid)
        disc = self.required_sensitive.get(pid, [])
        pol = p.masking_policy
        cov = [{**d, "covered": bool(pol and (d["table"], d["field"]) in pol.fields())} for d in disc]
        return {"policy": pol.to_dict() if pol else None, "templates": TEMPLATES, "strategies": sorted(STRATEGIES),
                "discovered": cov, "uncovered": sum(1 for c in cov if not c["covered"])}

    def add_masking_rules(self, actor: Principal, pid: str, rules: list[dict]) -> dict:
        p = self.project(pid)
        if p.status == "RUNNING":
            raise Conflict("project is running")
        p.masking_policy = p.masking_policy or MaskingPolicy("custom", "Custom policy")
        for r in rules:
            p.masking_policy.rules = [x for x in p.masking_policy.rules if (x.table, x.field) != (r["table"], r["field"])]
            p.masking_policy.rules.append(Rule(r["table"], r["field"], r["strategy"], r.get("category", r["strategy"].lower()),
                                               MaskMode(r.get("mode", "PSEUDONYMIZE"))))
        errs = p.masking_policy.validate()
        if errs:
            raise Conflict("; ".join(errs))
        p.report, p.approval = None, None
        p.status = "PLANNED" if p.plan else "DRAFT"
        self.audit.append(actor.id, "masking.rules_updated", pid, {"rules": len(rules)})
        return self.masking_state(pid)

    # ---------------- conflicts ----------------
    def analyze_conflicts(self, actor: Principal, pid: str) -> dict:
        p = self.project(pid)
        if not p.plan:
            raise Conflict("plan not built")
        sens = self._sensitive(p)
        p.report = analyze(p.plan, self.adapters[p.target_id], p.manifest, self.reg(p), sens)
        p.approval = None
        p.status = "CONFLICTS_ANALYZED"
        self.audit.append(actor.id, "conflicts.analyzed", pid, {"findings": len(p.report.findings), "blocking": len(p.report.blocking)})
        return self.conflicts(pid)

    def _sensitive(self, p: Project) -> set[tuple[str, str]]:
        """Fields whose values legitimately differ source-vs-target (masked or about to be)."""
        return (p.masking_policy.fields() if p.masking_policy else set()) | {
            (d["table"], d["field"]) for d in self.required_sensitive.get(p.id, [])}

    def conflicts(self, pid: str) -> dict:
        p = self.project(pid)
        if not p.report:
            raise Conflict("conflicts not analyzed")
        d = p.report.to_dict()
        d["simulated"] = True
        return d

    def suggest_policies(self, pid: str) -> dict:
        p = self.project(pid)
        if not p.report or not p.plan:
            raise Conflict("conflicts not analyzed")
        sens = self._sensitive(p)
        alts = []
        for name, pol in (("current", dict(p.manifest.conflict_policy)),
                          ("skip-differences", {**p.manifest.conflict_policy, "DUPLICATE_DIFFERENT": "SKIP"}),
                          ("quarantine-differences", {**p.manifest.conflict_policy, "DUPLICATE_DIFFERENT": "QUARANTINE"})):
            m = p.manifest.model_copy(update={"conflict_policy": pol})
            r = analyze(p.plan, self.adapters[p.target_id], m, self.reg(p), sens)
            alts.append({"policy": name, "settings": pol, "blocking": bool(r.blocking), "executable_objects": len(r.executable),
                         "quarantined": sum(1 for a in r.decisions.values() if a == Action.QUARANTINE)})
        return advisors.conflict_resolution(p.report.to_dict(), alts)

    def apply_conflict_policy(self, actor: Principal, pid: str, policy: dict[str, str],
                              overrides: dict[str, str] | None = None) -> Manifest:
        p = self.project(pid)
        m = p.manifest
        return self.set_manifest(actor, pid, m.scope, m.include_downstream, m.masking_policy_id,
                                 {**m.conflict_policy, **policy}, {**m.instance_overrides, **(overrides or {})})

    def approve_exception(self, actor: Principal, pid: str, instance: str, justification: str) -> Manifest:
        if not actor.can("exception:approve"):
            raise Forbidden("exception:approve required")
        if actor.kind == "agent":
            raise Forbidden("AI agents may not approve exceptions")
        if not justification.strip():
            raise Conflict("justification required")
        p = self.project(pid)
        if p.last_editor == actor.id:
            raise Forbidden("separation of duties: editor cannot approve their own exception")
        m = p.manifest
        # the exception approver is not the editor of the manifest: keep the previous editor for separation of duties
        editor = Principal(p.last_editor or actor.id, "", ())
        new = self.set_manifest(actor, pid, m.scope, m.include_downstream, m.masking_policy_id, m.conflict_policy,
                                m.instance_overrides, {**m.approved_exceptions, instance: justification}, editor)
        self.audit.append(actor.id, "exception.approved", pid, {"instance": instance, "justification": justification})
        return new

    # ---------------- approval ----------------
    def submit(self, actor: Principal, pid: str) -> Project:
        p = self.project(pid)
        if not (p.plan and p.report):
            raise Conflict("plan and conflict analysis are required")
        if p.plan.blocking:
            raise Conflict(f"plan has {len(p.plan.blocking)} blocking dependency issue(s)")
        if p.report.blocking:
            raise Conflict(f"{len(p.report.blocking)} blocking target conflict(s) must be resolved by policy or exception")
        if not p.masking_policy:
            raise Conflict("a masking policy is required (privacy-safe by default)")
        cov = MaskingEngine(p.masking_policy).coverage(self.required_sensitive.get(pid, []))
        if not cov["complete"]:
            raise Conflict(f"{len(cov['missing'])} discovered sensitive field(s) have no masking rule")
        hr = authz.hr_masking_violations(p.plan, p.masking_policy)
        if hr:
            raise Conflict("HR data requires per-run anonymization (policy gdpr-strict): " + "; ".join(hr[:4]) + (f" (+{len(hr) - 4} more)" if len(hr) > 4 else ""))
        p.status, p.submitted_by = "PENDING_APPROVAL", actor.id
        self.audit.append(actor.id, "plan.submitted", pid, {"hash": p.plan.manifest_hash})
        return p

    def approve(self, actor: Principal, pid: str, comment: str = "") -> Project:
        if not actor.can("plan:approve"):
            raise Forbidden("plan:approve required")
        p = self.project(pid)
        if p.status != "PENDING_APPROVAL":
            raise Conflict(f"project is {p.status}, not PENDING_APPROVAL")
        for creator in {p.created_by, p.last_editor, p.submitted_by} - {None}:
            check_separation_of_duties(creator, actor)
        p.approval = {"by": actor.id, "at": _now(), "manifest_hash": p.plan.manifest_hash, "comment": comment}
        p.status = "APPROVED"
        self.audit.append(actor.id, "plan.approved", pid, p.approval)
        return p

    def reject(self, actor: Principal, pid: str, comment: str) -> Project:
        if not actor.can("plan:approve"):
            raise Forbidden("plan:approve required")
        p = self.project(pid)
        p.status, p.approval = "DRAFT", None
        self.audit.append(actor.id, "plan.rejected", pid, {"comment": comment})
        return p

    # ---------------- execution ----------------
    def execute(self, actor: Principal, pid: str, fault_injector=None) -> Run:
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        p = self.project(pid)
        if p.status != "APPROVED" or not p.approval:
            raise Conflict("project is not approved")
        if p.approval["manifest_hash"] != p.manifest.content_hash() or p.plan.manifest_hash != p.manifest.content_hash():
            raise Conflict("approved plan does not match the current manifest; re-plan and re-approve")
        target = self.adapters[p.target_id]
        if target.system.is_production:
            raise ProductionWriteBlocked("production systems are never writable")
        if not target.system.can_be_write_target:
            raise Forbidden(f"{target.system.label} is locked against being overwritten (write access was revoked or never approved)")
        if authz.hr_masking_violations(p.plan, p.masking_policy):
            raise Conflict("HR data requires per-run anonymization: the masking policy no longer satisfies that")
        engine = MaskingEngine(p.masking_policy)
        self.engines[pid] = engine
        self.reg(p)
        run = self.executor.new_run(pid, p.plan, p.report)
        self.runs[run.id] = run
        p.runs.append(run.id)
        p.status = "RUNNING"
        self.audit.append(actor.id, "run.started", run.id, {"project": pid, "objects": len(run.order)})
        target.fault_injector = fault_injector
        try:
            self.executor.execute(run, p.plan, p.report, engine, target, actor.id)
        finally:
            target.fault_injector = None
        return self._after_run(p, run)

    def resume(self, actor: Principal, run_id: str, fault_injector=None) -> Run:
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        run = self.run(run_id)
        p = self.project(run.project_id)
        if run.status != "FAILED":
            raise Conflict("only FAILED runs can be resumed")
        target = self.adapters[p.target_id]
        self.reg(p)
        target.fault_injector = fault_injector
        self.audit.append(actor.id, "run.resumed", run.id, {"checkpoint": run.checkpoint})
        try:
            self.executor.execute(run, p.plan, p.report, self.engines[p.id], target, actor.id)
        finally:
            target.fault_injector = None
        return self._after_run(p, run)

    def _after_run(self, p: Project, run: Run) -> Run:
        if run.status == "COMPLETED":
            t0 = time.perf_counter()
            run.reconciliation = reconcile(run, p.plan, self.source_view(p.source_id), self.adapters[p.target_id],
                                           self.engines[p.id], self.reg(p), self.required_sensitive.get(p.id, []),
                                           p.report.row_exclusions)
            self.bench.record_run(p, run, time.perf_counter() - t0)
            run.release = run.reconciliation["release"]
            p.status = "COMPLETED" if run.release == "RELEASED" else "HELD"
            self.audit.append("system", "reconciliation.completed", run.id,
                              {"release": run.release, "failed": run.reconciliation["failed"]})
        else:
            p.status = "FAILED"
        return run

    def rollback(self, actor: Principal, run_id: str) -> Run:
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        run = self.run(run_id)
        p = self.project(run.project_id)
        if run.status not in ("FAILED", "COMPLETED"):
            raise Conflict("run cannot be rolled back in its current state")
        self.reg(p)
        self.executor.rollback(run, self.adapters[p.target_id], actor.id)
        p.status = "ROLLED_BACK"
        return run

    def run(self, rid: str) -> Run:
        if rid not in self.runs:
            raise NotFound(f"run {rid}")
        return self.runs[rid]

    # ---------------- reports ----------------
    def report_markdown(self, run_id: str) -> str:
        run = self.run(run_id)
        p = self.project(run.project_id)
        s, t = self.system(p.source_id), self.system(p.target_id)
        rec = run.reconciliation or {"checks": [], "release": "NOT_EVALUATED", "summary": {}, "masking": {}}
        L = [f"# Refresh execution report — {p.name}", "",
             "> **SIMULATED**: executed against synthetic SAP-like data; no real SAP system was read or written.", "",
             f"- Source: {s.label}  \n- Target: {t.label}  \n- Run: `{run.id}` ({run.status}), release: **{run.release}**",
             f"- Manifest v{p.manifest.version} hash `{run.manifest_hash[:16]}…`",
             f"- Approved by: {(p.approval or {}).get('by', 'n/a')} at {(p.approval or {}).get('at', 'n/a')}", "",
             "## Scope", f"```json\n{p.manifest.scope.model_dump_json(indent=2, exclude_defaults=True)}\n```", "",
             "## Outcome", f"- Objects loaded: {len(run.loaded)}  \n- Skipped (identical in target): {len(run.skipped)}  \n"
             f"- Quarantined: {len(run.quarantined)}", ""]
        if run.quarantined:
            L += ["### Quarantined objects"] + [f"- `{q}`" for q in run.quarantined] + [""]
        L += ["## Reconciliation", "| Category | Check | Result | Detail |", "|---|---|---|---|"]
        L += [f"| {c['category']} | {c['name']} | {c['status'].upper()} | {c['detail']} |" for c in rec["checks"]]
        m = rec.get("masking", {})
        L += ["", "## Masking", f"- Protection classes: {', '.join(m.get('protection_classes', []))}",
              f"- Reversible: {m.get('reversible')}", f"- Note: {m.get('residual_risk_note', '')}", ""]
        L += ["## Approvals & audit", f"- Audit chain: {self.audit.verify()}"]
        return "\n".join(L)

    def evidence_package(self, run_id: str) -> bytes:
        run = self.run(run_id)
        p = self.project(run.project_id)
        files = {
            "manifest.json": p.manifest.model_dump(mode="json"),
            "plan_summary.json": p.plan.summary() if p.plan else {},
            "conflicts.json": p.report.to_dict() if p.report else {},
            "run.json": run.public(),
            "reconciliation.json": run.reconciliation or {},
            "staging_hashes.json": run.staging_hashes,
            "audit.json": self.audit.entries(),
            "audit_verification.json": self.audit.verify(),
        }
        buf = io.BytesIO()
        digests = {}
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, obj in files.items():
                data = json.dumps(obj, indent=2, default=str).encode()
                digests[name] = sha256(data).hexdigest()
                z.writestr(name, data)
            rep = self.report_markdown(run_id).encode()
            digests["report.md"] = sha256(rep).hexdigest()
            z.writestr("report.md", rep)
            z.writestr("SHA256SUMS.json", json.dumps({"simulated": True, "files": digests}, indent=2))
        return buf.getvalue()

    # ---------------- agents ----------------
    def strategy(self, actor: Principal, pid: str, **kw) -> dict:
        p = self.project(pid)
        plan_rows = p.plan.summary()["total_rows"] if p.plan else 0
        src_rows = sum(self.adapters[p.source_id].table_counts().values()) if self.is_local(p.source_id) else max(plan_rows, 1) * 10
        return advisors.refresh_strategy(source_rows=src_rows, scope_rows=plan_rows, **kw)

    def masking_advice(self, pid: str) -> dict:
        p = self.project(pid)
        pol = p.masking_policy.fields() if p.masking_policy else set()
        return advisors.masking_recommendation(self.required_sensitive.get(pid, []), pol,
                                               self.system(p.target_id).role.value)

    # ---------------- data analysis (read-only) ----------------
    def _analysis_rows(self, actor: Principal, sid: str, table: str) -> list:
        from .sap.connectors.rfc import RemoteError
        s = self.system(sid)
        if not authz.system_ok(actor, s):
            raise Forbidden(f"outside your scope: you may not use system {s.label}")
        try:
            return self.adapters[s.id].select(table)
        except RemoteError as e:
            raise Conflict(f"the read of {table} failed: {e}")

    def analyze(self, actor: Principal, sid: str, kind: str, table: str, **kw) -> dict:
        """Distribution, selectivity or growth of one table of a source system. Read-only; the audit entry records what was asked, never values."""
        from .analysis import profiler
        allow_hr = actor.can("hr:copy")
        try:
            if kind not in ("distribution", "selectivity", "growth"):
                raise profiler.AnalysisError(f"unknown analysis {kind}")
            rows = self._analysis_rows(actor, sid, table) if table in profiler.TABLES else []
            if kind == "distribution":
                out = profiler.distribution(table, rows, kw["fields"], kw.get("top", 20), allow_hr)
            elif kind == "selectivity":
                out = profiler.selectivity(table, rows, kw["fields"], allow_hr)
            else:
                out = profiler.growth(table, rows, kw["date_field"], kw.get("period", "month"), allow_hr)
        except profiler.AnalysisError as e:
            raise Conflict(str(e))
        self.audit.append(actor.id, "analysis.run", self.system(sid).label, {"kind": kind, "table": table, "fields": kw.get("fields") or [kw.get("date_field")], "rows_read": out["rows_read"]})
        out["system"] = self.system(sid).label
        out["bounded"] = not self.is_local(sid)
        return out

    def analysis_catalog(self, sid: str) -> dict:
        from .analysis import profiler
        s = self.system(sid)
        reg = self.registries["S4" if family_is_s4(s) else "ECC"]
        tabs = profiler.table_objects(reg)
        names = {t["table"] for t in tabs}
        return {"system": s.label, "profiles": profiler.profiles(names), "tables": tabs, "date_fields": list(profiler.DATE_FIELDS), "not_available": profiler.NOT_AVAILABLE,
                "max_fields": profiler.MAX_FIELDS}
