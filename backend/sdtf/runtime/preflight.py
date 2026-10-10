"""Preflight for a system before the first live test: what has to be true on the machine running the platform,
on the network and on the SAP side, checked in order with a fix for every failure.

The checks never write anything. On the simulated transports the SAP-side checks run against the simulators
and say so; on a live transport each check reaches the system in the gentlest way (DNS, a TCP connect, RFC_PING,
a function module search, the add-on handshake on T001), so that a failure names the exact layer.
"""
from __future__ import annotations

import importlib
import os
import socket
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..catalog.store import RecordStore
from ..models import SapSystem
from . import rfc as rfcmod
from . import target_api as tapi

ADDON_MODULES = (rfcmod.FM_OPEN_SNAPSHOT, rfcmod.FM_TABLE_METADATA, rfcmod.FM_READ_PACKAGE, rfcmod.FM_AGGREGATE, rfcmod.FM_CDC_POLL)
CORE_TABLES = ("T001", "T001K", "BKPF", "KNA1", "LFA1", "MARA")
TCP_TIMEOUT = 3.0


def _check(cid: str, title: str, status: str, detail: str = "", fix: str = "") -> dict:
    return {"id": cid, "title": title, "status": status, "detail": detail, "fix": fix}


def _dns(host: str) -> tuple[bool, str]:
    try:
        infos = socket.getaddrinfo(host, None)
        ips = sorted({i[4][0] for i in infos})
        return True, ", ".join(ips[:4])
    except (socket.gaierror, OSError) as e:
        return False, str(e)


