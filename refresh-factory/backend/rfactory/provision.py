"""Shared provisioning building blocks used by test data management and the lean client builder.

  subset_plan     dependency-complete plans for roots chosen by a business-scenario template, from the source (read-only)
  synthetic_plan  consistent generated objects for a target (no source involved)
  execute_plan    masking coverage check -> conflict analysis -> checkpointed load -> reconciliation release gate;
                  on failure the target is rolled back and ProvisionError explains why
"""
from __future__ import annotations

import uuid

from .dependency.planner import Plan, PlanInstance, Planner
from .dependency.registry import CONFIG_TYPES
from .delta.engine import merge_plans
from .masking.engine import MaskingEngine, MaskingPolicy, discover_sensitive
from .reconcile.validator import reconcile
from .sap.adapter import SapSystem
from .sap.ddic import TABLES
from .sap.synthetic import SimulatedSap
from .selective.conflicts import analyze
from .selective.manifest import Manifest, Scope
from .tdm import generator
from .tdm.templates import find_candidates


class ProvisionError(RuntimeError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def org_scope(svc, source, tpl, params: dict) -> dict:
    """Keep organisation-level rows to the requested company code (plants for materials)."""
    cc = params.get("company_code", "1000")
    if tpl.root_type == "MATERIAL":
        return {"plants": [p["WERKS"] for p in source.select("T001W") if p["BUKRS"] == cc]}
    return {"company_codes": [cc]}


def subset_plan(svc, *, tpl, n: int, params: dict, source_id: str, target_id: str, taken: set[str], notes: list[str],
                check_target_config: bool = True, label: str = "subset", target_reader=None,
                family: str | None = None) -> tuple[Plan, dict, dict]:
    """`target_reader`/`family` let callers plan against a scratch target (estimates, builds that have no client yet)."""
    src, tgt = svc.source_view(source_id), target_reader or svc.adapters[target_id]
    reg = svc.registries[family or svc.system(target_id).family]
    hdr = reg.types[tpl.root_type].header
    cands = [c for c in find_candidates(tpl, src, params)
             if f"{tpl.root_type}:{c['key']}" not in taken and tgt.get(hdr, tuple(c["key"].split("/"))) is None]
    if not cands:
        raise ProvisionError(["no matching, not-yet-present business objects exist in the source"])
    manifest = Manifest(name=label, source_system_id=source_id, target_system_id=target_id, scope=Scope(object_type=tpl.root_type))
    planner, plans, per_root, attrs = Planner(src, reg), [], {}, {}
    for c in cands:
        if len(plans) >= n:
            break
        m = manifest.model_copy(update={"scope": Scope(object_type=tpl.root_type, explicit_keys=[c["key"]], **org_scope(svc, src, tpl, params)),
                                        "include_downstream": list(tpl.include_downstream)})
        pl = planner.build(m)
        if pl.blocking:
            continue
        if check_target_config:
            missing = sorted(f"{t}:{code}" for t, codes in pl.config_refs.items() for code in codes
                             if tgt.get(CONFIG_TYPES[t][0], (code,)) is None)
            if missing:  # selective copy never copies customizing: do not pick roots the target cannot host
                notes.append(f"candidate {c['key']} skipped: target lacks customizing {', '.join(missing)}")
                continue
        key = f"{tpl.root_type}:{c['key']}"
        plans.append(pl); per_root[key] = set(pl.instances); attrs[key] = c["attrs"]
    if not plans:
        raise ProvisionError(["no candidate could be provisioned (blocking dependency problems in the source, or customizing missing in the target)"])
    return merge_plans(plans, planner, f"{label}-{uuid.uuid4().hex[:6]}"), per_root, attrs


def _attrs(inst: PlanInstance, params: dict) -> dict:
    a = {"company_code": params.get("company_code", "1000")}
    if inst.type == "SALES_ORDER":
        h = inst.rows["VBAK"][0]
        a.update(customer=h["KUNNR"], net_value=h["NETWR"], currency=h["WAERK"], items=len(inst.rows["VBAP"]))
    elif inst.type == "PURCHASE_ORDER":
        a.update(vendor=inst.rows["EKKO"][0]["LIFNR"], items=len(inst.rows["EKPO"]))
    return a


def synthetic_plan(svc, *, tpl, n: int, params: dict, target_id: str, reader=None) -> tuple[Plan, dict, dict, SimulatedSap]:
    """`reader` lets callers plan against a scratch adapter (estimates); default is the real target."""
    tgt = reader or svc.adapters[target_id]
    family = svc.system(target_id).family
    try:
        plan, roots = generator.generate(tpl.synthetic_stage, tgt, family, n, params, seed=int(uuid.uuid4().int % 10**6))
    except generator.GenError as e:
        raise ProvisionError([str(e)])
    per_root: dict[str, set[str]] = {}
    for r in roots:  # closure of a synthetic root: itself, what it requires, and the downstream documents that require it
        seen, grew = {r}, True
        while grew:
            grew = False
            for i, x in plan.instances.items():
                if i not in seen and any(q in seen for q in x.requires):
                    seen.add(i); grew = True
            for i in list(seen):
                for q in plan.instances[i].requires:
                    if q in plan.instances and q not in seen:
                        seen.add(q); grew = True
        per_root[r] = seen
    attrs = {r: _attrs(plan.instances[r], params) for r in roots}
    gen = {t: [] for t in TABLES}
    for i in plan.instances.values():
        for t, rows in i.rows.items():
            gen[t] += rows
    return plan, per_root, attrs, SimulatedSap(SapSystem(id="gen", sid="GEN", client="000", role="SBX"), gen)


def plan_rows(plan: Plan) -> dict[str, list[dict]]:
    return {t: [r for i in plan.instances.values() for r in i.rows.get(t, [])] for t in {t for i in plan.instances.values() for t in i.rows}}


def execute_plan(svc, *, label: str, plan: Plan, manifest: Manifest, reg, target_id: str, mask_policy: MaskingPolicy,
                 mask_key: bytes | None, recon_source, actor: str, fault_injector=None):
    tgt = svc.adapters[target_id]
    engine = MaskingEngine(mask_policy, persistent_key=mask_key)
    discovered = discover_sensitive(plan_rows(plan))
    cov = engine.coverage(discovered)
    if not cov["complete"]:
        raise ProvisionError(["sensitive fields without a masking rule in the approved policy: "
                              + ", ".join(f"{m['table']}.{m['field']}" for m in cov["missing"])])
    sens = engine.policy.fields() | {(d["table"], d["field"]) for d in discovered}
    report = analyze(plan, tgt, manifest, reg, sens)
    if report.blocking:
        raise ProvisionError([f.message for f in report.blocking][:5])
    svc.executor.reg = reg
    run = svc.executor.new_run(label, plan, report)
    svc.runs[run.id] = run
    tgt.fault_injector = fault_injector
    try:
        svc.executor.execute(run, plan, report, engine, tgt, actor)
    finally:
        tgt.fault_injector = None
    if run.status != "COMPLETED":
        svc.executor.rollback(run, tgt, actor)
        raise ProvisionError([f"load failed and was rolled back: {run.error}"])
    run.reconciliation = reconcile(run, plan, recon_source, tgt, engine, reg, discovered, report.row_exclusions)
    run.release = run.reconciliation["release"]
    if run.release != "RELEASED":
        svc.executor.rollback(run, tgt, actor)
        raise ProvisionError([f"release gate failed ({', '.join(run.reconciliation['failed'])}); target rolled back"])
    return run, report
