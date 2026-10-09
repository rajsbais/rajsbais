"""Scenario A4: two source systems consolidated into one target company code."""
import pytest
from sqlalchemy import select

from sdtf.catalog.dedup import dedup_lookups_for_source, find_duplicate_masters
from sdtf.catalog.store import RecordStore, import_tables
from sdtf.demo import approve_ruleset, create_target_shell, spinco_shell
from sdtf.discovery.service import discover_system
from sdtf.graph.service import build_graph, persist_graph
from sdtf.models import Project, RuleSet, SapRecord, SapSystem
from sdtf.rules.engine import parse_ruleset, validate_ruleset
from sdtf.rules.factory import generate_candidate_ruleset
from sdtf.runtime.merge import RunPrecondition, dedup_for_merge, plan_merge, start_merge_run
from sdtf.scope.models import ScopeDefinition, TargetOwnership
from sdtf.scope.service import apply_disposition, approve_manifest, create_manifest, pending_dispositions
from sdtf.synthetic.ecc_generator import LandscapeSpec, generate_landscape


@pytest.fixture(scope="module")
def merger(engine):
    from sdtf.db import session_scope

    with session_scope() as s:
        proj = Project(name="Merger: Alpha + Beta", scenario_type="MERGER", created_by="architect")
        s.add(proj)
        s.flush()
        sources = []
        for sid, seed in (("ALP", 101), ("BET", 202)):
            src = SapSystem(project_id=proj.id, sid=sid, client="100", role="SOURCE", product="ECC", release="6.0 EHP8", connector="SYNTHETIC", connector_status="SIMULATED", logical_system=f"{sid}CLNT100")
            s.add(src)
            s.flush()
            import_tables(s, src.id, generate_landscape(LandscapeSpec(seed=seed, scale=1)))
            store = RecordStore.load(s, src.id)
            discover_system(s, src, "architect", store)
            persist_graph(s, src.id, build_graph(store, src.id))
            sources.append(src)
        tgt = create_target_shell(s, proj, "MRG", shell=spinco_shell(bukrs="M100", plants=("M110", "M120", "M130", "M140"), name="Merged Industrial GmbH"))
        return {"project_id": proj.id, "sources": [x.id for x in sources], "target_id": tgt.id}


def _manifest(session, merger, src_id, name):
    src, tgt = session.get(SapSystem, src_id), session.get(SapSystem, merger["target_id"])
    i = merger["sources"].index(src_id)
    plants = {"1010": "M110", "1020": "M120"} if i == 0 else {"1010": "M130", "1020": "M140"}
    defn = ScopeDefinition(name=name, scenario_type="MERGER", source_system_id=src.id, target_system_id=tgt.id, company_codes=["1000"], target_ownership=TargetOwnership(company_code_map={"1000": "M100"}, plant_map=plants, controlling_area_map={"1000": "M100"}))
    m = create_manifest(session, merger["project_id"], defn, "architect")
    apply_disposition(session, m, pending_dispositions(m), "TRANSFER", "approver", "merge")
    approve_manifest(session, m, "approver")
    return m, defn


def _ruleset(session, project_id, yaml_src):
    rs = parse_ruleset(yaml_src)
    v = validate_ruleset(rs)
    assert v["ok"], v
    row = RuleSet(project_id=project_id, name=rs.name, version=1, content_hash=rs.content_hash, source_yaml=yaml_src, compiled={"rules": rs.rules}, validation=v, created_by="architect")
    session.add(row)
    session.flush()
    return approve_ruleset(session, row, "approver")


def test_duplicate_masters_detected_across_sources(session, merger):
    cands = find_duplicate_masters(session, merger["sources"])
    assert len(cands["customers"]) >= 5 and all(c["cross_system"] for c in cands["customers"])
    assert cands["vendors"] and cands["materials"]
    c = cands["customers"][0]
    assert c["survivor"]["system"] == merger["sources"][0] and c["duplicates"][0]["system"] == merger["sources"][1]
    lk = dedup_lookups_for_source(cands, merger["sources"][1], lambda kind, key: f"BP{key}" if kind != "materials" else key)
    assert set(lk) == {"customers", "vendors", "materials"} and all(v.startswith("BP") for v in lk["customers"].values())
    assert not dedup_lookups_for_source(cands, merger["sources"][0], lambda k, key: key), "the leading source has no duplicates to redirect"


