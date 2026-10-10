import pytest

from sdtf.catalog.store import RecordStore
from sdtf.graph.service import TraversalPolicy, build_graph, load_graph, traverse
from sdtf.scope.models import ScopeDefinition, TargetOwnership
from sdtf.scope.service import (
    apply_disposition,
    approve_manifest,
    compare_manifests,
    create_manifest,
    evaluate_scope,
    pending_dispositions,
    verify_manifest_integrity,
)
from sdtf.synthetic.ecc_generator import LandscapeSpec, generate_landscape


def test_graph_builds_document_chains():
    tables = generate_landscape(LandscapeSpec(seed=11))
    store = RecordStore.from_tables("sys", tables)
    g = build_graph(store, "sys")
    st = g.stats()
    assert st["edges_by_type"]["DOC_FLOW"] > 0 and st["edges_by_type"]["CROSS_COMPANY"] > 0 and st["edges_by_type"]["MASTER_REF"] > 0
    # Sales Order -> Delivery -> Billing -> Accounting Document -> Clearing chain is traversable
    so = next(n for n in g.nodes if n.startswith("SD.SalesOrder:") and any(e["to"].startswith("SD.Delivery:") for e in g.out_edges[n]))
    res = traverse(g, [so], TraversalPolicy())
    types = {g.nodes[n]["type"] for n in res.included}
    assert {"SD.SalesOrder", "SD.Delivery", "MD.Customer", "MD.Material"} <= types
    # every expanded node has an explanation
    assert all(t["reason"] for t in res.traces)
    assert any(t["policy"] == "SEED" for t in res.traces)


def test_traversal_policies_are_honoured():
    tables = generate_landscape(LandscapeSpec(seed=11))
    g = build_graph(RecordStore.from_tables("sys", tables), "sys")
    so = next(n for n in g.nodes if n.startswith("SD.SalesOrder:"))
    stop = traverse(g, [so], TraversalPolicy(edge_policies={"DOC_FLOW": "STOP", "MASTER_REF": "STOP", "ORG_OWNERSHIP": "STOP", "ACCOUNTING_REF": "STOP", "PARENT_CHILD": "STOP", "CROSS_COMPANY": "STOP"}))
    assert set(stop.included) == {so} and stop.stopped
    ref = traverse(g, [so], TraversalPolicy(edge_policies={"MASTER_REF": "REFERENCE", "DOC_FLOW": "STOP", "ORG_OWNERSHIP": "STOP"}))
    assert any(v == "REFERENCE" for v in ref.included.values())


def test_scope_evaluation_and_manifest_lifecycle(session, slice_result):
    src, tgt, project_id = slice_result["source_id"], slice_result["target_id"], slice_result["project_id"]
    defn = ScopeDefinition(name="lifecycle", source_system_id=src, target_system_id=tgt, company_codes=["5000"], target_ownership=TargetOwnership(company_code_map={"5000": "SP01"}))
    ev = evaluate_scope(session, defn)
    imp = ev["impact"]
    assert imp["objects_total"] > 0 and imp["objects_expanded"] > 0
    classes = set(imp["by_classification"])
    assert {"FULLY_TRANSFERRED", "SHARED_DUPLICATED", "REFERENCE_ONLY", "RETAINED_BY_SELLER", "PARTIALLY_TRANSFERRED"} <= classes
    # cross-company accounting documents are detected and flagged for approval
    cross = [n for n, c in ev["classification"].items() if c["type"] == "FI.AccountingDocument" and c["classification"] == "PARTIALLY_TRANSFERRED"]
    assert cross and all(ev["classification"][n]["requires_approval"] for n in cross)
    # counterpart documents of retained company codes are never transferred
    assert all(c["classification"] == "RETAINED_BY_SELLER" for c in ev["classification"].values() if c["type"] == "FI.AccountingDocument" and c["company_codes"] and c["company_codes"][0] != "5000")
    m1 = create_manifest(session, project_id, defn, "architect", ev)
    assert m1.version == 1 and m1.status == "DRAFT" and verify_manifest_integrity(m1)
    m2 = create_manifest(session, project_id, defn, "architect", ev)
    assert m2.version == 2
    # approval requires dispositions and four eyes
    with pytest.raises(ValueError):
        approve_manifest(session, m1, "approver")
    n = apply_disposition(session, m1, pending_dispositions(m1), "TRANSFER", "approver", "ok")
    assert n > 0 and not pending_dispositions(m1) and verify_manifest_integrity(m1)
    with pytest.raises(PermissionError):
        approve_manifest(session, m1, "architect")
    approve_manifest(session, m1, "approver")
    assert m1.status == "APPROVED"
    with pytest.raises(ValueError):
        apply_disposition(session, m1, [], "TRANSFER", "approver")
    # tamper detection
    m1.definition = {**m1.definition, "company_codes": ["5000", "1000"]}
    assert not verify_manifest_integrity(m1)
    session.rollback()


def test_what_if_comparison(session, slice_result):
    src, tgt, project_id = slice_result["source_id"], slice_result["target_id"], slice_result["project_id"]
    full = ScopeDefinition(name="whatif", source_system_id=src, target_system_id=tgt, company_codes=["5000"], historical_policy="FULL")
    open_only = ScopeDefinition(name="whatif", source_system_id=src, target_system_id=tgt, company_codes=["5000"], historical_policy="OPEN_ITEMS_AND_BALANCES", cross_company_policy="EXCLUDE")
    a = create_manifest(session, project_id, full, "architect")
    b = create_manifest(session, project_id, open_only, "architect")
    cmp = compare_manifests(a, b)
    assert cmp["reclassified"] > 0 and cmp["delta_bytes"] < 0
    assert b.impact["by_classification"].get("RETAINED_BY_SELLER", 0) > a.impact["by_classification"].get("RETAINED_BY_SELLER", 0)


def test_persisted_graph_roundtrip(session, slice_result):
    g = load_graph(session, slice_result["source_id"])
    assert g.stats()["nodes"] == slice_result["graph_stats"]["nodes"]
    assert g.stats()["edges"] == slice_result["graph_stats"]["edges"]
