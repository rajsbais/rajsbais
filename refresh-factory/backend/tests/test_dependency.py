import pytest

from rfactory.dependency.planner import Planner
from rfactory.dependency.registry import ObjectType, Registry, Relationship, RelKind, TableLink, find_cycles
from rfactory.sap.adapter import ReadOnlyView
from .conftest import ALICE, make_project


def test_recursive_expansion_is_dependency_complete(svc):
    p = make_project(svc)
    svc.build_plan(ALICE, p.id)
    plan = p.plan
    assert not plan.blocking
    types = {i.type for i in plan.instances.values()}
    assert {"SALES_ORDER", "CUSTOMER", "MATERIAL", "DELIVERY", "BILLING", "FI_DOCUMENT"} <= types
    for inst in plan.instances.values():
        assert all(r in plan.instances for r in inst.requires), f"{inst.id} requires something outside the plan"
    # order, customers and materials of every root were pulled in
    for inst in plan.instances.values():
        if inst.type == "SALES_ORDER":
            assert f"CUSTOMER:{inst.rows['VBAK'][0]['KUNNR']}" in plan.instances


def test_load_order_respects_dependencies(svc):
    p = make_project(svc)
    svc.build_plan(ALICE, p.id)
    pos = {iid: n for n, iid in enumerate(p.plan.order)}
    for iid, inst in p.plan.instances.items():
        for r in inst.requires:
            assert pos[r] < pos[iid], f"{r} must load before {iid}"


def test_downstream_only_when_requested(svc):
    p = make_project(svc, downstream=())
    svc.build_plan(ALICE, p.id)
    assert {i.type for i in p.plan.instances.values()} == {"SALES_ORDER", "CUSTOMER", "MATERIAL"}


def test_cross_company_reference_and_shared_master_flagged(svc):
    p = make_project(svc)
    svc.build_plan(ALICE, p.id)
    codes = [i.code for i in p.plan.issues]
    assert "CROSS_COMPANY_REFERENCE" in codes
    assert "PLANT:2000" in {c for i in p.plan.instances.values() for c in i.configs}
    # customer 3 is maintained in both company codes; only the in-scope company-code row is copied
    for inst in p.plan.instances.values():
        if inst.type == "CUSTOMER":
            assert {r["BUKRS"] for r in inst.rows["KNB1"]} == {"1000"}


def test_date_and_org_filters(svc):
    p = make_project(svc, days=30)
    svc.build_plan(ALICE, p.id)
    ref = svc.adapters[svc.src_id].reference_date()
    for i in p.plan.instances.values():
        if i.origin == "ROOT":
            h = i.rows["VBAK"][0]
            assert h["BUKRS_VF"] == "1000" and (ref - __import__("datetime").timedelta(days=30)).isoformat() <= h["ERDAT"]


def test_unsupported_filter_and_empty_scope_block(svc):
    p = make_project(svc, object_type="CUSTOMER", plants=["1000"])
    svc.build_plan(ALICE, p.id)
    assert any(i.code == "UNSUPPORTED_FILTER" for i in p.plan.blocking)
    p2 = make_project(svc, company="9999")
    svc.build_plan(ALICE, p2.id)
    assert any(i.code == "EMPTY_SCOPE" for i in p2.plan.blocking)


def test_find_cycles_and_type_cycle_blocks_plan(svc):
    assert find_cycles({"A": {"B"}, "B": {"C"}, "C": {"A"}, "D": set()}) == [["A", "B", "C"]]
    assert find_cycles({"A": {"A"}}) == [["A"]]
    assert find_cycles({"A": {"B"}, "B": set()}) == []
    svc.registry.register_relationship(Relationship("bad: customer→order", "CUSTOMER", "SALES_ORDER", RelKind.REQUIRES, "KNA1", "KUNNR"))
    assert not svc.registry.validate()["ok"]
    p = make_project(svc)
    svc.build_plan(ALICE, p.id)
    assert any(i.code == "TYPE_CYCLE" for i in p.plan.blocking)


def test_instance_cycle_detected_and_blocked(svc):
    # a custom relationship that makes two *instances* depend on each other without a type cycle
    src = svc.adapters[svc.src_id]
    src.data["KNA1"][0]["LAND1"] = src.data["KNA1"][1]["KUNNR"]
    src.data["KNA1"][1]["LAND1"] = src.data["KNA1"][0]["KUNNR"]
    src._idx.clear()
    svc.registry.register_relationship(Relationship("customer→related customer", "CUSTOMER", "CUSTOMER", RelKind.REQUIRES, "KNA1", "LAND1"))
    p = make_project(svc, object_type="CUSTOMER", company="", days=0, explicit_keys=[src.data["KNA1"][0]["KUNNR"]])
    svc.build_plan(ALICE, p.id)
    assert p.plan.cycles and any(i.code == "INSTANCE_CYCLE" for i in p.plan.blocking)


def test_dangling_reference_blocks_and_source_validation_finds_orphans(svc):
    src = svc.adapters[svc.src_id]
    clean = Planner(ReadOnlyView(src), svc.registry).validate_relationships()
    assert clean["orphans"] == 0 and clean["dangling"] == 0
    victim = next(r for r in src.data["VBAK"] if r["BUKRS_VF"] == "1000")
    src.data["KNA1"] = [r for r in src.data["KNA1"] if r["KUNNR"] != victim["KUNNR"]]
    src.data["VBAK"] = [r for r in src.data["VBAK"] if r["VBELN"] != victim["VBELN"]]
    src._idx.clear()
    dirty = Planner(ReadOnlyView(src), svc.registry).validate_relationships()
    assert dirty["orphans"] > 0           # VBAP items without header
    assert dirty["dangling"] > 0          # documents pointing at the deleted customer / order
    p = make_project(svc, days=0)
    svc.build_plan(ALICE, p.id)
    assert any(i.code == "DANGLING_REFERENCE" for i in p.plan.blocking)


def test_custom_object_extension(svc):
    reg = Registry()
    reg.register_object_type(ObjectType("Z_PURCH_VIEW", "Custom PO view", "CUSTOM", "document",
                                        (TableLink("EKKO", None), TableLink("EKPO", "EKKO", (("EBELN", "EBELN"),))), ("EBELN",)))
    reg.register_relationship(Relationship("zpo→vendor", "Z_PURCH_VIEW", "VENDOR", RelKind.REQUIRES, "EKKO", "LIFNR"))
    assert reg.validate()["ok"]
    assert "Z_PURCH_VIEW" in {n["id"] for n in reg.graph()["nodes"]}


def test_read_only_view_cannot_write(svc):
    view = svc.source_view(svc.src_id)
    for op in ("upsert", "delete", "set_number_level", "assert_writable", "data"):
        with pytest.raises(AttributeError):
            getattr(view, op)
    with pytest.raises(AttributeError):
        view.anything = 1
    assert view.count("VBAK") > 0