def test_merge_plan_detects_collisions_then_runs_clean(session, merger):
    pid = merger["project_id"]
    m_a, d_a = _manifest(session, merger, merger["sources"][0], "alpha-1000")
    m_b, d_b = _manifest(session, merger, merger["sources"][1], "beta-1000")
    # naive: both sources with identical number ranges and BP prefixes -> collisions
    rs_a = _ruleset(session, pid, generate_candidate_ruleset(d_a, "S4HANA", name="alpha-naive"))
    rs_b_naive = _ruleset(session, pid, generate_candidate_ruleset(d_b, "S4HANA", name="beta-naive"))
    plan = plan_merge(session, pid, [{"manifest_id": m_a.id, "ruleset_id": rs_a.id}, {"manifest_id": m_b.id, "ruleset_id": rs_b_naive.id}])
    assert not plan["ready"] and plan["collisions"]["count"] > 0
    assert {"BKPF", "KNA1"} <= set(plan["collisions"]["by_table"]) and plan["company_codes_merged_from_several_sources"] == ["M100"]
    with pytest.raises(RunPrecondition, match="collision"):
        start_merge_run(session, pid, [{"manifest_id": m_a.id, "ruleset_id": rs_a.id}, {"manifest_id": m_b.id, "ruleset_id": rs_b_naive.id}], "operator")
    # resolved: per-source number ranges / prefixes and master-data dedup for the second source
    dd = dedup_for_merge(session, pid, {"manifest_id": m_a.id, "ruleset_id": rs_a.id}, merger["sources"][1])
    dedup = dd["lookups"]
    assert dd["candidates"]["customers"] > 0 and all(v.startswith("BP") for v in dedup["customers"].values())
    rs_b = _ruleset(session, pid, generate_candidate_ruleset(d_b, "S4HANA", name="beta-merge", source_index=1, dedup=dedup))
    plan = plan_merge(session, pid, [{"manifest_id": m_a.id, "ruleset_id": rs_a.id}, {"manifest_id": m_b.id, "ruleset_id": rs_b.id}])
    assert plan["ready"], plan["collisions"]["samples"][:5]
    assert plan["sources"][1]["skipped_by_rules"] >= 10, "duplicate masters of the second source are skipped"
    out = start_merge_run(session, pid, [{"manifest_id": m_a.id, "ruleset_id": rs_a.id}, {"manifest_id": m_b.id, "ruleset_id": rs_b.id}], "operator")
    assert [r["status"] for r in out["runs"]] == ["COMPLETED", "COMPLETED"]
    assert all(r["reconciliation"] == "PASS" for r in out["runs"]), out["runs"]
    assert out["financial"]["overall"] == "PASS", out["financial"]
    assert out["financial"]["sources"] == 2 and out["overall"] == "PASS"
    # the target holds one company code with documents from both sources in disjoint number ranges
    belnrs = [r[0] for r in session.execute(select(SapRecord.record_key).where(SapRecord.system_id == merger["target_id"], SapRecord.table_name == "BKPF"))]
    assert belnrs and all(k.startswith("M100|") for k in belnrs)
    nums = {int(k.split("|")[1]) // 10_000_000_000 for k in belnrs}
    assert nums == {0, 1}, nums
    # no duplicate customer masters: second source's customers reference the survivors
    kna1 = {r[0] for r in session.execute(select(SapRecord.record_key).where(SapRecord.system_id == merger["target_id"], SapRecord.table_name == "KNA1"))}
    for dup_key, survivor in dedup["customers"].items():
        assert f"BP2{dup_key}" not in kna1, "deduplicated customer must not be re-created under the second source's prefix"
        assert survivor in kna1, "survivor loaded by the leading source"
    assert any(k.startswith("BP2") for k in kna1), "non-duplicate customers of the second source keep their own prefix"
    # a merged target company code reconciles only at group level: member runs carry the group reference
    from sdtf.models import MigrationRun

    for r in out["runs"]:
        run = session.get(MigrationRun, r["id"])
        assert run.metrics["merge_group"] == out["merge_group"] and run.report["merge_group"]["financial"]["overall"] == "PASS"
