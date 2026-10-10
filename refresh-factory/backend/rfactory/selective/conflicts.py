"""Pre-import target conflict analysis and policy resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..dependency.planner import Plan
from ..dependency.registry import CONFIG_TYPES, Registry, RelKind
from ..sap.adapter import TargetAdapter
from ..sap.ddic import NUMBER_RANGE_OBJECTS, TABLES
from .manifest import Manifest


class ConflictType(str, Enum):
    DUPLICATE_IDENTICAL = "DUPLICATE_IDENTICAL"
    DUPLICATE_DIFFERENT = "DUPLICATE_DIFFERENT"
    CONFIG_MISSING = "CONFIG_MISSING"
    NUMBER_RANGE = "NUMBER_RANGE"
    CHAIN_INCONSISTENT = "CHAIN_INCONSISTENT"
    TARGET_DRIFT = "TARGET_DRIFT"  # delta: an object this scenario loaded earlier was edited in the target since
    POLICY_INVALID = "POLICY_INVALID"


class Action(str, Enum):
    LOAD = "LOAD"
    SKIP = "SKIP"
    FAIL = "FAIL"
    UPDATE = "UPDATE"
    REPLACE = "REPLACE"
    REMAP = "REMAP"
    QUARANTINE = "QUARANTINE"


ALLOWED: dict[ConflictType, set[Action]] = {
    ConflictType.DUPLICATE_IDENTICAL: {Action.SKIP, Action.UPDATE, Action.FAIL},
    ConflictType.DUPLICATE_DIFFERENT: {Action.FAIL, Action.SKIP, Action.UPDATE, Action.REPLACE, Action.QUARANTINE, Action.REMAP},
    ConflictType.CONFIG_MISSING: {Action.FAIL, Action.QUARANTINE},
    ConflictType.TARGET_DRIFT: {Action.FAIL, Action.SKIP, Action.REPLACE},
}
DEFAULT_POLICY = {
    ConflictType.DUPLICATE_IDENTICAL: Action.SKIP,
    ConflictType.DUPLICATE_DIFFERENT: Action.FAIL,
    ConflictType.CONFIG_MISSING: Action.QUARANTINE,
    ConflictType.TARGET_DRIFT: Action.FAIL,
}
MASTER_TYPES = {"CUSTOMER", "VENDOR", "MATERIAL"}  # in-place update is only meaningful for master data
EXECUTES = {Action.LOAD, Action.UPDATE, Action.REPLACE}


@dataclass
class Finding:
    id: str
    type: ConflictType
    severity: str  # blocking | warning | info
    instance: str | None
    message: str
    action: Action | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self):
        return {"id": self.id, "type": self.type.value, "severity": self.severity, "instance": self.instance,
                "message": self.message, "action": self.action.value if self.action else None, "details": self.details}


@dataclass
class ConflictReport:
    findings: list[Finding]
    decisions: dict[str, Action]
    number_range_adjustments: dict[str, int]
    equivalent_in_target: set[str]
    row_exclusions: dict[str, dict[str, list[tuple]]] = field(default_factory=dict)  # iid -> table -> keys not to copy

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]

    @property
    def executable(self) -> set[str]:
        return {i for i, a in self.decisions.items() if a in EXECUTES}

    def to_dict(self) -> dict:
        by_dec: dict[str, int] = {}
        for a in self.decisions.values():
            by_dec[a.value] = by_dec.get(a.value, 0) + 1
        by_type: dict[str, int] = {}
        for f in self.findings:
            by_type[f.type.value] = by_type.get(f.type.value, 0) + 1
        return {"blocking": len(self.blocking) > 0, "findings": [f.to_dict() for f in self.findings],
                "by_type": by_type, "decisions": by_dec,
                "decision_by_instance": {k: v.value for k, v in self.decisions.items()},
                "number_range_adjustments": self.number_range_adjustments,
                "executable_instances": len(self.executable)}


def validate_policy(policy: dict[str, str]) -> list[str]:
    errs = []
    for t, a in policy.items():
        try:
            ct, ac = ConflictType(t), Action(a)
        except ValueError:
            errs.append(f"unknown conflict type/action: {t}={a}")
            continue
        if ct not in ALLOWED:
            errs.append(f"{t}: no policy applies to this conflict type")
        elif ac not in ALLOWED[ct]:
            errs.append(f"{t}: action {a} not allowed")
        elif ac == Action.REMAP:
            errs.append(f"{t}: REMAP (identifier remapping) is not implemented")
    return errs


def _diff(plan_rows: dict[str, list[dict]], target: TargetAdapter, ignore: set[tuple[str, str]]) -> list[dict]:
    diffs = []
    for table, rows in plan_rows.items():
        td = TABLES[table]
        for r in rows:
            tr = target.get(table, tuple(r[k] for k in td.keys))
            key = [r[k] for k in td.keys]
            if tr is None:
                diffs.append({"table": table, "key": key, "kind": "missing_in_target"})
                continue
            for f in td.fields:
                if f in td.keys or (table, f) in ignore:
                    continue
                if tr.get(f) != r.get(f):
                    diffs.append({"table": table, "key": key, "kind": "field", "field": f,
                                  "source": r.get(f), "target": tr.get(f)})
    return diffs


def analyze(plan: Plan, target: TargetAdapter, manifest: Manifest, registry: Registry,
            sensitive: set[tuple[str, str]], owned: set[str] | None = None,
            drift: dict[str, dict] | None = None) -> ConflictReport:
    """`owned`: instances a delta scenario loaded earlier and whose target copy is unchanged (re-load = REPLACE, no conflict).
    `drift`: owned instances whose target copy was edited since; resolved by the TARGET_DRIFT policy."""
    owned, drift = owned or set(), drift or {}
    findings: list[Finding] = []
    n = [0]

    def add(t, sev, inst, msg, action=None, **details) -> Finding:
        n[0] += 1
        f = Finding(f"F{n[0]:04d}", t, sev, inst, msg, action, details)
        findings.append(f)
        return f

    policy_errs = validate_policy(manifest.conflict_policy)
    for e in policy_errs:
        add(ConflictType.POLICY_INVALID, "blocking", None, e)
    policy = dict(DEFAULT_POLICY)
    for k, v in manifest.conflict_policy.items():
        try:
            policy[ConflictType(k)] = Action(v)
        except ValueError:
            pass

    decisions: dict[str, Action] = {}
    row_exclusions: dict[str, dict[str, list[tuple]]] = {}
    equivalent: set[str] = set()
    excluded_nonequiv: set[str] = set()

    for iid in plan.order:
        inst = plan.instances[iid]
        ot = registry.types[inst.type]
        decision = Action.LOAD
        override = manifest.instance_overrides.get(iid)

        # 1. configuration prerequisites in target
        missing = []
        for cfg in inst.configs:
            ctype, code = cfg.split(":", 1)
            table, _ = CONFIG_TYPES[ctype]
            if target.get(table, (code,)) is None:
                missing.append(cfg)
        view = inst.rows
        if missing and inst.type in MASTER_TYPES:
            # master data: only the organisation-level rows that need the missing customizing are left out
            drops: dict[str, list[tuple]] = {}
            for rel in registry.relationships:
                if rel.kind == RelKind.CONFIG and rel.src == inst.type and rel.via_table != ot.header:
                    codes = {c.split(":", 1)[1] for c in missing if c.startswith(rel.dst + ":")}
                    for r in inst.rows.get(rel.via_table, []):
                        if r.get(rel.via_field) in codes:
                            drops.setdefault(rel.via_table, []).append(tuple(r[k] for k in TABLES[rel.via_table].keys))
            row_exclusions[iid] = drops
            view = {t: [r for r in rows if tuple(r[k] for k in TABLES[t].keys) not in set(drops.get(t, []))]
                    for t, rows in inst.rows.items()}
            add(ConflictType.CONFIG_MISSING, "warning", iid,
                f"{iid}: {sum(len(v) for v in drops.values())} organisation-level row(s) not copied because the target lacks "
                f"{', '.join(missing)}; the master itself is still loaded", Action.SKIP, missing=missing,
                excluded={t: len(v) for t, v in drops.items()})
        elif missing:
            act = Action(override) if override else policy[ConflictType.CONFIG_MISSING]
            sev = "blocking" if act == Action.FAIL or act not in ALLOWED[ConflictType.CONFIG_MISSING] else "warning"
            add(ConflictType.CONFIG_MISSING, sev, iid,
                f"Target lacks customizing {', '.join(missing)} required by {iid}; selective copy never copies configuration",
                act, missing=missing)
            if act == Action.QUARANTINE:
                decision = Action.QUARANTINE

        # 2a. delta: objects this scenario already owns
        if decision == Action.LOAD and iid in drift:
            act = Action(override) if override else policy[ConflictType.TARGET_DRIFT]
            sev = "blocking" if act == Action.FAIL else "warning"
            add(ConflictType.TARGET_DRIFT, sev, iid,
                f"{iid} was changed in the target after the last refresh; the source also changed it", act, **drift[iid])
            decision = {Action.SKIP: Action.SKIP, Action.REPLACE: Action.REPLACE}.get(act, Action.FAIL)
            if decision == Action.SKIP:
                equivalent.add(iid)  # the previous version still exists in the target: dependants stay valid
        elif decision == Action.LOAD and iid in owned:
            decision = Action.REPLACE

        # 2. existing target data
        if decision == Action.LOAD:
            hdr = inst.rows[ot.header][0]
            existing = target.get(ot.header, tuple(hdr[k] for k in TABLES[ot.header].keys))
            if existing is not None:
                diffs = _diff(view, target, sensitive)
                owner = target.owner_of(ot.header, inst.key)
                if not diffs:
                    act = Action(override) if override else policy[ConflictType.DUPLICATE_IDENTICAL]
                    ok = act in ALLOWED[ConflictType.DUPLICATE_IDENTICAL] and not (
                        act == Action.UPDATE and inst.type not in MASTER_TYPES)
                    add(ConflictType.DUPLICATE_IDENTICAL, "info" if ok and act != Action.FAIL else "blocking", iid,
                        f"{iid} already exists in target with identical content", act)
                    if act == Action.SKIP:
                        decision = Action.SKIP; equivalent.add(iid)
                    elif act == Action.UPDATE and ok:
                        decision = Action.UPDATE
                    else:
                        decision = Action.FAIL
                else:
                    act = Action(override) if override else policy[ConflictType.DUPLICATE_DIFFERENT]
                    sev, msg = "blocking", f"{iid} exists in target with different content ({len(diffs)} differences)"
                    det = {"differences": diffs[:10], "difference_count": len(diffs), "owner": owner}
                    if act == Action.SKIP:
                        sev, decision = "warning", Action.SKIP
                    elif act == Action.QUARANTINE:
                        sev, decision = "warning", Action.QUARANTINE
                    elif act == Action.UPDATE:
                        if inst.type in MASTER_TYPES:
                            sev, decision = "warning", Action.UPDATE
                        else:
                            msg += "; in-place UPDATE is not supported for transactional documents"
                            decision = Action.FAIL
                    elif act == Action.REPLACE:
                        just = manifest.approved_exceptions.get(iid, "").strip()
                        if just:
                            sev, decision = "warning", Action.REPLACE
                            det["exception"] = just
                        else:
                            msg += "; REPLACE requires an approved exception" + (f" (object owned by {owner})" if owner else "")
                            decision = Action.FAIL
                    elif act == Action.REMAP:
                        msg += "; REMAP is not implemented"
                        decision = Action.FAIL
                    else:
                        decision = Action.FAIL
                    add(ConflictType.DUPLICATE_DIFFERENT, sev, iid, msg, act, **det)

        # 3. document-chain consistency: a dependency that was not loaded and is not equivalent in target
        if decision == Action.QUARANTINE and iid in owned:
            equivalent.add(iid)  # previous version stays in the target
        if decision in EXECUTES:
            broken = [d for d in inst.requires if d in excluded_nonequiv]
            if broken:
                add(ConflictType.CHAIN_INCONSISTENT, "warning", iid,
                    f"{iid} depends on {broken[0]}, which will not be loaded; loading it would create an inconsistent chain",
                    Action.QUARANTINE, cascaded_from=broken)
                decision = Action.QUARANTINE
        # A skipped *master* already exists in the target under the same key, so references to it stay valid.
        # A skipped *document* with a colliding number is a different business object: its dependents must not attach to it.
        existing_master = decision == Action.SKIP and inst.type in MASTER_TYPES
        if decision in (Action.SKIP, Action.QUARANTINE) and iid not in equivalent and not existing_master:
            excluded_nonequiv.add(iid)
        decisions[iid] = decision

    # 4. number ranges (only for objects that will actually be loaded)
    adjustments: dict[str, int] = {}
    for table, (obj, fld) in NUMBER_RANGE_OBJECTS.items():
        keys = [int(plan.instances[i].rows[table][0][fld]) for i, a in decisions.items()
                if a in EXECUTES and table in plan.instances[i].rows and plan.instances[i].rows[table]]
        if not keys:
            continue
        mx, level = max(keys), target.number_level(obj)
        if level is None:
            add(ConflictType.NUMBER_RANGE, "blocking", None, f"Target has no number range interval for {obj}", Action.FAIL, object=obj)
        elif mx > level:
            adjustments[obj] = mx
            add(ConflictType.NUMBER_RANGE, "warning", None,
                f"Loaded {table} numbers reach {mx} but target {obj} level is {level}; level will be raised to {mx} after load "
                f"to prevent future duplicate keys", Action.UPDATE, object=obj, planned_max=mx, target_level=level)

    return ConflictReport(findings, decisions, adjustments, equivalent, row_exclusions)
