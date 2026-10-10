"""Manufacturing masters: bills of material, routings, work centers and batches as plant-scoped master data.

The synthetic landscape carries them consistently (components and work centers of the same plant, batch stock equal to
the plant stock), the discovery counts them per company code through the plant's valuation area, the graph links them
to the material, the production order, the work center and the cost center, the demo carve-out transfers the ones of
the SpinCo plants (renamed through the plant map) and reconciles them, and the migration cockpit catalogue resolves
each to its migration object."""

from sqlalchemy import select

from sdtf.catalog.business_objects import BUSINESS_OBJECTS, instance_company_codes
from sdtf.catalog.migration_objects import lookup
from sdtf.catalog.store import RecordStore
from sdtf.catalog.tables import TABLES
from sdtf.graph.service import build_graph
from sdtf.models import DiscoverySnapshot, ReconciliationResult, ScopeManifest
from sdtf.runtime.extraction import TRANSFER_CLASSES
from sdtf.synthetic.ecc_generator import LandscapeSpec, generate_landscape

MFG = ("MD.BillOfMaterial", "MD.Routing", "MD.WorkCenter", "MD.Batch")
MFG_TABLES = ("MAST", "STKO", "STPO", "MAPL", "PLKO", "PLPO", "CRHD", "CRCO", "MCH1", "MCHA", "MCHB")


def _plants(bo_id: str, r: dict, store: RecordStore) -> set[str]:
    """Plants of a manufacturing master from its assignment rows (the test's own reading of the model)."""
    if bo_id == "MD.BillOfMaterial":
        return {m["WERKS"] for m in store.lookup("MAST", "STLNR", r["STLNR"]) if m["STLAL"] == r["STLAL"]}
    if bo_id == "MD.Routing":
        return {r["WERKS"]} | {m["WERKS"] for m in store.lookup("MAPL", "PLNNR", r["PLNNR"]) if m["PLNAL"] == r["PLNAL"]}
    if bo_id == "MD.Batch":
        return {b["WERKS"] for b in store.lookup("MCHA", "CHARG", r["CHARG"]) if b["MATNR"] == r["MATNR"]}
    return {r["WERKS"]}


def test_generator_produces_consistent_manufacturing_masters():
    t = generate_landscape(LandscapeSpec(seed=21))
    marc = {(r["MATNR"], r["WERKS"]) for r in t["MARC"]}
    wc = {r["OBJID"]: r for r in t["CRHD"]}
    assert all(t[n] for n in MFG_TABLES)
    for m in t["MAST"]:
        assert (m["MATNR"], m["WERKS"]) in marc
        items = [i for i in t["STPO"] if i["STLNR"] == m["STLNR"]]
        assert 2 <= len(items) <= 3 and all((i["IDNRK"], m["WERKS"]) in marc and i["IDNRK"] != m["MATNR"] for i in items)
        assert any(r["PLNNR"] for r in t["MAPL"] if r["MATNR"] == m["MATNR"] and r["WERKS"] == m["WERKS"]), "every BOM material has a routing"
    for o in t["PLPO"]:
        assert o["ARBID"] in wc and wc[o["ARBID"]]["WERKS"] == o["WERKS"]
    cost_centers = {(k["KOKRS"], k["KOSTL"]) for k in t["CSKS"]}
    assert all((c["KOKRS"], c["KOSTL"]) in cost_centers for c in t["CRCO"])
    stock = {(r["MATNR"], r["WERKS"]): r["LABST"] for r in t["MARD"]}
    for b in t["MCHB"]:
        assert b["CLABS"] == stock[(b["MATNR"], b["WERKS"])] > 0
    assert {(b["CHARG"], b["MATNR"]) for b in t["MCHA"]} == {(b["CHARG"], b["MATNR"]) for b in t["MCH1"]}
    # the header key of every object type is the table key, so instance keys and record keys agree
    for bo in BUSINESS_OBJECTS.values():
        assert tuple(TABLES[bo.header_table].key_fields) == tuple(bo.key_fields), bo.id


def test_manufacturing_masters_are_plant_scoped_and_discovered(session, slice_result):
    store = RecordStore.load(session, slice_result["source_id"])
    cc_of_plant = {k["BWKEY"]: k["BUKRS"] for k in store.rows("T001K")}
    for bo_id in MFG:
        bo = BUSINESS_OBJECTS[bo_id]
        assert bo.org_scope == "PLANT" and bo.kind == "MASTER" and bo.load_methods[0].method == "MIGRATION_COCKPIT"
        for r in store.rows(bo.header_table)[:20]:
            plants = _plants(bo_id, r, store)
            assert plants and set(instance_company_codes(bo, r, store)) == {cc_of_plant[w] for w in plants}
    snap = session.execute(select(DiscoverySnapshot).where(DiscoverySnapshot.system_id == slice_result["source_id"]).order_by(DiscoverySnapshot.created_at.desc())).scalars().first()
    inv = snap.summary["business_objects"]
    for bo_id in MFG:
        assert inv[bo_id]["count"] == store.count(BUSINESS_OBJECTS[bo_id].header_table) > 0, bo_id
        assert set(inv[bo_id]["by_company_code"]) <= set(cc_of_plant.values()) and sum(inv[bo_id]["by_company_code"].values()) == inv[bo_id]["count"], bo_id


