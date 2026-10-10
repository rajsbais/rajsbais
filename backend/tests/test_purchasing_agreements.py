"""Scheduling agreements and purchase contracts: purchasing documents that share the purchase order's header table
and are told apart by the document category (BSTYP). The synthetic landscape carries them with schedule lines,
receipts and release orders; the catalogue types them through the header filter on every path that enumerates
headers (discovery, graph, cockpit grouping, delta typing); the demo carve-out transfers the SpinCo ones and the
cockpit export routes them to their migration objects."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from sdtf.catalog.business_objects import (
    BUSINESS_OBJECTS,
    header_rows,
    instance_status,
    matches_header,
    retype_by_header,
)
from sdtf.catalog.migration_objects import lookup
from sdtf.catalog.store import RecordStore
from sdtf.graph.service import build_graph
from sdtf.models import DiscoverySnapshot, ReconciliationResult, ScopeManifest
from sdtf.runtime.cockpit_export import export_cockpit_files
from sdtf.runtime.extraction import TRANSFER_CLASSES
from sdtf.runtime.loaders import object_of, regroup_by_header
from sdtf.synthetic.ecc_generator import LandscapeSpec, generate_landscape

PURCHASING = ("MM.PurchaseOrder", "MM.SchedulingAgreement", "MM.Contract")


def test_generator_produces_agreements_contracts_and_release_orders():
    t = generate_landscape(LandscapeSpec(seed=31))
    by_cat = {c: [h for h in t["EKKO"] if h["BSTYP"] == c] for c in ("F", "L", "K")}
    assert all(by_cat.values()) and {h["BSTYP"] for h in t["EKKO"]} == {"F", "L", "K"}
    items = {}
    for i in t["EKPO"]:
        items.setdefault(i["EBELN"], []).append(i)
    lines = {}
    for l in t["EKET"]:
        lines.setdefault(l["EBELN"], []).append(l)
    receipts = {(h["EBELN"], h["BELNR"]) for h in t["EKBE"] if h["VGABE"] == "1"}
    docs = {(m["MBLNR"], m["MJAHR"]) for m in t["MKPF"]}
    for sa in by_cat["L"]:
        assert items[sa["EBELN"]] and len(lines[sa["EBELN"]]) == 3 and sa["KDATB"] and sa["KDATE"] > sa["KDATB"]
        for l in lines[sa["EBELN"]]:
            assert 0 <= l["WEMNG"] <= l["MENGE"]
            if l["WEMNG"]:
                assert any(k[0] == sa["EBELN"] and (k[1], sa["GJAHR"]) in docs for k in receipts), "received schedule lines have a goods receipt"
    for ct in by_cat["K"]:
        assert items[ct["EBELN"]] and ct["KTWRT"] == round(sum(i["NETWR"] for i in items[ct["EBELN"]]), 2) and ct["BSART"] == "MK"
        assert ct["EBELN"] not in lines, "contracts carry no schedule lines"
    contracts = {c["EBELN"]: c for c in by_cat["K"]}
    releases = [i for i in t["EKPO"] if i["KONNR"]]
    assert releases and all(i["KONNR"] in contracts and any(ci["EBELP"] == i["KTPNR"] for ci in items[i["KONNR"]]) for i in releases)
    assert all(all(ci["LOEKZ"] != "L" for ci in items[i["KONNR"]]) for i in releases), "release orders draw on open contracts only"
    assert any(all(i["LOEKZ"] == "L" for i in items[c["EBELN"]]) for c in by_cat["K"]), "an ended contract exists"
    store = RecordStore.from_tables("sys", t)
    parts = {c: {h["EBELN"] for h in header_rows(store, BUSINESS_OBJECTS[b])} for c, b in zip("FLK", PURCHASING, strict=True)}
    assert parts["F"] | parts["L"] | parts["K"] == {h["EBELN"] for h in t["EKKO"]} and not (parts["F"] & parts["L"]) and not (parts["L"] & parts["K"])
    statuses = {instance_status(BUSINESS_OBJECTS["MM.SchedulingAgreement"], h, store) for h in by_cat["L"]}
    assert statuses == {"OPEN", "CLOSED"}
    assert {instance_status(BUSINESS_OBJECTS["MM.Contract"], h, store) for h in by_cat["K"]} == {"OPEN", "CLOSED"}


def test_shared_header_table_is_typed_by_the_header_image():
    po, sa, ct = BUSINESS_OBJECTS["MM.PurchaseOrder"], BUSINESS_OBJECTS["MM.SchedulingAgreement"], BUSINESS_OBJECTS["MM.Contract"]
    assert po.header_table == sa.header_table == ct.header_table == "EKKO" and po.header_filter == {"BSTYP": "F"}
    assert matches_header(sa, {"BSTYP": "L"}) and not matches_header(sa, {"BSTYP": "F"}) and matches_header(BUSINESS_OBJECTS["MD.Material"], {"MATNR": "X"})
    assert object_of("EKKO", "5500000001", {"EBELN": "5500000001", "BSTYP": "L"}) == ("MM.SchedulingAgreement", "5500000001")
    assert object_of("EKKO", "5500000002", {"EBELN": "5500000002", "BSTYP": "K"}) == ("MM.Contract", "5500000002")
    assert object_of("EKKO", "5500000003", {"EBELN": "5500000003", "BSTYP": "F"})[0] == "MM.PurchaseOrder" == object_of("EKKO", "5500000004")[0]
    assert object_of("EKPO", "5500000001|10", {"EBELN": "5500000001", "EBELP": 10}) == ("MM.PurchaseOrder", "5500000001"), "shared items carry no category: first type until the header is known"
    assert object_of("EKET", "5500000001|10|1", {"EBELN": "5500000001", "EBELP": 10, "ETENR": 1}) == ("MM.SchedulingAgreement", "5500000001"), "schedule lines belong to the agreement alone"
    assert retype_by_header("MM.PurchaseOrder", {"BSTYP": "L"}) == "MM.SchedulingAgreement" and retype_by_header("MM.PurchaseOrder", None) == "MM.PurchaseOrder" and retype_by_header("MD.Material", {"MATNR": "X"}) == "MD.Material"
    rec = lambda table: SimpleNamespace(table_name=table)  # noqa: E731
    groups = {("MM.SchedulingAgreement", "1"): [rec("EKKO")], ("MM.PurchaseOrder", "1"): [rec("EKPO"), rec("EKET")], ("MM.PurchaseOrder", "2"): [rec("EKKO"), rec("EKPO")], ("MM.PurchaseOrder", "3"): [rec("EKPO")], ("MD.Material", "M"): [rec("MARC")]}
    order = list(groups)
    groups, order = regroup_by_header(groups, order)
    assert [r.table_name for r in groups[("MM.SchedulingAgreement", "1")]] == ["EKKO", "EKPO", "EKET"] and ("MM.PurchaseOrder", "1") not in groups
    assert ("MM.PurchaseOrder", "3") in groups and ("MD.Material", "M") in groups and order == [("MM.SchedulingAgreement", "1"), ("MM.PurchaseOrder", "2"), ("MM.PurchaseOrder", "3"), ("MD.Material", "M")]


def test_agreements_are_discovered_and_linked(session, slice_result):
    store = RecordStore.load(session, slice_result["source_id"])
    snap = session.execute(select(DiscoverySnapshot).where(DiscoverySnapshot.system_id == slice_result["source_id"]).order_by(DiscoverySnapshot.created_at.desc())).scalars().first()
    inv = snap.summary["business_objects"]
    assert sum(inv[b]["count"] for b in PURCHASING) == store.count("EKKO") and all(inv[b]["count"] > 0 for b in PURCHASING)
    assert inv["MM.SchedulingAgreement"]["open"] < inv["MM.SchedulingAgreement"]["count"] and inv["MM.Contract"]["open"] < inv["MM.Contract"]["count"]
    assert set(inv["MM.SchedulingAgreement"]["by_company_code"]) <= {k["BUKRS"] for k in store.rows("T001K")} | {h["BUKRS"] for h in store.rows("EKKO")}
    g = build_graph(store, slice_result["source_id"])
    names = {(g.nodes[e["from"]]["type"], g.nodes[e["to"]]["type"], e["attributes"].get("name")) for es in g.out_edges.values() for e in es}
    assert {("MM.PurchaseOrder", "MM.Contract", "PurchaseOrder→Contract"), ("MM.SchedulingAgreement", "MM.MaterialDocument", "SchedulingAgreement→GoodsReceipt"), ("MM.SchedulingAgreement", "MD.Vendor", "SchedulingAgreement→Vendor"), ("MM.Contract", "MD.Material", "Contract→Material")} <= names
    # a purchase order never carries agreement edges and an agreement never purchase-order edges
    assert not any(a == "MM.SchedulingAgreement" and n.startswith("PurchaseOrder") or a == "MM.PurchaseOrder" and n.startswith("SchedulingAgreement") for a, _b, n in names)
    assert all(g.nodes[n]["type"] == "MM.Contract" for n in g.nodes if n.startswith("MM.Contract:")) and not any(g.nodes[n]["attributes"].get("missing") for n in g.nodes if n.startswith("MM.Contract:"))


def test_carve_out_transfers_and_exports_the_spinco_agreements(session, slice_result, tmp_path):
    from tests.test_cockpit_rounds import _wipe

    manifest = session.get(ScopeManifest, slice_result["manifest_id"])
    cls = manifest.selection["classification"]
    src = RecordStore.load(session, slice_result["source_id"])
    tgt = RecordStore.load(session, slice_result["target_id"])
    for bo_id in ("MM.SchedulingAgreement", "MM.Contract"):
        bo = BUSINESS_OBJECTS[bo_id]
        transferred = {k.split(":", 1)[1] for k, v in cls.items() if v["type"] == bo_id and v["classification"] in TRANSFER_CLASSES}
        expected = {h["EBELN"] for h in header_rows(src, bo) if h["BUKRS"] == "5000"}
        assert expected and expected <= transferred, bo_id
        got = header_rows(tgt, bo)
        assert len(got) == len(transferred) and {h["BUKRS"] for h in got} == {"SP01"}, bo_id
    assert tgt.count("EKET") > 0 and {l["EBELN"] for l in tgt.rows("EKET")} <= {h["EBELN"] for h in header_rows(tgt, BUSINESS_OBJECTS["MM.SchedulingAgreement"])}
    res = {(r.subject, r.check_name): r.status for r in session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == slice_result["run_id"], ReconciliationResult.subject.in_(["EKET", "EKKO", "EKPO", "MM.SchedulingAgreement", "MM.Contract"]))).scalars().all()}
    assert res[("EKET", "record_count")] == "PASS" and res[("EKET", "checksum")] == "PASS" and res[("MM.SchedulingAgreement", "open_document_validity")] == "PASS" and res[("MM.Contract", "open_document_validity")] == "PASS"
    for bo_id, name in (("MM.SchedulingAgreement", "Purchase scheduling agreement"), ("MM.Contract", "Purchase contract")):
        hit = lookup(bo_id, "2025")
        assert hit["status"] == "found" and hit["name"] == name
    assert lookup("MM.SchedulingAgreement", "1709")["status"] == "none"
    # the cockpit export groups the items and schedule lines with their agreement header, not with a purchase order
    _wipe(session, slice_result["run_id"])
    out = export_cockpit_files(session, slice_result["run_id"], out_dir=str(tmp_path), use_templates=False)
    objs = out["objects"]
    assert "Purchase scheduling agreement" in objs["MM.SchedulingAgreement"]["migration_object"] and "Purchase contract" in objs["MM.Contract"]["migration_object"]
    sa_instances = objs["MM.SchedulingAgreement"].get("instances")
    if sa_instances is not None:
        assert sa_instances == len(header_rows(tgt, BUSINESS_OBJECTS["MM.SchedulingAgreement"]))
    po_tables = set(objs["MM.PurchaseOrder"].get("tables") or objs["MM.PurchaseOrder"].get("rows_by_table") or [])
    assert "EKET" not in po_tables
    _wipe(session, slice_result["run_id"])


@pytest.mark.parametrize("bo_id", PURCHASING)
def test_purchasing_types_share_the_header_and_keep_their_items(bo_id):
    bo = BUSINESS_OBJECTS[bo_id]
    assert bo.header_table == "EKKO" and bo.key_fields == ("EBELN",) and bo.header_filter and "EKPO" in bo.item_tables
