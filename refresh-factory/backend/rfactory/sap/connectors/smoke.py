"""First-contact smoke test for a REAL SAP system: read-only, bounded, and its report contains no business data.

    python -m rfactory.sap.connectors.smoke rfc   --ashost localhost --sysnr 00 --client 100 --user RFREAD --password-env SAP_PW --yes
    python -m rfactory.sap.connectors.smoke odata --base-url https://host:44300 --user RFREAD --password-env SAP_PW --yes

What it does
  * connects through the same adapters the platform uses (RFC: pyrfc + the SAP NW RFC SDK; OData V2: HTTPS), so what works here works there;
  * reads system information, then checks the platform's DDIC model against the system (RFC) or its mapping against $metadata (OData);
  * reads every modelled table with a SMALL row cap and a call-rate limit, and reports counts (">= N" when capped), timings, error kinds,
    key-field widths and whether keys come back in the internal form the platform expects (leading zeros);
  * optionally probes the change-document reader (RFC) without reading any change content.
What it never does: write, call anything that is not on the read-only allow-list, or put a row value into its report (counts, field names,
widths, timings and error classes only). Run it against a sandbox or a copy first, with a dedicated read-only user.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ..adapter import SapSystem
from ..ddic import TABLES
from .profile import ConnectionProfile, ProfileError
from .rfc import RemoteAuthError, RemoteError, RemoteTableMissing, RfcCommunicationError, ScanTooLarge, UnstablePaging

DEFAULT_MAX_ROWS = 500
INTERNAL_WIDTH = {"VBELN": 10, "KUNNR": 10, "LIFNR": 10, "EBELN": 10, "MATNR": 18, "AUFNR": 12}  # internal (zero-padded) widths of numeric keys


def _classify(e: Exception) -> str:
    return ("not-authorised" if isinstance(e, RemoteAuthError) else "table-missing" if isinstance(e, RemoteTableMissing) else
            "communication" if isinstance(e, RfcCommunicationError) else "unstable-paging" if isinstance(e, UnstablePaging) else
            "refused-by-platform" if isinstance(e, RemoteError) else type(e).__name__)


def _key_shape(adapter, table: str, rows: list[dict]) -> dict:
    """Widths and leading-zero behaviour of the key fields, from the DATA but without revealing it."""
    out = {}
    for k in TABLES[table].keys:
        vals = [str(r.get(k, "")) for r in rows if r.get(k) not in (None, "")]
        if not vals:
            continue
        out[k] = {"widths": sorted({len(v) for v in vals}), "numeric": all(v.isdigit() for v in vals), "leading_zero": any(v.startswith("0") for v in vals if v.isdigit() and len(v) > 1)}
    return out


def run_smoke(adapter, *, tables: list[str] | None = None, max_rows: int = DEFAULT_MAX_ROWS, clock=time.perf_counter, probe_change_documents: bool = True) -> dict:
    kind = getattr(adapter, "kind", "rfc")
    adapter.profile.max_scan_rows = max_rows
    rep: dict = {"kind": kind, "started": datetime.now(timezone.utc).isoformat(), "row_cap": max_rows, "read_only": True,
                 "note": "This report contains counts, field names, widths, timings and error classes only: no row values.",
                 "system": {}, "connection": {}, "drift": {}, "tables": {}, "change_documents": None, "verdict": [], "stats": {}}
    t0 = clock()
    try:
        if kind == "rfc":
            adapter._call("RFC_PING")
            info = adapter._call("RFC_SYSTEM_INFO").get("RFCSI_EXPORT", {})
            rep["system"] = {"sid": info.get("RFCSYSID"), "release": info.get("RFCSAPRL"), "database": info.get("RFCDBSYS"), "host": info.get("RFCHOST"),
                             "system_date": info.get("RFCDATE"), "system_time": info.get("RFCTIME")}
        else:
            adapter.ping()
            rep["system"] = {"base_url": adapter.profile.base_url}
        rep["connection"] = {"ok": True, "seconds": round(clock() - t0, 3)}
    except Exception as e:  # noqa: BLE001 - the first failure is the finding
        rep["connection"] = {"ok": False, "error_class": _classify(e), "message": str(e)[:200]}
        rep["verdict"].append({"level": "BLOCKER", "text": f"Could not connect: {_classify(e)}. {str(e)[:160]}"})
        return rep

    drift = adapter.schema_drift()
    rep["drift"] = drift
    if drift:
        rep["verdict"].append({"level": "ATTENTION", "text": f"{len(drift)} table(s) differ from the platform's model: " + ", ".join(sorted(drift)[:8])})

    todo = tables or [t for t, d in TABLES.items() if not d.config or t in ("T001", "T001W")]
    for t in todo:
        entry: dict = {}
        t1 = clock()
        try:
            if hasattr(adapter, "mapped") and not adapter.mapped(t):
                entry = {"status": "unmapped", "note": "no OData mapping for this table"}
            else:
                rows = adapter.select(t)
                entry = {"status": "ok", "rows_read": len(rows), "capped": False, "seconds": round(clock() - t1, 3), "fields": len(TABLES[t].fields),
                         "empty_fields": sorted(f for f in TABLES[t].fields if rows and all(r.get(f) in (None, "") for r in rows))[:12], "keys": _key_shape(adapter, t, rows)}
        except ScanTooLarge:
            entry = {"status": "ok", "rows_read": max_rows, "capped": True, "seconds": round(clock() - t1, 3), "note": f"at least {max_rows} rows (read stopped at the cap)"}
        except Exception as e:  # noqa: BLE001 - classified, never raised: one table must not hide the others
            entry = {"status": "error", "error_class": _classify(e), "message": str(e)[:160]}
        rep["tables"][t] = entry

    bad = {t: e for t, e in rep["tables"].items() if e["status"] == "error"}
    auth = sorted(t for t, e in bad.items() if e["error_class"] == "not-authorised")
    missing = sorted(t for t, e in bad.items() if e["error_class"] == "table-missing")
    other = sorted(t for t, e in bad.items() if e["error_class"] not in ("not-authorised", "table-missing"))
    if auth:
        rep["verdict"].append({"level": "ATTENTION", "text": f"The user is not authorised to read: {', '.join(auth)} (S_TABU_DIS / S_RFC). Grant read access for the tables the refresh scope needs."})
    if missing:
        rep["verdict"].append({"level": "ATTENTION", "text": f"Not found in this system (module not installed, or a different release): {', '.join(missing)}"})
    if other:
        rep["verdict"].append({"level": "BLOCKER", "text": f"Unexpected errors on: {', '.join(other)}: see the table entries"})
    zero = sorted(t for t, e in rep["tables"].items() if e.get("status") == "ok" and any(
        k["numeric"] and min(k["widths"]) < INTERNAL_WIDTH[kk] for kk, k in e.get("keys", {}).items() if kk in INTERNAL_WIDTH))
    if zero:
        rep["verdict"].append({"level": "ATTENTION", "text": f"Document or partner numbers arrive without leading zeros in {', '.join(zero)}: the platform expects the internal form (conversion exits)."})
    ok = [t for t, e in rep["tables"].items() if e["status"] == "ok"]
    rep["verdict"].append({"level": "INFO", "text": f"{len(ok)} of {len(todo)} tables could be read."})

    if probe_change_documents and kind == "rfc" and getattr(adapter, "cdr", None) is not None:
        cdr = adapter.cdr
        probe: dict = {}
        adapter.profile.max_scan_rows = max(max_rows, 5000)  # the probe reads catalogue and header rows, not business tables: the table cap does not apply
        try:
            probe["covered_header_tables"] = sorted(cdr.coverage())
            probe["unreadable"] = cdr.unreadable
            probe["system_clock"] = cdr.source_now().isoformat()
            probe["watermark_now"] = cdr.watermark()
            probe["changes_last_hour"] = len(cdr.read(cdr.watermark() - 10_000)) if cdr.coverage() else None
        except Exception as e:  # noqa: BLE001
            probe["error_class"] = _classify(e)
            probe["message"] = str(e)[:160]
            rep["verdict"].append({"level": "ATTENTION", "text": f"The change-document reader could not run ({_classify(e)}): delta refresh will fall back to full compare."})
        rep["change_documents"] = probe
    rep["stats"] = adapter.stats.public()
    rep["finished"] = datetime.now(timezone.utc).isoformat()
    rep["seconds"] = round(clock() - t0, 2)
    return rep


def markdown(rep: dict) -> str:
    L = [f"# Smoke test ({rep['kind']})", "", f"- Read-only: yes · row cap per table: {rep['row_cap']} · seconds: {rep.get('seconds', '?')}"]
    L.append(f"- System: {json.dumps(rep['system'])}")
    L.append(f"- Connection: {json.dumps(rep['connection'])}")
    L += ["", "## Verdict"] + [f"- **{v['level']}**: {v['text']}" for v in rep["verdict"]]
    if rep["drift"]:
        L += ["", "## Differences from the platform's model"] + [f"- `{t}`: {'; '.join(v[:3])}" for t, v in sorted(rep["drift"].items())]
    L += ["", "## Tables", "", "| Table | Status | Rows read | Seconds | Note |", "|---|---|---|---|---|"]
    for t, e in sorted(rep["tables"].items()):
        L.append(f"| {t} | {e['status']}{' (' + e['error_class'] + ')' if e.get('error_class') else ''} | {e.get('rows_read', '')}{'+' if e.get('capped') else ''} | {e.get('seconds', '')} | {e.get('note') or e.get('message') or ''} |")
    if rep["change_documents"] is not None:
        L += ["", "## Change documents", "", "```json", json.dumps(rep["change_documents"], indent=1), "```"]
    L += ["", "_This report contains no row values._"]
    return "\n".join(L) + "\n"


def _profile(a: argparse.Namespace) -> ConnectionProfile:
    pw = f"env:{a.password_env}" if a.password_env else ""
    opts = {}
    if a.change_documents:
        opts["change_documents"] = True
    if a.token_url:
        opts["token_url"] = a.token_url
    if a.kind == "rfc":
        return ConnectionProfile(f"smoke {a.ashost}", "rfc", ashost=a.ashost, sysnr=a.sysnr, client=a.client, user=a.user, password_ref=pw,
                                 calls_per_minute=a.calls_per_minute, page_rows=min(a.max_rows, 500), options=opts)
    return ConnectionProfile(f"smoke {a.base_url}", "odata", base_url=a.base_url, client=a.client or "", user=a.user, password_ref=pw, calls_per_minute=a.calls_per_minute, options=opts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rfactory.sap.connectors.smoke", description=__doc__.split("\n\n")[0])
    ap.add_argument("kind", choices=["rfc", "odata"])
    ap.add_argument("--ashost", default="localhost")
    ap.add_argument("--sysnr", default="00")
    ap.add_argument("--client", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--token-url", default="")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password-env", required=True, help="name of the environment variable that holds the password (never the password itself)")
    ap.add_argument("--tables", default="", help="comma separated; default: every modelled table")
    ap.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    ap.add_argument("--calls-per-minute", type=int, default=120)
    ap.add_argument("--change-documents", action="store_true", help="also probe the CDHDR change-document reader (RFC)")
    ap.add_argument("--out", default=".", help="directory for the report files")
    ap.add_argument("--yes", action="store_true", help="confirm: read-only user, sandbox or copy, bounded reads")
    a = ap.parse_args(argv)
    if not os.environ.get(a.password_env):
        print(f"Environment variable {a.password_env} is not set.", file=sys.stderr)
        return 2
    try:
        prof = _profile(a)
        prof.validate()
    except ProfileError as e:
        print(f"Invalid settings: {e}", file=sys.stderr)
        return 2
    print(f"About to READ from {a.ashost + ' client ' + a.client if a.kind == 'rfc' else a.base_url} as {a.user}: at most {a.max_rows} rows per table, "
          f"{a.calls_per_minute} calls/minute, no writes. Use a sandbox or a copy and a read-only user.")
    if not a.yes:
        print("Add --yes to confirm and run.")
        return 2
    system = SapSystem(sid=(a.ashost if a.kind == "rfc" else "ODATA")[:8].upper(), client=a.client or "000", role="SBX")
    try:
        if a.kind == "rfc":
            from .rfc import PyRfcTransport, RfcSourceAdapter
            adapter = RfcSourceAdapter(system, PyRfcTransport(prof), prof)
        else:
            from .odata import HttpODataTransport, ODataSourceAdapter
            adapter = ODataSourceAdapter(system, HttpODataTransport(prof), prof)
    except ImportError:
        print("pyrfc / the SAP NetWeaver RFC SDK is not installed: RFC cannot be used on this machine (see docs/04-real-system-test.md).", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"Could not set up the connection: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    rep = run_smoke(adapter, tables=[t for t in a.tables.split(",") if t] or None, max_rows=a.max_rows, probe_change_documents=a.change_documents)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"smoke-{a.kind}-{(rep['system'].get('sid') or 'system')}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    (out / f"{stem}.json").write_text(json.dumps(rep, indent=1))
    (out / f"{stem}.md").write_text(markdown(rep))
    print(markdown(rep))
    print(f"Report written to {out / (stem + '.md')} (and .json). It contains no row values and is safe to share.")
    return 0 if rep["connection"].get("ok") and not any(v["level"] == "BLOCKER" for v in rep["verdict"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
