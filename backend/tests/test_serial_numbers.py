"""Serial numbers as equipment records: the serialised materials of the synthetic landscape carry one equipment
record per serial number with its stock at a plant; the object is plant-scoped through the serial number stock,
discovered with company codes, linked to the material and the plant, transferred for the carved-out plants only
and resolved to the Equipment migration object."""

from sqlalchemy import select

from sdtf.catalog.business_objects import BUSINESS_OBJECTS, instance_company_codes
from sdtf.catalog.migration_objects import lookup
from sdtf.catalog.store import RecordStore
from sdtf.graph.service import build_graph
from sdtf.models import DiscoverySnapshot, ReconciliationResult, ScopeManifest
from sdtf.runtime.extraction import TRANSFER_CLASSES
from sdtf.synthetic.ecc_generator import LandscapeSpec, generate_landscape


def test_generator_serialises_materials_consistently():
    t = generate_landscape(LandscapeSpec(seed=41))
    assert t["EQUI"] and len(t["EQUI"]) == len(t["EQBS"])
    stock = {b["EQUNR"]: b for b in t["EQBS"]}
    marc = {(r["MATNR"], r["WERKS"]) for r in t["MARC"]}
    mtype = {r["MATNR"]: r["MTART"] for r in t["MARA"]}
    for e in t["EQUI"]:
        b = stock[e["EQUNR"]]
        assert b["MATNR"] == e["MATNR"] and b["SERNR"] == e["SERNR"] and (e["MATNR"], b["B_WERK"]) in marc and mtype[e["MATNR"]] == "FERT"
    assert len({(e["MATNR"], e["SERNR"]) for e in t["EQUI"]}) == len(t["EQUI"]), "serial numbers unique per material"
    assert len({e["MATNR"] for e in t["EQUI"]}) < len({m for m, _ in marc}), "only some materials are serialised"


def test_serial_numbers_are_discovered_and_linked(session, slice_result):
    store = RecordStore.load(session, slice_result["source_id"])
    bo = BUSINESS_OBJECTS["MD.Equipment"]
    cc_of_plant = {k["BWKEY"]: k["BUKRS"] for k in store.rows("T001K")}
    assert bo.org_scope == "PLANT" and bo.kind == "MASTER" and bo.item_tables == ("EQBS",)
    for e in store.rows("EQUI"):
        b = store.get("EQBS", EQUNR=e["EQUNR"])
        assert instance_company_codes(bo, e, store) == [cc_of_plant[b["B_WERK"]]]
    snap = session.execute(select(DiscoverySnapshot).where(DiscoverySnapshot.system_id == slice_result["source_id"]).order_by(DiscoverySnapshot.created_at.desc())).scalars().first()
    inv = snap.summary["business_objects"]["MD.Equipment"]
    assert inv["count"] == store.count("EQUI") > 0 and sum(inv["by_company_code"].values()) == inv["count"]
    g = build_graph(store, slice_result["source_id"])
    names = {(g.nodes[e["from"]]["type"], g.nodes[e["to"]]["type"], e["attributes"].get("name")) for es in g.out_edges.values() for e in es}
    assert {("MD.Material", "MD.Equipment", "Material→SerialNumber"), ("MD.Equipment", "MD.Material", "SerialNumber→Material"), ("MD.Equipment", "CFG.Plant", "SerialNumber→Plant")} <= names
    assert not any(g.nodes[n]["attributes"].get("missing") for n in g.nodes if n.startswith("MD.Equipment:"))


def test_carve_out_transfers_the_spinco_serial_numbers(session, slice_result):
    manifest = session.get(ScopeManifest, slice_result["manifest_id"])
    cls = manifest.selection["classification"]
    src = RecordStore.load(session, slice_result["source_id"])
    tgt = RecordStore.load(session, slice_result["target_id"])
    spinco_plants = {k["BWKEY"] for k in src.rows("T001K") if k["BUKRS"] == "5000"}
    expected = {b["EQUNR"] for b in src.rows("EQBS") if b["B_WERK"] in spinco_plants}
    transferred = {k.split(":", 1)[1] for k, v in cls.items() if v["type"] == "MD.Equipment" and v["classification"] in TRANSFER_CLASSES}
    assert expected and transferred == expected
    assert {e["EQUNR"] for e in tgt.rows("EQUI")} == expected and tgt.count("EQBS") == len(expected)
    assert {b["B_WERK"] for b in tgt.rows("EQBS")} <= {"SP10", "SP20"}, "the plant map renames the serial number stock's plant"
    res = {(r.subject, r.check_name): r.status for r in session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == slice_result["run_id"], ReconciliationResult.subject.in_(["EQUI", "EQBS"]))).scalars().all()}
    assert res[("EQUI", "record_count")] == "PASS" and res[("EQBS", "checksum")] == "PASS"
    hit = lookup("MD.Equipment", "2025")
    assert hit["status"] == "found" and hit["name"] == "Equipment" and hit["id"] == "SIF_EQUIPMENT"