def _tcp(host: str, port: int) -> tuple[bool, str]:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            return True, f"connected in {round((time.monotonic() - t0) * 1000)} ms"
    except (OSError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"


# ------------------------------------------------------------------------------------------------- RFC source
def preflight_rfc(session: Session, s: SapSystem) -> list[dict]:
    rfc = (s.meta or {}).get("rfc", {}) or {}
    mode = (rfc.get("transport") or rfcmod.rfc_config.transport_mode()).lower()
    dest = rfcmod.resolve_destination(s.sid, s.meta)
    simulated = mode == "simulated"
    checks: list[dict] = []
    # 1. the machine
    if simulated:
        checks.append(_check("pyrfc", "pyrfc and the SAP NW RFC SDK", "SKIP", "transport is the simulated add-on; no SDK needed", ""))
    else:
        try:
            m = importlib.import_module("pyrfc")
            checks.append(_check("pyrfc", "pyrfc and the SAP NW RFC SDK", "PASS", f"pyrfc {getattr(m, '__version__', '?')} imports", ""))
        except Exception as e:  # noqa: BLE001
            checks.append(_check("pyrfc", "pyrfc and the SAP NW RFC SDK", "FAIL", f"import pyrfc failed: {type(e).__name__}: {str(e)[:160]}", "install the SAP NW RFC SDK from the SAP Support Portal, set SAPNWRFC_HOME to its folder (and add its lib to PATH / LD_LIBRARY_PATH), then pip install pyrfc; python -c 'import pyrfc' must succeed in the environment that runs the API"))
        home = os.getenv("SAPNWRFC_HOME", "")
        if home and os.path.isdir(home):
            checks.append(_check("sdk-home", "SAPNWRFC_HOME", "PASS", home, ""))
        else:
            checks.append(_check("sdk-home", "SAPNWRFC_HOME", "WARN" if checks[-1]["status"] == "PASS" else "FAIL", "not set or not a directory" if not home else f"{home} is not a directory", "export SAPNWRFC_HOME=/path/to/nwrfcsdk (the folder with lib/ and include/)"))
    # 2. the destination
    stored = rfc.get("dest") or {}
    literal_secret = any(k in rfcmod.SECRET_KEYS and isinstance(v, str) and v and not v.startswith("env:") for k, v in stored.items())
    checks.append(_check("secrets", "No secret stored in the platform database", "FAIL" if literal_secret else "PASS", "a literal password sits in meta.rfc.dest" if literal_secret else "the stored destination carries no secret (env references only)", "replace the password in the destination with env:NAME and export NAME on the machine running the API" if literal_secret else ""))
    if simulated:
        checks.append(_check("destination", "RFC destination", "SKIP", "simulated transport", ""))
    else:
        missing = [k for k in ("ashost", "sysnr", "client", "user") if not dest.get(k)]
        pw_var = (stored.get("passwd") or "")[4:] if str(stored.get("passwd", "")).startswith("env:") else f"SDTF_RFC_DEST_{s.sid.upper()}_PASSWD"
        if missing:
            checks.append(_check("destination", "RFC destination", "FAIL", f"missing {', '.join(missing)}; resolved: {rfcmod.mask_destination(dest)}", f"set ashost / sysnr / client / user on the Connect page or in SDTF_RFC_DEST_{s.sid.upper()} (JSON pyrfc.Connection parameters)"))
        else:
            checks.append(_check("destination", "RFC destination", "PASS", str(rfcmod.mask_destination(dest)), ""))
        if dest.get("passwd"):
            checks.append(_check("password", "Password resolved from the environment", "PASS", f"{pw_var} is set in the API process", ""))
        else:
            checks.append(_check("password", "Password resolved from the environment", "FAIL", f"{pw_var} is empty in the API process", f"export {pw_var}='…' in the shell that starts the API (never in the database)"))
    # 3. the network
    if simulated or not dest.get("ashost"):
        checks.append(_check("dns", "Host name resolves", "SKIP", "simulated transport" if simulated else "no host", ""))
        checks.append(_check("tcp", "Gateway port reachable", "SKIP", "simulated transport" if simulated else "no host", ""))
    else:
        ok, detail = _dns(dest["ashost"])
        checks.append(_check("dns", "Host name resolves", "PASS" if ok else "FAIL", f"{dest['ashost']} → {detail}" if ok else f"{dest['ashost']}: {detail}", "" if ok else f"add the VM's address for {dest['ashost']} to the hosts file of this machine (C:\\Windows\\System32\\drivers\\etc\\hosts), or use the IP address as the host"))
        port = 3300 + int(str(dest.get("sysnr", "0")).strip() or 0)
        if ok:
            tok, tdetail = _tcp(dest["ashost"], port)
            checks.append(_check("tcp", "Gateway port reachable", "PASS" if tok else "FAIL", f"{dest['ashost']}:{port} {tdetail}", "" if tok else f"the SAP instance is not listening on {port}: is the VM running and the instance started (sapcontrol -nr {dest.get('sysnr')} -function GetProcessList)? does the host firewall allow the port?"))
        else:
            checks.append(_check("tcp", "Gateway port reachable", "SKIP", "host did not resolve", ""))
    # 4. the SAP side
    transport = None
    blocked = any(c["status"] == "FAIL" for c in checks if c["id"] in ("pyrfc", "destination", "password", "dns", "tcp"))
    if blocked and not simulated:
        for cid, title in (("rfc-ping", "RFC logon (RFC_PING)"), ("addon", "Z_SDTF_* function modules present"), ("handshake", "Add-on handshake on T001"), ("tables", "Core tables readable (S_TABU_NAM)")):
            checks.append(_check(cid, title, "SKIP", "earlier check failed", ""))
        return checks
    try:
        transport = rfcmod.make_transport(s.sid, s.meta, store_loader=lambda: RecordStore.load(session, s.id, tables=list(CORE_TABLES)))
    except rfcmod.RfcError as e:
        checks.append(_check("rfc-ping", "RFC logon (RFC_PING)", "FAIL", f"{e.key}: {e.message[:200]}", "check user, password, client and the user type (a SYSTEM / COMMUNICATION user that is not locked); RFC_LOGON_FAILURE names the reason in SM21 on the SAP side"))
        return checks
    if simulated:
        checks.append(_check("rfc-ping", "RFC logon (RFC_PING)", "PASS", "simulated add-on answers", ""))
        checks.append(_check("addon", "Z_SDTF_* function modules present", "PASS", "the simulated add-on provides all five: " + ", ".join(ADDON_MODULES), ""))
    else:
        try:
            transport.call("RFC_PING")
            checks.append(_check("rfc-ping", "RFC logon (RFC_PING)", "PASS", "logon and ping ok", ""))
        except rfcmod.RfcError as e:
            checks.append(_check("rfc-ping", "RFC logon (RFC_PING)", "FAIL", f"{e.key}: {e.message[:200]}", "check user, password, client and that the user is not locked; RFC_LOGON_FAILURE names the reason; a communication failure here means the port answered but the system refused the connection"))
            checks.append(_check("addon", "Z_SDTF_* function modules present", "SKIP", "no logon", ""))
            checks.append(_check("handshake", "Add-on handshake on T001", "SKIP", "no logon", ""))
            checks.append(_check("tables", "Core tables readable (S_TABU_NAM)", "SKIP", "no logon", ""))
            return checks
        try:
            r = transport.call("RFC_FUNCTION_SEARCH", FUNCNAME="Z_SDTF_*", GROUPNAME="*")
            found = {str(x.get("FUNCNAME", "")).upper() for x in (r.get("FUNCTIONS") or [])}
            missing = [m for m in ADDON_MODULES if m not in found]
            checks.append(_check("addon", "Z_SDTF_* function modules present", "PASS" if not missing else "FAIL", "all five present" if not missing else f"missing: {', '.join(missing)} (found: {', '.join(sorted(found)) or 'none'})", "" if not missing else "create the missing modules from sap-abap/src/*.abap with the structures in sap-abap/src/DDIC.md in a development client, mark them remote-enabled, and transport them to the client you read from (sap-abop/README.md)"))
            if missing:
                checks.append(_check("handshake", "Add-on handshake on T001", "SKIP", "add-on incomplete", ""))
                checks.append(_check("tables", "Core tables readable (S_TABU_NAM)", "SKIP", "add-on incomplete", ""))
                return checks
        except rfcmod.RfcError as e:
            checks.append(_check("addon", "Z_SDTF_* function modules present", "WARN", f"RFC_FUNCTION_SEARCH not callable ({e.key}); the handshake below decides", "grant S_RFC for RFC_FUNCTION_SEARCH or ignore this check if the handshake passes"))
    client = rfcmod.AbapAddonClient(transport, package_size=5)
    try:
        snap = client.open_snapshot(["T001"])
        meta = client.table_metadata("T001")
        rows, _cursor, _eof = client.read_package("T001", [])
        try:
            n = client.count("T001", [])
            agg = f"aggregate ok ({n} rows counted in the database)"
        except rfcmod.RfcError as e:
            agg = f"aggregate missing ({e.key}): reconciliation reads rows without read-integrity evidence"
        checks.append(_check("handshake", "Add-on handshake on T001", "PASS", f"snapshot {snap}; T001 {meta.get('rows')} rows, {len(rows)} sampled, checksum verified; {agg}", ""))
    except rfcmod.RfcError as e:
        checks.append(_check("handshake", "Add-on handshake on T001", "FAIL", f"{e.key}: {e.message[:200]}", "the add-on answered but the contract failed: compare the module's interface with sap-abap/README.md; NOT_AUTHORIZED means S_TABU_NAM for T001 is missing for the RFC user"))
        checks.append(_check("tables", "Core tables readable (S_TABU_NAM)", "SKIP", "handshake failed", ""))
        return checks
    denied, errors = [], []
    for t in CORE_TABLES:
        try:
            md = client.table_metadata(t)
            if not md.get("authorized", True):
                denied.append(t)
        except rfcmod.RfcError as e:
            if e.key == "TABLE_UNKNOWN":
                continue
            errors.append(f"{t}: {e.key}")
    if denied or errors:
        checks.append(_check("tables", "Core tables readable (S_TABU_NAM)", "WARN", (f"not authorised: {', '.join(denied)}" if denied else "") + ("; " if denied and errors else "") + ("; ".join(errors) if errors else ""), "grant S_TABU_NAM (display) for the tables in scope to the RFC user; discovery lists every table it cannot read"))
    else:
        checks.append(_check("tables", "Core tables readable (S_TABU_NAM)", "PASS", ", ".join(CORE_TABLES), ""))
    return checks


# ------------------------------------------------------------------------------------------------- API target
def preflight_api(session: Session, s: SapSystem) -> list[dict]:
    api = (s.meta or {}).get("api", {}) or {}
    mode = (api.get("transport") or os.getenv("SDTF_S4_API_TRANSPORT", "auto")).lower()
    dest = tapi.resolve_api_destination(s.sid, s.meta)
    simulated = s.connector == "SYNTHETIC" or mode == "simulated" or (mode == "auto" and not dest)
    checks: list[dict] = []
    stored = api.get("dest") or {}
    literal_secret = any(k in ("passwd", "password", "client_secret", "token") and isinstance(v, str) and v and not v.startswith("env:") for k, v in stored.items())
    checks.append(_check("secrets", "No secret stored in the platform database", "FAIL" if literal_secret else "PASS", "a literal secret sits in meta.api.dest" if literal_secret else "the stored destination carries no secret (env references only)", "replace it with env:NAME and export NAME on the machine running the API" if literal_secret else ""))
    if simulated:
        for cid, title in (("destination", "API destination"), ("password", "Credential resolved from the environment"), ("dns", "Host name resolves"), ("tcp", "HTTPS port reachable"), ("tls", "TLS verification")):
            checks.append(_check(cid, title, "SKIP", "simulated gateway", ""))
    else:
        if not dest.get("base_url"):
            checks.append(_check("destination", "API destination", "FAIL", f"no base_url; resolved: {tapi.mask_api_destination(dest)}", f"set the base URL (https://host:44300) on the Connect page or in SDTF_S4_API_{s.sid.upper()}"))
        else:
            checks.append(_check("destination", "API destination", "PASS", str(tapi.mask_api_destination(dest)), ""))
        oauth = bool(dest.get("token_url") and dest.get("client_id"))
        cred = dest.get("client_secret") if oauth else dest.get("passwd")
        var = (stored.get("client_secret" if oauth else "passwd") or "")
        var = var[4:] if str(var).startswith("env:") else f"SDTF_S4_API_{s.sid.upper()}_PASSWD"
        checks.append(_check("password", "Credential resolved from the environment", "PASS" if cred else "FAIL", f"{var} is set" if cred else f"{var} is empty in the API process", "" if cred else f"export {var}='…' in the shell that starts the API"))
        u = urlparse(dest.get("base_url") or "")
        host, port = u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
        if host:
            ok, detail = _dns(host)
            checks.append(_check("dns", "Host name resolves", "PASS" if ok else "FAIL", f"{host} → {detail}" if ok else f"{host}: {detail}", "" if ok else f"add the VM's address for {host} to the hosts file of this machine, or use the IP address in the base URL (the certificate name will then not match: untick TLS verification for the lab)"))
            if ok:
                tok, tdetail = _tcp(host, port)
                checks.append(_check("tcp", "HTTPS port reachable", "PASS" if tok else "FAIL", f"{host}:{port} {tdetail}", "" if tok else f"nothing listens on {host}:{port}: is the VM running and the ICM up (SMICM)? is the HTTPS port {port} the one SMICM shows?"))
            else:
                checks.append(_check("tcp", "HTTPS port reachable", "SKIP", "host did not resolve", ""))
        else:
            checks.append(_check("dns", "Host name resolves", "SKIP", "no host", ""))
            checks.append(_check("tcp", "HTTPS port reachable", "SKIP", "no host", ""))
        verify = dest.get("verify", dest.get("verify_tls", True))
        if u.scheme == "https" and verify and (host.endswith(".nodomain") or host.endswith(".local") or host.replace(".", "").isdigit()):
            checks.append(_check("tls", "TLS verification", "WARN", f"verification is on and {host} will not carry a publicly trusted certificate", "untick 'verify the TLS certificate' for the lab system, or import the system's certificate into this machine's trust store"))
        else:
            checks.append(_check("tls", "TLS verification", "PASS", "on" if verify else "off (lab setting; never for a productive system)", ""))
    blocked = any(c["status"] == "FAIL" for c in checks if c["id"] in ("destination", "password", "dns", "tcp"))
    if blocked:
        checks.append(_check("catalogue", "Gateway catalogue and bound services", "SKIP", "earlier check failed", ""))
        return checks
    try:
        from . import metadata_check as mc

        transport = tapi.make_target_transport(session, s)
        res = mc.check_target(transport)
    except tapi.ApiError as e:
        checks.append(_check("catalogue", "Gateway catalogue and bound services", "FAIL", f"{e.code}: {e.message[:200]}", "401 / 403: check the communication user and its authorisations; 404 on the catalogue: activate the services in /IWFND/MAINT_SERVICE; a TLS error: see the TLS check"))
        return checks
    if not res["catalog_available"]:
        checks.append(_check("catalogue", "Gateway catalogue and bound services", "FAIL", f"catalogue not readable: {res['catalog_error'][:200]}", "the gateway catalogue service (/sap/opu/odata/IWFND/CATALOGSERVICE;v=2) must be active and the user authorised"))
        return checks
    summ = res["summary"]
    not_ok = [x["service"] for x in res["services"] if x.get("verdict") in ("UNAVAILABLE", "ENTITY_SETS_MISSING")]
    dev = [x["service"] for x in res["services"] if x.get("verdict") == "DEVIATIONS"]
    status = "FAIL" if not_ok else ("WARN" if dev else "PASS")
    detail = f"{summ.get('VERIFIED', 0)} verified, {summ.get('DEVIATIONS', 0)} with deviations, {summ.get('ENTITY_SETS_MISSING', 0)} missing entity sets, {summ.get('UNAVAILABLE', 0)} unavailable" + (f"; not usable: {', '.join(not_ok)}" if not_ok else "") + (f"; deviations: {', '.join(dev)}" if dev else "")
    checks.append(_check("catalogue", "Gateway catalogue and bound services", status, detail, "" if status == "PASS" else "run the metadata check on the Connect page for the field-level deviations; activate missing services in /IWFND/MAINT_SERVICE"))
    return checks


# --------------------------------------------------------------------------------------------------- document
def preflight(session: Session, s: SapSystem) -> dict:
    t0 = time.monotonic()
    if s.connector == "RFC":
        checks = preflight_rfc(session, s)
        kind = "RFC"
    elif s.connector == "SYNTHETIC" and s.role == "SOURCE":
        checks = [_check("connector", "Connector", "PASS", "synthetic landscape in the platform's record store: there is no system to reach", "register the real ECC as an RFC source (Connect page) to test a live system")]
        kind = "SYNTHETIC"
    elif s.connector in ("API", "SYNTHETIC"):
        checks = preflight_api(session, s)
        kind = "API"
        if s.connector == "API" and (s.meta or {}).get("rfc"):
            checks += [{**c, "id": f"rfc-{c['id']}", "title": f"Read-back over RFC: {c['title']}"} for c in preflight_rfc(session, s)]
    else:
        checks, kind = [_check("connector", "Connector", "FAIL", f"no preflight for connector {s.connector}", "register the system with connector RFC (source) or API (target)")], s.connector
    counts = {k: sum(1 for c in checks if c["status"] == k) for k in ("PASS", "WARN", "FAIL", "SKIP")}
    first_fail = next((c for c in checks if c["status"] == "FAIL"), None)
    simulated = kind == "SYNTHETIC" or all(c["status"] == "SKIP" for c in checks if c["id"] in ("dns", "tcp"))
    return {
        "system": {"id": s.id, "sid": s.sid, "client": s.client, "connector": s.connector, "product": s.product, "release": s.release},
        "kind": kind,
        "simulated": simulated,
        "checks": checks,
        "summary": {**counts, "ready": counts["FAIL"] == 0, "first_failure": first_fail["id"] if first_fail else None},
        "next": ("fix: " + first_fail["fix"]) if first_fail else ("the simulators answer every check; connect the live system by saving a destination on the Connect page and run the preflight again" if simulated else "run the handshake and the discovery on the Landscape page, then the metadata check (API targets)"),
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
        "generated_at": datetime.now(UTC).isoformat(),
        "note": "reads only: nothing is written on any system; secrets are read from the environment and never shown",
    }


def preflight_markdown(res: dict) -> str:
    s = res["system"]
    md = [f"# Preflight {s['sid']}/{s['client']} ({s['connector']})", "", f"{res['summary']['PASS']} pass, {res['summary']['WARN']} warn, {res['summary']['FAIL']} fail, {res['summary']['SKIP']} skipped; ready: {'yes' if res['summary']['ready'] else 'no'}. {res['next']}", "", "| # | Check | Status | Detail | Fix |", "|---|---|---|---|---|"]
    for c in res["checks"]:
        md.append(f"| {c['id']} | {c['title']} | {c['status']} | {c['detail'].replace('|', '/')} | {c['fix'].replace('|', '/')} |")
    md += ["", f"> {res['note']}"]
    return "\n".join(md) + "\n"


__all__ = ["ADDON_MODULES", "CORE_TABLES", "preflight", "preflight_rfc", "preflight_api", "preflight_markdown"]
