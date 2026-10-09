"""Declarative, version-controlled transformation rule language (YAML) and deterministic engine.

Rule types: org_reassign, value_map, key_map, number_range, currency_convert, default, conditional,
field_map, lookup_enrich, reject. Every applied rule leaves a lineage entry; every failure is an
exception, never a silent pass-through. See docs/05-transformation-rule-dsl.md.
"""
from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from typing import Any

import yaml

from ..catalog.tables import TABLES, record_key

RULE_TYPES = ("org_reassign", "value_map", "key_map", "number_range", "currency_convert", "default", "conditional", "field_map", "lookup_enrich", "reject")


@dataclass
class RuleError(Exception):
    rule_id: str
    message: str

    def __str__(self):
        return f"[{self.rule_id}] {self.message}"


@dataclass
class Lineage:
    rule_id: str
    field: str
    before: Any
    after: Any

    def as_dict(self):
        return {"rule": self.rule_id, "field": self.field, "from": self.before, "to": self.after}


@dataclass
class CompiledRuleSet:
    name: str
    version: int
    description: str
    applies_to: dict
    lookups: dict[str, dict]
    rules: list[dict]
    tests: list[dict] = field(default_factory=list)
    content_hash: str = ""

    def rules_for(self, table: str) -> list[dict]:
        return [r for r in self.rules if any(fnmatch.fnmatch(table, pat) for pat in r.get("tables", ["*"]))]


# ------------------------------------------------------------------------------ compile / validate
def parse_ruleset(source_yaml: str) -> CompiledRuleSet:
    doc = yaml.safe_load(source_yaml) or {}
    if not isinstance(doc, dict) or "ruleset" not in doc:
        raise ValueError("ruleset YAML must be a mapping with a 'ruleset' name")
    rules = doc.get("rules") or []
    for r in rules:
        r.setdefault("tables", ["*"])
        if isinstance(r["tables"], str):
            r["tables"] = [r["tables"]]
    return CompiledRuleSet(
        name=str(doc["ruleset"]),
        version=int(doc.get("version", 1)),
        description=str(doc.get("description", "")),
        applies_to=doc.get("applies_to") or {},
        lookups={k: {str(a): b for a, b in (v or {}).items()} for k, v in (doc.get("lookups") or {}).items()},
        rules=rules,
        tests=doc.get("tests") or [],
        content_hash=hashlib.sha256(source_yaml.encode()).hexdigest(),
    )


def validate_ruleset(rs: CompiledRuleSet) -> dict:
    errors, warnings = [], []
    seen = set()
    for r in rs.rules:
        rid = r.get("id")
        if not rid:
            errors.append("rule without id")
            continue
        if rid in seen:
            errors.append(f"[{rid}] duplicate rule id")
        seen.add(rid)
        t = r.get("type")
        if t not in RULE_TYPES:
            errors.append(f"[{rid}] unknown rule type '{t}'")
            continue
        fields = _rule_fields(r)
        explicit = [pat for pat in r["tables"] if "*" not in pat]
        for pat in explicit:
            if pat not in TABLES:
                errors.append(f"[{rid}] unknown table '{pat}'")
        known = [pat for pat in explicit if pat in TABLES]
        if known and t not in ("field_map", "default", "lookup_enrich", "conditional"):
            for f in fields:
                if not any(f in TABLES[pat].fields for pat in known):
                    errors.append(f"[{rid}] field '{f}' does not exist in any of {known}")
        if t in ("value_map", "lookup_enrich") and "lookup" in r and r["lookup"] not in rs.lookups:
            errors.append(f"[{rid}] lookup '{r['lookup']}' is not defined")
        if t in ("org_reassign", "value_map") and not (r.get("map") or r.get("lookup")):
            errors.append(f"[{rid}] requires 'map' or 'lookup'")
        if t == "key_map" and r.get("strategy", "prefix") not in ("prefix", "offset", "lookup"):
            errors.append(f"[{rid}] unknown key_map strategy")
        if t == "key_map" and r.get("strategy", "prefix") == "prefix" and not r.get("prefix"):
            errors.append(f"[{rid}] prefix strategy requires 'prefix'")
        if t == "number_range" and not isinstance(r.get("offset", 0), int):
            errors.append(f"[{rid}] number_range offset must be integer")
        if t == "currency_convert" and not r.get("rates"):
            errors.append(f"[{rid}] currency_convert requires 'rates'")
        if t in ("conditional", "reject") and not r.get("when"):
            errors.append(f"[{rid}] requires 'when'")
        if t == "default" and not r.get("set"):
            errors.append(f"[{rid}] requires 'set'")
        if t in ("value_map", "org_reassign"):
            m = r.get("map") or rs.lookups.get(r.get("lookup"), {})
            if set(m.keys()) & set(map(str, m.values())):
                warnings.append(f"[{rid}] mapping is not idempotent (some targets are also sources)")
        if t == "key_map" and r.get("strategy", "prefix") == "prefix":
            warnings.append(f"[{rid}] prefix key mapping is not idempotent; the engine applies it exactly once per record")
    tests = run_tests(rs) if not errors else {"passed": 0, "failed": 0, "results": []}
    if tests["failed"]:
        errors.append(f"{tests['failed']} embedded test(s) failed")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "tests": tests, "rule_count": len(rs.rules)}


