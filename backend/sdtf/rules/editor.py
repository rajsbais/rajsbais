"""Rule editor services behind the Rules workbench: a structured rule document composed into the canonical YAML
of the DSL, lookup tables parsed from CSV exports, the grid view of a rule set and the per-rule decisions that
precede the four-eyes approval of the set. See docs/05-transformation-rule-dsl.md."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

import yaml
from sqlalchemy.orm import Session

from ..audit.service import record_event
from ..models import RuleSet
from .engine import RULE_TYPES, _rule_fields

DECISIONS = ("APPROVED", "REJECTED", "PENDING")
RULE_KEYS = ("id", "type", "description", "tables", "field", "fields", "also_fields", "map", "lookup", "on_missing", "default", "strategy", "prefix", "offset", "emit_field", "currency_field", "to", "rates", "set", "overwrite", "when", "then", "drop_source", "key_field", "set_field", "message")
HEADER_WORDS = {"source", "target", "from", "to", "old", "new", "key", "value", "src", "tgt", "ecc", "s4", "s4hana", "code", "mapped", "mapping"}


# ------------------------------------------------------------------------------------ structured document
def normalise_rule(r: dict) -> dict:
    """A rule as the editor holds it, with empty values dropped and the table list as a list."""
    out = {}
    for k in RULE_KEYS:
        v = r.get(k)
        if v in (None, "", [], {}):
            continue
        if k == "tables" and isinstance(v, str):
            v = [t.strip() for t in v.split(",") if t.strip()]
        if k in ("fields", "also_fields") and isinstance(v, str):
            v = [t.strip() for t in v.split(",") if t.strip()]
        out[k] = v
    for k, v in r.items():
        if k not in out and k not in RULE_KEYS and v not in (None, "", [], {}):
            out[k] = v
    out.setdefault("tables", ["*"])
    return out


def compose_ruleset(doc: dict) -> str:
    """The canonical YAML of a structured rule document (name, version, description, applies_to, lookups, rules,
    tests). Rules keep their order; the YAML is what gets hashed, validated and approved."""
    if not doc.get("ruleset"):
        raise ValueError("the rule document needs a 'ruleset' name")
    rules = [normalise_rule(r) for r in (doc.get("rules") or []) if isinstance(r, dict)]
    lookups = {str(k): {str(a): b for a, b in (v or {}).items()} for k, v in (doc.get("lookups") or {}).items() if k}
    out = {"ruleset": str(doc["ruleset"]), "version": int(doc.get("version") or 1), "description": str(doc.get("description") or "")}
    if doc.get("applies_to"):
        out["applies_to"] = doc["applies_to"]
    out["lookups"] = lookups
    out["rules"] = rules
    out["tests"] = [t for t in (doc.get("tests") or []) if isinstance(t, dict)]
    return yaml.safe_dump(out, sort_keys=False, allow_unicode=True)


def document_of(rs: RuleSet) -> dict:
    """The structured document of a stored rule set, for the editor grid."""
    src = yaml.safe_load(rs.source_yaml) or {}
    return {"ruleset": rs.name, "version": rs.version, "description": src.get("description", ""), "applies_to": src.get("applies_to") or {}, "lookups": rs.compiled.get("lookups") or {}, "rules": rs.compiled.get("rules") or [], "tests": src.get("tests") or []}


# ------------------------------------------------------------------------------------------- lookup CSV
def parse_lookup_csv(text: str, name: str, key_column: str | None = None, value_column: str | None = None, delimiter: str | None = None) -> dict:
    """A lookup table from a two-column CSV export (comma, semicolon or tab). The first row is the header when it
    names the requested columns or reads like one (source/target, from/to, old/new, key/value); without a header the
    first two columns are key and value. Duplicate keys with the same value are skipped, conflicting ones are errors."""
    text = text.lstrip("﻿")
    sample = text[:4096]
    if delimiter is None:
        delimiter = max((",", ";", "\t", "|"), key=lambda d: sample.count(d))
        if sample.count(delimiter) == 0:
            delimiter = ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    report = {"name": name, "delimiter": delimiter, "header": False, "columns": [], "key_column": key_column, "value_column": value_column, "rows": 0, "entries": {}, "skipped": 0, "duplicates": [], "conflicts": [], "errors": []}
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        report["errors"].append("lookup name must be alphanumeric (underscores and dashes allowed)")
    if not rows:
        report["errors"].append("the file holds no rows")
        return report
    first = [c.strip() for c in rows[0]]
    wants = {c.lower() for c in (key_column, value_column) if c}
    header = bool(wants and wants <= {c.lower() for c in first}) or (not wants and len(first) >= 2 and all(c.lower() in HEADER_WORDS for c in first[:2]))
    if wants and not header:
        report["errors"].append(f"columns {sorted(wants)} not found in the first row {first}")
        return report
    ki, vi = 0, 1
    if header:
        report["header"], report["columns"] = True, first
        lower = [c.lower() for c in first]
        ki = lower.index(key_column.lower()) if key_column else 0
        vi = lower.index(value_column.lower()) if value_column else (1 if len(first) > 1 else 0)
        if ki == vi:
            report["errors"].append("key and value columns must differ")
            return report
        rows = rows[1:]
    report["key_column"], report["value_column"] = (first[ki] if header else f"column {ki + 1}"), (first[vi] if header else f"column {vi + 1}")
    entries: dict[str, str] = {}
    for n, r in enumerate(rows, start=2 if header else 1):
        report["rows"] += 1
        if len(r) <= max(ki, vi):
            report["skipped"] += 1
            report["errors"].append(f"row {n}: fewer than {max(ki, vi) + 1} columns")
            continue
        k, v = r[ki].strip(), r[vi].strip()
        if not k:
            report["skipped"] += 1
            continue
        if k in entries:
            if entries[k] == v:
                report["duplicates"].append({"row": n, "key": k})
            else:
                report["conflicts"].append({"row": n, "key": k, "first": entries[k], "other": v})
            continue
        entries[k] = v
    if report["conflicts"]:
        report["errors"].append(f"{len(report['conflicts'])} key(s) map to different values")
    if len(report["errors"]) > 25:
        report["errors"] = report["errors"][:25] + [f"… {len(report['errors']) - 25} more"]
    report["entries"] = entries
    return report


def lookup_yaml(name: str, entries: dict) -> str:
    return yaml.safe_dump({"lookups": {name: entries}}, sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------------------------------- grid + decisions
def _when_text(w) -> str:
    if not w:
        return ""
    if "all" in w:
        return " and ".join(_when_text(x) for x in w["all"])
    if "any" in w:
        return " or ".join(_when_text(x) for x in w["any"])
    f = w.get("field", "?")
    for op in ("equals", "in", "not_in", "present", "prefix", "not_prefix", "in_lookup", "not_in_lookup"):
        if op in w:
            v = w[op]
            return f"{f} {op.replace('_', ' ')} {json.dumps(v) if isinstance(v, list) else v}"
    return json.dumps(w)


def _mapping_text(r: dict, lookups: dict) -> str:
    t = r.get("type")
    if t == "lookup_enrich":
        return f"{r.get('set_field')} from {r.get('lookup')} by {r.get('key_field')}"
    if r.get("map") and t != "field_map":
        items = list(r["map"].items())
        return ", ".join(f"{a} → {b}" for a, b in items[:3]) + (f" (+{len(items) - 3})" if len(items) > 3 else "")
    if r.get("lookup"):
        return f"lookup {r['lookup']} ({len(lookups.get(r['lookup'], {}))} entries)"
    if t == "key_map":
        return f"{r.get('strategy', 'prefix')} {r.get('prefix') or r.get('offset') or ''}".strip()
    if t == "number_range":
        return f"offset {r.get('offset', 0)}" + (f", prefix {r['prefix']}" if r.get("prefix") else "")
    if t == "currency_convert":
        return f"to {r.get('to')} ({len(r.get('rates') or {})} rates)"
    if t == "default":
        return ", ".join(f"{a} = {b}" for a, b in (r.get("set") or {}).items())
    if t == "conditional":
        then = r.get("then") or {}
        return ", ".join(f"{a} = {b}" for a, b in (then.get("set") or {}).items()) + (f" + {len(then.get('rules') or [])} nested" if then.get("rules") else "")
    if t == "field_map":
        return ", ".join(f"{a} → {b}" for a, b in (r.get("map") or {}).items())
    if t in ("reject", "skip"):
        return r.get("message", "")
    return ""


def _fields_of(r: dict) -> list[str]:
    t = r.get("type")
    if t == "default":
        return list((r.get("set") or {}).keys())
    if t == "field_map":
        return list((r.get("map") or {}).keys())
    if t == "lookup_enrich":
        return [r.get("set_field", "")]
    if t == "conditional":
        return list(((r.get("then") or {}).get("set") or {}).keys())
    try:
        return _rule_fields(r)
    except KeyError:
        return []


def decisions_summary(rs: RuleSet) -> dict:
    d = rs.rule_decisions or {}
    rules = rs.compiled.get("rules") or []
    counts = {"approved": 0, "rejected": 0, "pending": 0}
    for r in rules:
        dec = (d.get(r.get("id")) or {}).get("decision", "PENDING")
        counts[dec.lower() if dec.lower() in counts else "pending"] += 1
    return {**counts, "rules": len(rules)}


def rule_grid(rs: RuleSet) -> list[dict]:
    """One row per rule: what it does in words, where it applies, the mapping and condition, the decision."""
    lookups = rs.compiled.get("lookups") or {}
    d = rs.rule_decisions or {}
    out = []
    for i, r in enumerate(rs.compiled.get("rules") or []):
        dec = d.get(r.get("id")) or {}
        out.append({
            "position": i + 1,
            "id": r.get("id"),
            "type": r.get("type"),
            "description": r.get("description", ""),
            "tables": r.get("tables", ["*"]),
            "fields": _fields_of(r),
            "mapping": _mapping_text(r, lookups),
            "condition": _when_text(r.get("when")),
            "on_missing": r.get("on_missing", ""),
            "decision": dec.get("decision", "PENDING"),
            "decided_by": dec.get("by", ""),
            "decided_at": dec.get("at", ""),
            "comment": dec.get("comment", ""),
            "carried_from": dec.get("carried_from"),
            "rule": r,
        })
    return out


def decide_rule(session: Session, rs: RuleSet, rule_id: str, decision: str, actor: str, comment: str = "") -> dict:
    """Per-rule decision by an approver (four-eyes: not the author), only while the set is a draft."""
    if actor == rs.created_by:
        raise PermissionError("four-eyes principle: the creator of a ruleset cannot decide on its rules")
    if rs.status != "DRAFT":
        raise ValueError(f"ruleset is {rs.status}: decisions are frozen")
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    if not any(r.get("id") == rule_id for r in rs.compiled.get("rules") or []):
        raise LookupError(f"rule '{rule_id}' is not in this ruleset")
    if decision == "REJECTED" and not comment.strip():
        raise ValueError("a rejection needs a comment")
    decisions = dict(rs.rule_decisions or {})
    if decision == "PENDING":
        decisions.pop(rule_id, None)
    else:
        decisions[rule_id] = {"decision": decision, "by": actor, "at": datetime.now(timezone.utc).isoformat(), "comment": comment.strip()}
    rs.rule_decisions = decisions
    record_event(session, actor, "RULE_DECIDED", "RULESET", rs.id, {"rule": rule_id, "decision": decision, "comment": comment.strip()})
    session.flush()
    return decisions.get(rule_id) or {"decision": "PENDING"}


def carry_decisions(previous: RuleSet, rules: list[dict]) -> dict:
    """Decisions of the previous version that still apply: a rule keeps its decision when its content is identical."""
    prev_rules = {r.get("id"): json.dumps(normalise_rule(r), sort_keys=True, default=str) for r in previous.compiled.get("rules") or []}
    carried = {}
    for r in rules:
        rid = r.get("id")
        dec = (previous.rule_decisions or {}).get(rid)
        if dec and prev_rules.get(rid) == json.dumps(normalise_rule(r), sort_keys=True, default=str):
            carried[rid] = {**dec, "carried_from": previous.version}
    return carried


def check_rule_decisions(rs: RuleSet) -> list[str]:
    """Rules rejected by an approver: the set cannot be approved while any remain."""
    d = rs.rule_decisions or {}
    return [r.get("id") for r in rs.compiled.get("rules") or [] if (d.get(r.get("id")) or {}).get("decision") == "REJECTED"]


def blanket_approve_rules(rs: RuleSet, approver: str, comment: str) -> int:
    """Approval of the set approves every rule still pending, recorded per rule so that the lineage names who."""
    d = dict(rs.rule_decisions or {})
    n = 0
    at = datetime.now(timezone.utc).isoformat()
    for r in rs.compiled.get("rules") or []:
        rid = r.get("id")
        if (d.get(rid) or {}).get("decision") != "APPROVED":
            d[rid] = {"decision": "APPROVED", "by": approver, "at": at, "comment": comment.strip() or "approved with the rule set", "with_ruleset": True}
            n += 1
    rs.rule_decisions = d
    return n


__all__ = ["RULE_TYPES", "DECISIONS", "compose_ruleset", "document_of", "parse_lookup_csv", "lookup_yaml", "rule_grid", "decisions_summary", "decide_rule", "carry_decisions", "check_rule_decisions", "blanket_approve_rules"]
