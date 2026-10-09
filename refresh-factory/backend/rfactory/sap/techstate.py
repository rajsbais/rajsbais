"""SIMULATED technical configuration of an SAP system (the things a system copy drags along from production).

Business data lives in `synthetic.py`; this is the *technical* side that post-copy automation has to fix: logical system,
RFC destinations, background jobs, mail routing, IDoc partners, TMS, licence, certificates and SSO trusts, users, cloud and
integration endpoints, printers, external schedulers, monitoring, gateway aliases, instance parameters, HANA state.
A real adapter would read and change these through supported APIs (RFC/BAPI, SOAP, OData); here they are plain data so the
factory's logic, guards and evidence can be exercised end to end.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

# category -> item identity field for list categories (dict categories have None)
LISTS = {"rfc": "name", "jobs": "name", "idoc": "name", "certs": "subject", "sso_trusts": "name", "users": "user", "cloud": "name",
         "integration": "name", "printers": "name", "schedulers": "name", "monitoring": "name", "gateway": "name"}
DICTS = ["smtp", "tms", "license", "hana", "params", "logon"]
SCALARS = ["logical_system", "logsys_refs"]
CATEGORIES = list(LISTS) + DICTS + SCALARS


def _item(name, host, env, active=True, **kw):
    return {"name": name, "host": host, "env": env, "active": active, **kw}


def prod_state(sid: str, client: str, family: str) -> dict[str, Any]:
    p = f"{sid.lower()}"
    return {
        "logical_system": f"{sid}CLNT{client}",
        "logsys_refs": {f"{sid}CLNT{client}": 48213},
        "params": {"SAPSYSTEMNAME": sid, "login/system_client": client, "rdisp/mshost": f"{p}app01.prod.corp", "icm/host_name_full": f"{p}.prod.corp"},
        "logon": {"group": f"{sid}_PROD_GROUP", "message_server": f"{p}ms.prod.corp"},
        "rfc": [_item("BW_PROD", "bwp.prod.corp", "prod"), _item("SRM_PROD", "srmp.prod.corp", "prod"), _item("CRM_PROD", "crmp.prod.corp", "prod"),
                _item(f"{sid}_SELF", f"{p}app01.prod.corp", "prod")],
        "jobs": [{"name": "SAP_REORG_JOBS", "status": "scheduled", "interface": False, "destination": None, "env": "prod", "host": None},
                 {"name": "ZSD_EDI_OUTBOUND", "status": "scheduled", "interface": True, "destination": "edi.prod.corp", "env": "prod", "host": "edi.prod.corp"},
                 {"name": "ZFI_BANK_PAYMENTS", "status": "released", "interface": True, "destination": "bank.prod.corp", "env": "prod", "host": "bank.prod.corp"},
                 {"name": "ZMM_PO_RELEASE", "status": "scheduled", "interface": False, "destination": None, "env": "prod", "host": None},
                 {"name": "ZHR_PAYROLL_EXPORT", "status": "scheduled", "interface": True, "destination": "payroll.prod.corp", "env": "prod", "host": "payroll.prod.corp"}],
        "smtp": {"node": "INT", "relay_host": "smtp.prod.corp", "host": "smtp.prod.corp", "env": "prod", "outbound_enabled": True},
        "idoc": [_item("CUST_EDI_OUT", "edi.prod.corp", "prod", direction="outbound", port="EDI_PROD"),
                 _item("BANK_PAIN_OUT", "bank.prod.corp", "prod", direction="outbound", port="BANK_PROD"),
                 _item("SUPPLIER_IN", "edi.prod.corp", "prod", direction="inbound", port="EDI_PROD")],
        "tms": {"domain": "DOM_PROD", "controller": "tmsp.prod.corp", "host": "tmsp.prod.corp", "env": "prod", "consistent": True, "routes": ["DEV>QAS>PRD"]},
        "license": {"valid": True, "hardware_key": f"{sid}-HWKEY-PROD", "expires": "2027-12-31"},
        "certs": [{"subject": f"CN={p}.prod.corp", "pse": "SSL server Standard", "env": "prod", "host": f"{p}.prod.corp"},
                  {"subject": "CN=sso.prod.corp", "pse": "SAML2 SP", "env": "prod", "host": "sso.prod.corp"}],
        "sso_trusts": [_item("PROD_IDP", "idp.prod.corp", "prod", trusted=True), _item("BW_PROD_TRUST", "bwp.prod.corp", "prod", trusted=True)],
        "users": [{"user": "ALICE", "type": "dialog", "locked": False, "roles": ["Z_SD_CLERK"], "env": "prod", "host": None},
                  {"user": "BOB", "type": "dialog", "locked": False, "roles": ["Z_FI_ACCOUNTANT"], "env": "prod", "host": None},
                  {"user": "FIREFIGHTER_01", "type": "dialog", "locked": False, "roles": ["SAP_ALL"], "env": "prod", "host": None},
                  {"user": "DDIC", "type": "dialog", "locked": False, "roles": ["SAP_ALL"], "env": "prod", "host": None},
                  {"user": "WF-BATCH", "type": "system", "locked": False, "roles": ["Z_WORKFLOW"], "env": "prod", "host": None},
                  {"user": "RFC_BW", "type": "communication", "locked": False, "roles": ["Z_RFC_BW"], "env": "prod", "host": None}],
        "cloud": [_item("TENANT_PROD", "tenant-prod.cloud.example.com", "prod"), _item("ARIBA_PROD", "ariba-prod.cloud.example.com", "prod")],
        "integration": [_item("PO_SENDER_EDI", "po.prod.corp", "prod"), _item("CPI_TENANT_PROD", "cpi-prod.integration.example.com", "prod")],
        "printers": [_item("LP01", "print.prod.corp", "prod"), _item("LP02", "print2.prod.corp", "prod")],
        "schedulers": [_item("CONTROLM_AGENT", "ctm.prod.corp", "prod")],
        "monitoring": [_item("SOLMAN_ALERTS", "solman.prod.corp", "prod"), _item("SPLUNK_FWD", "splunk.prod.corp", "prod")],
        "gateway": [_item("BACKEND_ALIAS", f"{p}app01.prod.corp", "prod")] if family == "S4" else [],
        "hana": {"log_mode": "normal", "backup_catalog_ok": True, "license_ok": True, "replication": "none", "host": f"{p}db.prod.corp", "env": "prod"},
    }


def nonprod_state(sid: str, client: str, family: str, tier: str = "qa") -> dict[str, Any]:
    p, t = sid.lower(), tier
    return {
        "logical_system": f"{sid}CLNT{client}",
        "logsys_refs": {f"{sid}CLNT{client}": 0},
        "params": {"SAPSYSTEMNAME": sid, "login/system_client": client, "rdisp/mshost": f"{p}app01.{t}.corp", "icm/host_name_full": f"{p}.{t}.corp"},
        "logon": {"group": f"{sid}_{t.upper()}_GROUP", "message_server": f"{p}ms.{t}.corp"},
        "rfc": [_item("BW_QA", f"bwq.{t}.corp", "nonprod"), _item("SRM_QA", f"srmq.{t}.corp", "nonprod"), _item(f"{sid}_SELF", f"{p}app01.{t}.corp", "nonprod")],
        "jobs": [{"name": "SAP_REORG_JOBS", "status": "scheduled", "interface": False, "destination": None, "env": "nonprod", "host": None},
                 {"name": "ZMM_PO_RELEASE", "status": "scheduled", "interface": False, "destination": None, "env": "nonprod", "host": None},
                 {"name": "ZQA_SMOKE_TEST", "status": "scheduled", "interface": False, "destination": None, "env": "nonprod", "host": None},
                 {"name": "ZSD_EDI_OUTBOUND", "status": "scheduled", "interface": True, "destination": f"edi.{t}.corp", "env": "nonprod", "host": f"edi.{t}.corp"}],
        "smtp": {"node": "INT", "relay_host": f"mailsink.{t}.corp", "host": f"mailsink.{t}.corp", "env": "nonprod", "outbound_enabled": False},
        "idoc": [_item("CUST_EDI_OUT", f"edi.{t}.corp", "nonprod", False, direction="outbound", port="EDI_QA"),
                 _item("SUPPLIER_IN", f"edi.{t}.corp", "nonprod", True, direction="inbound", port="EDI_QA")],
        "tms": {"domain": f"DOM_{t.upper()}", "controller": f"tms.{t}.corp", "host": f"tms.{t}.corp", "env": "nonprod", "consistent": True, "routes": ["DEV>QAS"]},
        "license": {"valid": True, "hardware_key": f"{sid}-HWKEY-{t.upper()}", "expires": "2027-06-30"},
        "certs": [{"subject": f"CN={p}.{t}.corp", "pse": "SSL server Standard", "env": "nonprod", "host": f"{p}.{t}.corp"},
                  {"subject": f"CN=sso.{t}.corp", "pse": "SAML2 SP", "env": "nonprod", "host": f"sso.{t}.corp"}],
        "sso_trusts": [_item("QA_IDP", f"idp.{t}.corp", "nonprod", trusted=True)],
        "users": [{"user": "QA_TESTER1", "type": "dialog", "locked": False, "roles": ["Z_QA_TEST"], "env": "nonprod", "host": None},
                  {"user": "QA_ADMIN", "type": "dialog", "locked": False, "roles": ["Z_QA_ADMIN"], "env": "nonprod", "host": None},
                  {"user": "WF-BATCH", "type": "system", "locked": False, "roles": ["Z_WORKFLOW"], "env": "nonprod", "host": None},
                  {"user": "RFC_BW", "type": "communication", "locked": False, "roles": ["Z_RFC_BW"], "env": "nonprod", "host": None}],
        "cloud": [_item("TENANT_QA", f"tenant-{t}.cloud.example.com", "nonprod")],
        "integration": [_item("PO_SENDER_EDI", f"po.{t}.corp", "nonprod"), _item("CPI_TENANT_QA", f"cpi-{t}.integration.example.com", "nonprod")],
        "printers": [_item("LP01", f"print.{t}.corp", "nonprod")],
        "schedulers": [_item("CONTROLM_AGENT", f"ctm.{t}.corp", "nonprod", False)],
        "monitoring": [_item("SOLMAN_ALERTS", f"solman.{t}.corp", "nonprod")],
        "gateway": [_item("BACKEND_ALIAS", f"{p}app01.{t}.corp", "nonprod")] if family == "S4" else [],
        "hana": {"log_mode": "normal", "backup_catalog_ok": True, "license_ok": True, "replication": "none", "host": f"{p}db.{t}.corp", "env": "nonprod"},
    }


class TechState:
    """Mutable technical configuration with snapshot/restore/digest helpers."""

    def __init__(self, state: dict[str, Any]):
        self.s = state

    @classmethod
    def default_for(cls, system) -> "TechState":
        if system.is_production:
            return cls(prod_state(system.sid, system.client, system.family))
        tier = {"TRN": "trn", "SBX": "sbx", "UAT": "uat", "DEV": "dev"}.get(system.role.value, "qa")
        return cls(nonprod_state(system.sid, system.client, system.family, tier))

    def snapshot(self, cats: list[str] | None = None) -> dict[str, Any]:
        return {c: copy.deepcopy(self.s[c]) for c in (cats or CATEGORIES) if c in self.s}

    def restore(self, snap: dict[str, Any]) -> None:
        for c, v in snap.items():
            self.s[c] = copy.deepcopy(v)

    def digest(self, cats: list[str] | None = None) -> str:
        return hashlib.sha256(json.dumps(self.snapshot(cats), sort_keys=True, default=str).encode()).hexdigest()

    def hosts(self) -> set[str]:
        out: set[str] = set()
        for c in self.s.values():
            for it in (c if isinstance(c, list) else [c] if isinstance(c, dict) else []):
                if isinstance(it, dict):
                    for k in ("host", "destination", "relay_host", "controller", "message_server"):
                        if it.get(k):
                            out.add(it[k])
        return out

    def deliver_test_mail(self) -> str:
        """Simulated SMTP test: returns the host that would actually receive the message."""
        return self.s["smtp"]["relay_host"]


def is_prod_ref(item: dict, prod_hosts: set[str]) -> bool:
    """An item points to production if it is labelled prod OR its host/destination is a known production host
    (so a mislabelled environment cannot hide a production endpoint)."""
    if not isinstance(item, dict):
        return False
    if item.get("env") == "prod":
        return True
    return any(item.get(k) in prod_hosts for k in ("host", "destination", "relay_host", "controller") if item.get(k))


def list_items(state: dict[str, Any], cat: str) -> list[dict]:
    v = state.get(cat, [])
    return v if isinstance(v, list) else [v] if isinstance(v, dict) else []


def simulate_system_copy(source: "TechState", target: "TechState") -> dict:
    """SIMULATION of what a homogeneous system copy leaves in the target: every database-resident technical setting of
    production arrives in the non-production system. File-system/infrastructure settings (instance parameters, HANA
    host, logon groups) stay with the target; the licence becomes invalid (hardware key mismatch); TMS is inconsistent."""
    keep = {c: copy.deepcopy(target.s[c]) for c in ("params", "hana", "logon")}
    target.s = copy.deepcopy(source.s)
    target.restore(keep)
    target.s["license"] = {**target.s["license"], "valid": False}
    target.s["tms"] = {**target.s["tms"], "consistent": False}
    return {"copied_categories": [c for c in CATEGORIES if c not in keep], "kept": sorted(keep), "licence": "invalid until reinstalled"}