def _rule_fields(r: dict) -> list[str]:
    t = r["type"]
    if t == "org_reassign":
        return [r.get("field", "BUKRS")] + list(r.get("also_fields", []))
    if t in ("value_map", "key_map", "number_range", "currency_convert"):
        return list(r.get("fields", []) or ([r["field"]] if r.get("field") else []))
    return []


# ------------------------------------------------------------------------------ evaluation helpers
def _match(when: dict, rec: dict) -> bool:
    if not when:
        return True
    if "all" in when:
        return all(_match(w, rec) for w in when["all"])
    if "any" in when:
        return any(_match(w, rec) for w in when["any"])
    f = when.get("field")
    v = rec.get(f)
    if "equals" in when:
        return str(v) == str(when["equals"])
    if "in" in when:
        return str(v) in {str(x) for x in when["in"]}
    if "not_in" in when:
        return str(v) not in {str(x) for x in when["not_in"]}
    if "present" in when:
        return bool(v) == bool(when["present"])
    if "prefix" in when:
        return str(v).startswith(str(when["prefix"]))
    return True


def apply_rule(rule: dict, rs: CompiledRuleSet, table: str, rec: dict, lineage: list[Lineage]) -> None:
    rid, t = rule["id"], rule["type"]
    if not _match(rule.get("when", {}), rec):
        return
    if t in ("org_reassign", "value_map"):
        mapping = rule.get("map") or rs.lookups[rule["lookup"]]
        mapping = {str(k): v for k, v in mapping.items()}
        fields = _rule_fields(rule)
        on_missing = rule.get("on_missing", "passthrough")
        for f in fields:
            if f not in rec:
                continue
            v = rec.get(f)
            if v in ("", None):
                continue
            if str(v) in mapping:
                nv = mapping[str(v)]
                if nv != v:
                    lineage.append(Lineage(rid, f, v, nv))
                    rec[f] = nv
            elif on_missing == "error":
                raise RuleError(rid, f"no mapping for {f}={v!r} in {table}")
            elif on_missing == "default" and "default" in rule:
                lineage.append(Lineage(rid, f, v, rule["default"]))
                rec[f] = rule["default"]
    elif t == "key_map":
        strategy = rule.get("strategy", "prefix")
        for f in _rule_fields(rule):
            v = rec.get(f)
            if v in ("", None) or f not in rec:
                continue
            if strategy == "prefix":
                nv = f"{rule['prefix']}{v}"
            elif strategy == "offset":
                nv = str(int(v) + int(rule["offset"]))
            else:
                m = rs.lookups[rule["lookup"]]
                if str(v) not in m:
                    if rule.get("on_missing", "error") == "error":
                        raise RuleError(rid, f"no key mapping for {f}={v!r}")
                    continue
                nv = m[str(v)]
            lineage.append(Lineage(rid, f, v, nv))
            rec[f] = nv
            if rule.get("emit_field"):
                rec[rule["emit_field"]] = nv
    elif t == "number_range":
        for f in _rule_fields(rule):
            v = rec.get(f)
            if v in ("", None) or f not in rec:
                continue
            try:
                nv = str(int(v) + int(rule.get("offset", 0)))
            except ValueError:
                raise RuleError(rid, f"{f}={v!r} is not numeric") from None
            if rule.get("prefix"):
                nv = f"{rule['prefix']}{nv}"
            lineage.append(Lineage(rid, f, v, nv))
            rec[f] = nv
    elif t == "currency_convert":
        cur_field = rule.get("currency_field", "WAERS")
        cur = rec.get(cur_field)
        target = rule["to"]
        if cur and cur != target:
            rate = rule["rates"].get(f"{cur}->{target}")
            if rate is None:
                raise RuleError(rid, f"no rate for {cur}->{target}")
            for f in _rule_fields(rule):
                if f in rec and rec[f] not in ("", None):
                    nv = round(float(rec[f]) * float(rate), 2)
                    lineage.append(Lineage(rid, f, rec[f], nv))
                    rec[f] = nv
            lineage.append(Lineage(rid, cur_field, cur, target))
            rec[cur_field] = target
    elif t == "default":
        for f, v in rule["set"].items():
            if rec.get(f) in ("", None) or rule.get("overwrite"):
                lineage.append(Lineage(rid, f, rec.get(f), v))
                rec[f] = v
    elif t == "conditional":
        then = rule.get("then", {})
        for f, v in (then.get("set") or {}).items():
            lineage.append(Lineage(rid, f, rec.get(f), v))
            rec[f] = v
        for sub in then.get("rules", []):
            sub = {**sub, "id": f"{rid}/{sub.get('id', sub['type'])}"}
            apply_rule(sub, rs, table, rec, lineage)
    elif t == "field_map":
        for src, dst in rule["map"].items():
            if src in rec:
                lineage.append(Lineage(rid, dst, rec.get(dst), rec[src]))
                rec[dst] = rec[src]
                if rule.get("drop_source", True):
                    del rec[src]
    elif t == "lookup_enrich":
        key = rec.get(rule["key_field"])
        m = rs.lookups[rule["lookup"]]
        if key is not None and str(key) in m:
            lineage.append(Lineage(rid, rule["set_field"], rec.get(rule["set_field"]), m[str(key)]))
            rec[rule["set_field"]] = m[str(key)]
        elif rule.get("on_missing") == "error":
            raise RuleError(rid, f"no enrichment for {rule['key_field']}={key!r}")
    elif t == "reject":
        raise RuleError(rid, rule.get("message", "record rejected by rule"))