def test_graph_links_manufacturing_masters(session, slice_result):
    store = RecordStore.load(session, slice_result["source_id"])
    g = build_graph(store, slice_result["source_id"])
    by_type = g.stats()["nodes_by_type"]
    assert all(by_type.get(b, 0) > 0 for b in MFG)
    labels = {(g.nodes[e["from"]]["type"], g.nodes[e["to"]]["type"], e["attributes"].get("name") or e["attributes"].get("label")) for es in g.out_edges.values() for e in es}
    pairs = {(a, b) for a, b, _ in labels}
    assert {("MD.Material", "MD.BillOfMaterial"), ("MD.Material", "MD.Routing"), ("MD.Material", "MD.Batch"), ("MD.BillOfMaterial", "MD.Material"), ("MD.Routing", "MD.WorkCenter"), ("MD.WorkCenter", "CO.CostCenter"), ("PP.ProductionOrder", "MD.BillOfMaterial"), ("PP.ProductionOrder", "MD.Routing"), ("MD.Batch", "CFG.Plant")} <= pairs
    # a BOM references only components, never the materials it is assigned to
    for n, es in g.out_edges.items():
        if g.nodes[n]["type"] == "MD.BillOfMaterial":
            comps = {e["to"] for e in es if g.nodes[e["to"]]["type"] == "MD.Material"}
            stlnr, stlal = n.split(":", 1)[1].split("|")
            assigned = {"MD.Material:" + m["MATNR"] for m in store.lookup("MAST", "STLNR", stlnr) if m["STLAL"] == stlal}
            assert comps and assigned and not comps & assigned


def test_carve_out_transfers_the_spinco_manufacturing_masters(session, slice_result):
    manifest = session.get(ScopeManifest, slice_result["manifest_id"])
    cls = manifest.selection["classification"]
    src = RecordStore.load(session, slice_result["source_id"])
    spinco_plants = {k["BWKEY"] for k in src.rows("T001K") if k["BUKRS"] == "5000"}
    for bo_id in MFG:
        bo = BUSINESS_OBJECTS[bo_id]
        transferred = {k.split(":", 1)[1] for k, v in cls.items() if v["type"] == bo_id and v["classification"] in TRANSFER_CLASSES}
        expected = {bo.key_of(r) for r in src.rows(bo.header_table) if _plants(bo_id, r, src) & spinco_plants}
        assert transferred == expected and expected, bo_id
    tgt = RecordStore.load(session, slice_result["target_id"])
    renamed = {"5010": "SP10", "5020": "SP20"}
    for header in ("MAST", "MAPL", "CRHD", "MCHA"):
        expected = [r for r in src.rows(header) if r["WERKS"] in spinco_plants]
        got = tgt.rows(header)
        assert len(got) == len(expected) > 0, header
        assert {r["WERKS"] for r in got} <= set(renamed.values()), header
    assert all(tgt.count(t) > 0 for t in MFG_TABLES)
    # items only of transferred headers: every BOM item has its header, every operation its routing
    stlnr = {r["STLNR"] for r in tgt.rows("MAST")}
    assert {r["STLNR"] for r in tgt.rows("STPO")} == stlnr == {r["STLNR"] for r in tgt.rows("STKO")}
    objids = {r["OBJID"] for r in tgt.rows("CRHD")}
    assert {o["ARBID"] for o in tgt.rows("PLPO")} <= objids
    res = session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == slice_result["run_id"], ReconciliationResult.subject.in_(MFG_TABLES))).scalars().all()
    checks = {(r.subject, r.check_name): r.status for r in res}
    assert all(checks[(t, "record_count")] == "PASS" and checks[(t, "checksum")] == "PASS" for t in MFG_TABLES), checks
    for bo_id, ident in (("MD.BillOfMaterial", "SIF_BOM"), ("MD.Routing", "SIF_ROUTING"), ("MD.WorkCenter", "SIF_WORK_CENTER"), ("MD.Batch", "SIF_BATCH")):
        hit = lookup(bo_id, "2025")
        assert hit["status"] == "found" and hit["id"] == ident, bo_id
