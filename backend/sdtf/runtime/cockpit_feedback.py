"""Import of the migration cockpit's upload simulation feedback.

After the staging files are uploaded, the *Migrate Your Data* app simulates the migration object per instance
and produces a message log (severity, message, instance key, structure/sheet, field; downloadable as a
spreadsheet or copied as text). This module reads that log in the shapes it comes in (CSV/TSV with any
delimiter, JSON, SpreadsheetML XML), recognises the columns by several spellings, matches every message to the
business object instance the export wrote (by the instance key as the export groups it, by the key columns when
the log carries them, or by a key search across objects when the object is not named), and records:

* one `CockpitFeedback` row per message, with the staged instance it belongs to (or `unmatched`);
* the staged rows of instances with an error: `load_status = COCKPIT_ERROR` (warnings keep their status);
* one LOAD-stage `TransformationException` per error message (severity ERROR) and per warning (WARN), so the
  exception list and the Runs page show them next to the platform's own findings;
* a classified summary per object (configuration missing, mandatory field missing, duplicate, format/value,
  authorisation, other) on the run report, with the share of instances that passed the simulation.

The export can then write a retry package of the rejected instances only (`scope="rejected"`).

What this is not: the app's message log format is not standardised by SAP; the column recognition is tolerant,
and every row that cannot be placed is reported as unmatched with the reason instead of being dropped. A sample
log can be generated for a run (`sample_feedback`) to exercise the flow; it is marked as illustrative.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit.service import record_event
from ..catalog.business_objects import BUSINESS_OBJECTS
from ..catalog.tables import TABLES
from ..models import CockpitFeedback, MigrationRun, SapSystem, TransformationException
from ..staging import get_backend
from .cockpit_templates import SS, templates_for
from .loaders import EventView, object_of, plan_cockpit, regroup_by_header
from .migration_objects import project_registry

SEVERITIES = {"E": "E", "ERROR": "E", "A": "E", "ABORT": "E", "X": "E", "W": "W", "WARNING": "W", "S": "S", "SUCCESS": "S", "I": "I", "INFO": "I", "INFORMATION": "I"}
_COLUMNS = (
    ("severity", ("messagetype", "type", "severity", "msgtype", "msgty", "status", "category", "level")),
    ("message", ("message", "messagetext", "text", "msgtext", "description", "longtext", "error")),
    ("key", ("instance", "instancekey", "key", "objectkey", "recordkey", "businesskey", "id", "keyvalue", "keyvalues", "instanceid")),
    ("object", ("migrationobject", "object", "objectname", "objecttype", "migobject", "businessobject")),
    ("sheet", ("sheet", "sheetname", "structure", "structurename", "table")),
    ("field", ("field", "fieldname", "column")),
    ("msg_class", ("messageclass", "msgclass", "msgid", "class", "messageid")),
    ("msg_number", ("messagenumber", "msgno", "number", "msgnr")),
    ("row", ("row", "rownumber", "line", "lineno")),
)
CATEGORIES = (
    ("duplicate", r"already exist|duplicate|already (?:been )?(?:created|migrated|posted)|exists already"),
    ("configuration_missing", r"does not exist|not exist|not defined|unknown (?:company|plant|sales|purch|account|chart|controlling|currency|unit)|not (?:found|maintained|valid) in|is not allowed in|no .* (?:exists|maintained)"),
    ("mandatory_missing", r"mandatory|required|must be (?:specified|filled|entered)|is initial|missing"),
    ("format_or_value", r"format|invalid|not (?:a )?valid|conversion|too long|exceeds|length|numeric|date|decimal|unit of measure"),
    ("authorisation", r"authori[sz]ation|not authori[sz]ed|no permission"),
    ("locked", r"locked|lock"),
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def classify(message: str) -> str:
    m = (message or "").lower()
    for cat, pat in CATEGORIES:
        if re.search(pat, m):
            return cat
    return "other"


# ------------------------------------------------------------------------------------------------ parsing
def _rows_from_spreadsheetml(text: str) -> list[list[str]]:
    from xml.etree import ElementTree as ET

    root = ET.fromstring(text)
    ns = {"ss": SS}
    out: list[list[str]] = []
    for ws in root.findall("ss:Worksheet", ns):
        for row in ws.findall("ss:Table/ss:Row", ns):
            cells, col = [], 0
            for c in row.findall("ss:Cell", ns):
                idx = c.get(f"{{{SS}}}Index")
                col = int(idx) if idx else col + 1
                while len(cells) < col - 1:
                    cells.append("")
                d = c.find("ss:Data", ns)
                cells.append("".join(d.itertext()).strip() if d is not None else "")
            out.append(cells)
        if out:
            break  # the first sheet with rows is the message log
    return out


def parse_feedback(content: str, filename: str = "") -> tuple[list[dict], dict]:
    """Normalise a message log into rows `{severity, message, key, object, sheet, field, msg_class, msg_number,
    row, extra}` plus a layout report (format, recognised columns, unrecognised headers)."""
    text = content.lstrip("﻿").strip()
    layout: dict = {"format": "", "columns": {}, "unrecognised": [], "rows": 0}
    records: list[dict] = []
    if not text:
        raise ValueError("the feedback file is empty")
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("messages") or data.get("items") or data.get("entries") or data.get("rows") or []
        if not isinstance(data, list):
            raise ValueError("JSON feedback must be a list of messages or an object with a 'messages' list")
        layout["format"] = "json"
        keys = list(data[0]) if data else []
        table = [[str(k) for k in keys]] + [[("" if r.get(k) is None else str(r.get(k))) for k in keys] for r in data] if data else [[]]
    elif text.startswith("<"):
        layout["format"] = "spreadsheetml"
        table = _rows_from_spreadsheetml(text)
    else:
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            delim = dialect.delimiter
        except csv.Error:
            delim = "\t" if "\t" in sample else ";" if sample.count(";") > sample.count(",") else ","
        layout["format"] = f"delimited ({'tab' if delim == chr(9) else delim})"
        table = list(csv.reader(io.StringIO(text), delimiter=delim))
    table = [r for r in table if any(c.strip() for c in r)]
    if not table:
        raise ValueError("no rows found in the feedback file")
    # header: the first row whose cells match at least two known column names
    hdr_i = next((i for i, r in enumerate(table[:10]) if sum(1 for c in r if any(_norm(c) in names for _, names in _COLUMNS)) >= 2), None)
    if hdr_i is None:
        raise ValueError("no header row recognised (expected columns such as Message Type, Message, Instance/Key)")
    header = table[hdr_i]
    cols: dict[str, int] = {}
    unrec = []
    for j, c in enumerate(header):
        n = _norm(c)
        hit = next((k for k, names in _COLUMNS if n in names and k not in cols), None)
        if hit:
            cols[hit] = j
        elif c.strip():
            unrec.append(c.strip())
    if "message" not in cols:
        raise ValueError("no message column recognised")
    layout["columns"] = {k: header[j] for k, j in cols.items()}
    layout["unrecognised"] = unrec
    extra_cols = [(j, header[j].strip()) for j in range(len(header)) if j not in cols.values() and header[j].strip()]
    for r in table[hdr_i + 1 :]:
        v = {k: (r[j].strip() if j < len(r) else "") for k, j in cols.items()}

        def get(k: str, _v: dict = v) -> str:
            return _v.get(k, "")

        sev_raw = get("severity").strip().upper()
        sev = SEVERITIES.get(sev_raw, SEVERITIES.get(sev_raw[:1], "")) if sev_raw else ""
        msg = get("message")
        if not msg and not get("key"):
            continue
        records.append({"severity": sev or ("E" if re.search(r"\berror\b", msg, re.I) else "I"), "severity_raw": sev_raw, "message": msg, "key": get("key"), "object": get("object"), "sheet": get("sheet"), "field": get("field"), "msg_class": get("msg_class"), "msg_number": get("msg_number"), "row": get("row"), "extra": {name: (r[j].strip() if j < len(r) else "") for j, name in extra_cols}})
    layout["rows"] = len(records)
    return records, layout


# ------------------------------------------------------------------------------------------------ matching
def _instances(session: Session, run: MigrationRun) -> dict[str, dict[str, dict]]:
    """Every instance the export routes to the cockpit: `{object_type: {instance_key: {"records": [...],
    "keys": {field: value}}}}`, grouped exactly as the export groups them."""
    tgt = session.get(SapSystem, run.target_system_id)
    product = tgt.product if tgt else "S4HANA"
    backend = get_backend(session=session)
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for rec in backend.iter_records(run.id):
        if rec.load_status in ("STAGED", "SKIPPED", "MATCHED") or not rec.target_payload:
            continue
        groups[object_of(rec.table_name, rec.target_key or rec.record_key, rec.target_payload)].append(rec)
    groups, _ = regroup_by_header(groups)
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for (bo_id, okey), members in groups.items():
        bo = BUSINESS_OBJECTS.get(bo_id)
        views = [EventView(0, r.table_name, "I", r.record_key, r.target_key, dict(r.target_payload), bo_id, okey, okey) for r in members]
        cockpit, _ = plan_cockpit(bo_id, views, product)
        if not cockpit:
            continue
        keyed = {e.record_key for g, _ in cockpit for e in g}
        recs = [r for r in members if r.record_key in keyed]
        head = next((r for r in recs if bo and r.table_name == bo.header_table), recs[0])
        keys = {k: str(head.target_payload.get(k, "")) for k in (bo.key_fields if bo else ())}
        out[bo_id][okey] = {"records": recs, "keys": keys}
    return out


def _key_variants(parts: list[str]) -> set[str]:
    """Forms a feedback key may take for an instance key: parts joined by any separator, with and without leading
    zeros, upper-cased."""
    vs = set()
    for strip in (False, True):
        ps = [p.lstrip("0") or "0" if strip and p.isdigit() else p for p in parts]
        vs.add("".join(_norm(p) for p in ps))
    return vs


def _object_for(name: str, run: MigrationRun, session: Session, templates: dict, registry: list[dict]) -> str | None:
    """Business object for a migration object name/ID/business object id the log names."""
    if not name:
        return None
    if name in BUSINESS_OBJECTS:
        return name
    n = _norm(name)
    for ot, t in templates.items():
        if t.migration_object and _norm(t.migration_object) == n:
            return ot
    for e in registry:
        if _norm(e.get("name", "")) == n or _norm(e.get("id", "")) == n:
            ots = e.get("object_types") or []
            if len(ots) == 1:
                return ots[0]
    from ..catalog.migration_objects import MIGRATION_OBJECTS

    for mo in MIGRATION_OBJECTS:
        if _norm(mo.id_hint) == n or any(_norm(x) == n for _, x in mo.names) or _norm(mo.cloud_name) == n:
            if len(mo.object_types) == 1:
                return mo.object_types[0]
    for ot, bo in BUSINESS_OBJECTS.items():
        if _norm(bo.name) == n:
            return ot
    return None


def import_feedback(session: Session, run_id: str, content: str, filename: str, actor: str, replace: bool = True, attempt_sequence: int | None = None) -> dict:
    """`attempt_sequence`: the round the log answers (default: the latest round not yet simulated, else the latest
    round). `replace` replaces the feedback of that round only."""
    from .cockpit_attempts import attempts, current_attempt, record_outcomes

    run = session.get(MigrationRun, run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    rows, layout = parse_feedback(content, filename)
    if attempt_sequence is not None:
        attempt = next((a for a in attempts(session, run_id) if a.sequence == attempt_sequence), None)
        if attempt is None:
            raise ValueError(f"round {attempt_sequence} does not exist for this run")
    else:
        attempt = current_attempt(session, run_id)
    instances = _instances(session, run)
    templates = templates_for(session, run.project_id)
    registry = project_registry(session, run.project_id)
    # indexes: per object the key variants; global index for key search when the object is not named
    index: dict[str, dict[str, str]] = {}
    for ot, insts in instances.items():
        idx: dict[str, str] = {}
        for okey, inst in insts.items():
            for v in _key_variants(okey.split("|")) | _key_variants(list(inst["keys"].values())):
                idx.setdefault(v, okey)
        index[ot] = idx
    if replace:
        for f in session.execute(select(CockpitFeedback).where(CockpitFeedback.run_id == run_id, CockpitFeedback.attempt_id == (attempt.id if attempt else ""))).scalars().all():
            session.delete(f)
        for ex in session.execute(select(TransformationException).where(TransformationException.run_id == run_id, TransformationException.stage == "COCKPIT", TransformationException.rule_id.like(f"cockpit:%:r{attempt.sequence if attempt else 0}"))).scalars().all():
            session.delete(ex)
        session.flush()
    backend = get_backend(session=session)
    created: list[CockpitFeedback] = []
    exceptions: list[TransformationException] = []
    errored: dict[str, set[str]] = defaultdict(set)
    in_log: set[tuple[str, str]] = set()
    unmatched_reasons: dict[str, int] = defaultdict(int)
    for r in rows:
        ot = _object_for(r["object"], run, session, templates, registry)
        okey = None
        reason = ""
        # key columns of the business object inside the row's extra columns
        if ot and ot in instances:
            bo = BUSINESS_OBJECTS[ot]
            if all(k in r["extra"] and r["extra"][k] for k in bo.key_fields):
                cand = "|".join(r["extra"][k] for k in bo.key_fields)
                okey = next((index[ot].get(v) for v in _key_variants(cand.split("|")) if v in index[ot]), None)
        if okey is None and r["key"]:
            parts = [p for p in re.split(r"[\s/|,;:_\-]+", r["key"].strip()) if p]
            variants = _key_variants(parts) | {_norm(r["key"])}
            if ot and ot in index:
                okey = next((index[ot][v] for v in variants if v in index[ot]), None)
                if okey is None:
                    reason = f"key {r['key']!r} not among the exported instances of {ot}"
            else:
                hits = {(o, index[o][v]) for o in index for v in variants if v in index[o]}
                if len(hits) == 1:
                    ot, okey = next(iter(hits))
                elif len(hits) > 1:
                    reason = f"key {r['key']!r} is ambiguous across objects ({', '.join(sorted(o for o, _ in hits))}); name the migration object"
                else:
                    reason = f"key {r['key']!r} not among the exported instances" + (f" (object {r['object']!r} not recognised)" if r["object"] and not ot else "")
        elif okey is None:
            reason = "no instance key in the message" if not r["key"] else reason or "not matched"
        if okey is None:
            unmatched_reasons[reason] += 1
        cat = classify(r["message"]) if r["severity"] in ("E", "W") else ""
        fb = CockpitFeedback(run_id=run_id, object_type=ot or "", migration_object=r["object"], instance_key=r["key"], matched_key=okey or "", matched=okey is not None, severity=r["severity"], message=r["message"][:1000], message_class=r["msg_class"][:40], message_number=r["msg_number"][:10], sheet=r["sheet"][:120], field=r["field"][:60], category=cat, reason=reason[:300], source_file=filename[:200], imported_by=actor, attempt_id=attempt.id if attempt else "", attempt_sequence=attempt.sequence if attempt else 0)
        session.add(fb)
        created.append(fb)
        if okey is not None and r["severity"] in ("E", "W"):
            table = next((t for t in TABLES if _norm(t) == _norm(r["sheet"])), "") or (BUSINESS_OBJECTS[ot].header_table if ot in BUSINESS_OBJECTS else "")
            exceptions.append(TransformationException(run_id=run_id, stage="COCKPIT", table_name=table, record_key=okey, rule_id=f"cockpit:{cat}:r{attempt.sequence if attempt else 0}", severity="ERROR" if r["severity"] == "E" else "WARN", message=f"{r['message']}" + (f" [{r['sheet']}{'.' + r['field'] if r['field'] else ''}]" if r["sheet"] or r["field"] else "") + (f" (round {attempt.sequence})" if attempt else "")))
            if r["severity"] == "E":
                errored[ot].add(okey)
        if okey is not None:
            in_log.add((ot, okey))
    # staged statuses: instances with an error are marked; instances in the log without an error are released
    rejected_set = {(ot, okey) for ot, keys in errored.items() for okey in keys}
    changed = []
    released = 0
    for ot, keys in errored.items():
        for okey in keys:
            for rec in instances[ot][okey]["records"]:
                if rec.load_status != "COCKPIT_ERROR":
                    rec.load_status = "COCKPIT_ERROR"
                    rec.lineage = (rec.lineage or []) + [{"rule": "cockpit", "field": "*", "from": "simulation", "to": "COCKPIT_ERROR", "round": attempt.sequence if attempt else 0}]
                    changed.append(rec)
    for ot, okey in in_log - rejected_set:
        for rec in instances.get(ot, {}).get(okey, {}).get("records", []):
            if rec.load_status == "COCKPIT_ERROR":
                rec.load_status = "LOADED"
                rec.lineage = (rec.lineage or []) + [{"rule": "cockpit", "field": "*", "from": "COCKPIT_ERROR", "to": "LOADED", "round": attempt.sequence if attempt else 0}]
                changed.append(rec)
                released += 1
    if changed:
        backend.update_records(run_id, changed)
    session.add_all(exceptions)
    session.flush()
    result = record_outcomes(session, attempt, rejected_set, in_log, actor, filename) if attempt is not None else {}
    summary = feedback_summary(session, run_id, instances=instances, layout=layout, unmatched_reasons=dict(unmatched_reasons), attempt=attempt)
    run.report = {**(run.report or {}), "cockpit_feedback": summary}
    record_event(session, actor, "COCKPIT_FEEDBACK_IMPORTED", "RUN", run_id, {"file": filename, "round": attempt.sequence if attempt else 0, "messages": len(created), "errors": summary["errors"], "unmatched": summary["unmatched"], "rows_marked": len(changed) - released, "rows_released": released, **({"resolved": result.get("resolved", 0)} if result else {})})
    session.flush()
    return summary


def feedback_summary(session: Session, run_id: str, instances: dict | None = None, layout: dict | None = None, unmatched_reasons: dict | None = None, attempt=None) -> dict:
    """Summary of the feedback of one round (default: the latest round that has feedback), plus the run-level
    burn-down across rounds."""
    from .cockpit_attempts import attempts, burndown, still_rejected

    run = session.get(MigrationRun, run_id)
    if attempt is None:
        with_fb = [a for a in attempts(session, run_id) if a.status in ("SIMULATED", "MIGRATED")]
        attempt = with_fb[-1] if with_fb else None
    stmt = select(CockpitFeedback).where(CockpitFeedback.run_id == run_id)
    if attempt is not None:
        stmt = stmt.where(CockpitFeedback.attempt_id == attempt.id)
    rows = session.execute(stmt).scalars().all()
    if instances is None:
        instances = _instances(session, run)
    per: dict[str, dict] = {}
    for ot, insts in instances.items():
        per[ot] = {"instances": len(insts), "errors": 0, "warnings": 0, "success": 0, "info": 0, "instances_with_errors": set(), "instances_with_messages": set(), "categories": defaultdict(int)}
    for f in rows:
        if not f.matched or f.object_type not in per:
            continue
        p = per[f.object_type]
        p["instances_with_messages"].add(f.matched_key)
        if f.severity == "E":
            p["errors"] += 1
            p["instances_with_errors"].add(f.matched_key)
        elif f.severity == "W":
            p["warnings"] += 1
        elif f.severity == "S":
            p["success"] += 1
        else:
            p["info"] += 1
        if f.category:
            p["categories"][f.category] += 1
    objects = {}
    for ot, p in per.items():
        with_msgs = len(p["instances_with_messages"])
        rejected = len(p["instances_with_errors"])
        objects[ot] = {"instances": p["instances"], "with_messages": with_msgs, "rejected": rejected, "accepted": max(0, with_msgs - rejected), "not_in_log": p["instances"] - with_msgs, "errors": p["errors"], "warnings": p["warnings"], "success": p["success"], "info": p["info"], "categories": dict(p["categories"]), "pass_rate": round((with_msgs - rejected) / with_msgs, 3) if with_msgs else None}
    unmatched = [f for f in rows if not f.matched]
    reasons = unmatched_reasons if unmatched_reasons is not None else dict(sorted(defaultdict(int, {f.reason: sum(1 for g in unmatched if g.reason == f.reason) for f in unmatched}).items()))
    total_rejected = sum(o["rejected"] for o in objects.values())
    total_with = sum(o["with_messages"] for o in objects.values())
    bd = burndown(session, run_id)
    return {"imported": bool(rows), "round": attempt.sequence if attempt else 0, "round_scope": attempt.scope if attempt else "", "messages": len(rows), "errors": sum(1 for f in rows if f.severity == "E"), "warnings": sum(1 for f in rows if f.severity == "W"), "matched": len(rows) - len(unmatched), "unmatched": len(unmatched), "unmatched_reasons": reasons, "rejected_instances": total_rejected, "instances_in_log": total_with, "pass_rate": round((total_with - total_rejected) / total_with, 3) if total_with else None, "categories": {k: sum(o["categories"].get(k, 0) for o in objects.values()) for k in sorted({c for o in objects.values() for c in o["categories"]})}, "objects": objects, "layout": layout or (run.report or {}).get("cockpit_feedback", {}).get("layout", {}), "source_files": sorted({f.source_file for f in rows}), "imported_at": datetime.now(timezone.utc).isoformat() if rows else None, "still_rejected": len(still_rejected(session, run_id)), "rounds": len(bd["rounds"]), "resolved_total": bd["resolved_total"], "converged": bd["converged"]}


def rejected_instances(session: Session, run_id: str) -> set[tuple[str, str]]:
    """(object_type, instance_key) of every instance whose latest simulation outcome is rejected."""
    from .cockpit_attempts import still_rejected

    return still_rejected(session, run_id)


def feedback_out(f: CockpitFeedback) -> dict:
    return {"id": f.id, "round": f.attempt_sequence, "object_type": f.object_type, "migration_object": f.migration_object, "instance_key": f.instance_key, "matched_key": f.matched_key, "matched": f.matched, "severity": f.severity, "message": f.message, "message_class": f.message_class, "message_number": f.message_number, "sheet": f.sheet, "field": f.field, "category": f.category, "reason": f.reason, "source_file": f.source_file, "imported_by": f.imported_by, "created_at": f.created_at}


def clear_feedback(session: Session, run_id: str, actor: str) -> int:
    rows = session.execute(select(CockpitFeedback).where(CockpitFeedback.run_id == run_id)).scalars().all()
    n = len(rows)
    for f in rows:
        session.delete(f)
    for ex in session.execute(select(TransformationException).where(TransformationException.run_id == run_id, TransformationException.stage == "COCKPIT")).scalars().all():
        session.delete(ex)
    run = session.get(MigrationRun, run_id)
    backend = get_backend(session=session)
    changed = []
    for rec in backend.iter_records(run_id, status="COCKPIT_ERROR"):
        rec.load_status = "LOADED"
        rec.lineage = (rec.lineage or []) + [{"rule": "cockpit", "field": "*", "from": "COCKPIT_ERROR", "to": "LOADED"}]
        changed.append(rec)
    if changed:
        backend.update_records(run_id, changed)
    from .cockpit_attempts import reset_attempts

    rounds = reset_attempts(session, run_id)
    if run is not None:
        run.report = {k: v for k, v in (run.report or {}).items() if k != "cockpit_feedback"}
    session.flush()
    record_event(session, actor, "COCKPIT_FEEDBACK_CLEARED", "RUN", run_id, {"messages": n, "rows_reset": len(changed), "rounds_reset": rounds})
    return n


# ------------------------------------------------------------------------------------------------ sample
def sample_feedback(session: Session, run_id: str, errors_per_object: int | None = None) -> str:
    """An ILLUSTRATIVE message log for the run's exported instances (CSV as the app's spreadsheet export is laid
    out): success messages for most instances, one error and a warning per object. For a retry round (current
    round of scope `rejected`) the log covers that round's instances only and accepts all but one, so the rounds
    converge. Not an SAP file."""
    from .cockpit_attempts import current_attempt

    run = session.get(MigrationRun, run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    instances = _instances(session, run)
    cur = current_attempt(session, run_id)
    retry = cur is not None and cur.scope == "rejected" and cur.status in ("EXPORTED", "UPLOADED")
    if retry:
        keep = {(ot, k) for ot, k in cur.instance_keys or []}
        instances = {ot: {k: v for k, v in insts.items() if (ot, k) in keep} for ot, insts in instances.items()}
        instances = {ot: insts for ot, insts in instances.items() if insts}
    if errors_per_object is None:
        errors_per_object = 0 if retry else 1
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Migration Object", "Instance", "Message Type", "Message Class", "Message Number", "Message", "Sheet", "Field"])
    samples = {"configuration_missing": "Company code {v} does not exist", "mandatory_missing": "Field {f} is mandatory and has no value", "format_or_value": "Value '{v}' in field {f} has an invalid format", "duplicate": "Record {k} already exists in the target system"}
    cats = list(samples)
    for n, (ot, insts) in enumerate(sorted(instances.items())):
        bo = BUSINESS_OBJECTS.get(ot)
        name = bo.name if bo else ot
        keys = sorted(insts)
        for i, okey in enumerate(keys):
            inst = insts[okey]
            key_txt = "/".join(inst["keys"].values()) or okey.replace("|", "/")
            if i < errors_per_object or (retry and n == 0 and i == 0):  # a retry round keeps one instance rejected
                cat = cats[(n + i) % len(cats)]
                kf = list(inst["keys"]) or ["KEY"]
                w.writerow([name, key_txt, "E", "SDTF_SIM", "001", samples[cat].format(v=list(inst["keys"].values())[0] if inst["keys"] else okey, f=kf[-1], k=key_txt), bo.header_table if bo else "", kf[-1]])
            elif i == errors_per_object:
                w.writerow([name, key_txt, "W", "SDTF_SIM", "002", "Value will be converted to the target format", bo.header_table if bo else "", ""])
            else:
                w.writerow([name, key_txt, "S", "SDTF_SIM", "000", "Instance simulated successfully", "", ""])
    w.writerow(["ILLUSTRATIVE", "", "I", "", "", "Not an SAP file: generated by sdtf to exercise the feedback import", "", ""])
    return buf.getvalue()