def transform_record(rs: CompiledRuleSet, table: str, record: dict) -> tuple[dict, list[dict]]:
    """Deterministic: same ruleset + same input -> same output. Raises RuleError on rejection."""
    rec = dict(record)
    lineage: list[Lineage] = []
    for rule in rs.rules_for(table):
        apply_rule(rule, rs, table, rec, lineage)
    return rec, [l.as_dict() for l in lineage]


def target_key(table: str, rec: dict) -> str:
    return record_key(table, rec) if table in TABLES else "|".join(str(v) for v in list(rec.values())[:3])


# ------------------------------------------------------------------------------ tests / dry run
def run_tests(rs: CompiledRuleSet) -> dict:
    results = []
    for i, case in enumerate(rs.tests):
        name = case.get("name", f"case-{i + 1}")
        table = case["table"]
        try:
            out, lineage = transform_record(rs, table, case["input"])
            if case.get("expect_reject"):
                ok, detail = False, "expected rejection but record passed"
            else:
                exp = case.get("expected", {})
                mism = {k: (out.get(k), v) for k, v in exp.items() if out.get(k) != v}
                ok, detail = not mism, (f"mismatches: {mism}" if mism else "ok")
        except RuleError as e:
            ok = bool(case.get("expect_reject"))
            detail = str(e)
        results.append({"name": name, "ok": ok, "detail": detail})
    return {"passed": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"]), "results": results}


def dry_run(rs: CompiledRuleSet, samples: dict[str, list[dict]], limit: int = 25) -> dict:
    out = {"tables": {}, "exceptions": [], "rule_impact": {}, "records": 0, "changed": 0}
    impact: dict[str, int] = {}
    for table, rows in samples.items():
        tbl = {"records": 0, "changed": 0, "fields_changed": {}, "samples": []}
        for rec in rows:
            out["records"] += 1
            tbl["records"] += 1
            try:
                new, lineage = transform_record(rs, table, rec)
            except RuleError as e:
                out["exceptions"].append({"table": table, "key": record_key(table, rec) if table in TABLES else "", "rule": e.rule_id, "message": e.message})
                continue
            if lineage:
                out["changed"] += 1
                tbl["changed"] += 1
                for l in lineage:
                    tbl["fields_changed"][l["field"]] = tbl["fields_changed"].get(l["field"], 0) + 1
                    impact[l["rule"]] = impact.get(l["rule"], 0) + 1
                if len(tbl["samples"]) < limit:
                    tbl["samples"].append({"before": rec, "after": new, "lineage": lineage})
        out["tables"][table] = tbl
    out["rule_impact"] = impact
    return out
