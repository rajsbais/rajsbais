"""Carve-out deal templates (policies applied, deviations assessed, hive-down renumbering) and residual cleanup
plans: items from the candidates, decisions, four-eyes approval guarded by the transfer, execution on the simulated
source with the package written first, refusal on an add-on source; API and CLI."""
import os
import zipfile

import pytest
from sqlalchemy import select

from sdtf.carveout import cleanup as cu
from sdtf.carveout.deals import apply_deal, deal_assessment, deal_templates
from sdtf.carveout.service import cleanup_candidates, residual_exposure_report
from sdtf.catalog.store import RecordStore
from sdtf.cli import main as cli_main
from sdtf.models import AuditEvent, SapSystem, ScopeManifest
from sdtf.scope.models import ScopeDefinition

API = "/api/v1"


def test_deal_templates_apply_and_assess():
    ids = [d["id"] for d in deal_templates()]
    assert ids == ["ASSET_DEAL", "SHARE_DEAL", "HIVE_DOWN"] and all({"policies", "residual_rule", "obligations", "approvals"} <= set(d) for d in deal_templates())
    base = {"name": "x", "source_system_id": "s", "target_system_id": "t", "company_codes": ["5000"]}
    a = apply_deal(base, "asset_deal")
    assert a["deal_type"] == "ASSET_DEAL" and a["historical_policy"] == "OPEN_ITEMS_AND_BALANCES" and a["document_status"] == "OPEN_ONLY" and a["cross_company_policy"] == "REFERENCE" and "Deal type: Asset deal." in a["description"]
    s = apply_deal({**base, "historical_policy": "YEARS"}, "SHARE_DEAL")
    assert s["historical_policy"] == "YEARS" and s["document_status"] == "ALL"  # an explicit value is kept
    assess = deal_assessment(s)
    assert assess["deal_type"] == "SHARE_DEAL" and assess["residual_rule"] == "CLEANUP_AFTER_TSA" and [d["field"] for d in assess["deviations"]] == ["historical_policy"] and assess["deviations"][0]["template"] == "FULL" and "which history moves" in assess["deviations"][0]["consequence"]
    with pytest.raises(ValueError, match="new company code"):
        apply_deal(base, "HIVE_DOWN")
    h = apply_deal(base, "HIVE_DOWN", "NC01")
    assert h["target_ownership"]["company_code_map"] == {"5000": "NC01"} and ScopeDefinition(**h).deal_type == "HIVE_DOWN"
    assert deal_assessment({**h, "target_ownership": {"company_code_map": {"5000": "5000"}}})["deviations"][0]["field"] == "target_ownership.company_code_map"
    with pytest.raises(ValueError, match="unknown deal type"):
        apply_deal(base, "MERGER")
    none = deal_assessment(base)
    assert none["deal_type"] is None and none["templates"] == ids and none["deviations"] == []


@pytest.fixture(scope="module")
def isolated(engine):
    """A project of its own: the execution removes rows from the source, which the shared slice must keep."""
    from sdtf.db import session_scope
    from sdtf.demo import run_vertical_slice

    with session_scope() as s:
        out = run_vertical_slice(s, scale=1, seed=11)
        return {"project_id": out["project"].id, "source_id": out["source"].id, "manifest_id": out["manifest"].id, "run_id": out["run"].id}


def test_cleanup_plan_lifecycle_and_execution(session, isolated, tmp_path):
    m = session.get(ScopeManifest, isolated["manifest_id"])
    assert m.status == "APPROVED"
    before = residual_exposure_report(session, m)
    assert before["residual_cleanup_candidates"]["count"] > 0 and before["deal"]["deal_type"] is None
    cands = cleanup_candidates(session, m)
    assert len(cands) == before["residual_cleanup_candidates"]["count"] and {c["table"] for c in cands} <= {"KNB1", "LFB1", "MARC"}
    p = cu.create_plan(session, m, "architect")
    assert p.sequence == 1 and p.status == "DRAFT" and p.residual_rule == "" and len(p.items) == len(cands) and all(i["decision"] == "INCLUDE" and i["action"] == i["proposed_action"] for i in p.items)
    assert p.summary["included"] == len(cands) and p.summary["deletes"] == len(cands) and set(p.summary["by_table"]) == {c["table"] for c in cands}
    # decisions
    with pytest.raises(ValueError, match="needs a note"):
        cu.decide_item(session, p, p.items[0]["id"], "EXCLUDE", "architect")
    with pytest.raises(LookupError):
        cu.decide_item(session, p, "C99999", "EXCLUDE", "architect", "x")
    with pytest.raises(ValueError, match="decision must be"):
        cu.decide_item(session, p, p.items[0]["id"], "DROP", "architect", "x")
    excluded = cu.decide_item(session, p, p.items[0]["id"], "EXCLUDE", "architect", "keep this view for the TSA")
    assert excluded["decision"] == "EXCLUDE" and p.summary["excluded"] == 1 and p.summary["included"] == len(cands) - 1
    # approval: four eyes, readiness
    with pytest.raises(PermissionError, match="four-eyes"):
        cu.approve_plan(session, p, "architect")
    with pytest.raises(ValueError, match="only an APPROVED plan"):
        cu.execute_plan(session, p, "operator")
    cu.approve_plan(session, p, "approver", "legal reviewed")
    assert p.status == "APPROVED" and p.approved_by == "approver"
    with pytest.raises(ValueError, match="items are decided while it is a draft"):
        cu.decide_item(session, p, p.items[1]["id"], "EXCLUDE", "architect", "late")
    # execution on the simulated source: package first, then the rows go
    src = session.get(SapSystem, isolated["source_id"])
    tables = sorted({i["table"] for i in p.items})
    counts_before = {t: RecordStore.load(session, src.id, tables=[t]).count(t) for t in tables}
    cu.execute_plan(session, p, "operator", out_dir=str(tmp_path))
    assert p.status == "EXECUTED" and p.executed_by == "operator" and p.execution["source"]["simulated"] is True
    results = p.execution["results"]
    assert results.get("SKIPPED_EXCLUDED") == 1 and results.get("VIEW_REMOVED", 0) + results.get("ARCHIVED_TO_PACKAGE_AND_REMOVED", 0) == len(cands) - 1 and not results.get("NOT_FOUND_IN_SOURCE")
    removed = p.execution["removed_rows"]
    assert sum(removed.values()) == len(cands) - 1
    for t in tables:
        assert RecordStore.load(session, src.id, tables=[t]).count(t) == counts_before[t] - removed.get(t, 0)
    zp = p.package["zip"]
    assert os.path.exists(zp) and zp.startswith(str(tmp_path))
    with zipfile.ZipFile(zp) as zf:
        names = set(zf.namelist())
        assert "package.json" in names and all(f"{t}.csv" in names for t in tables)
        first_csv = zf.read(f"{tables[0]}.csv").decode().splitlines()
        assert first_csv[0].startswith("ITEM,ACTION,OBJECT,") and len(first_csv) >= 1
    after = residual_exposure_report(session, m)
    assert after["residual_cleanup_candidates"]["count"] == 1  # the excluded view is still there
    p2 = cu.create_plan(session, m, "architect")
    assert p2.sequence == 2 and len(p2.items) == 1
    md = cu.plan_markdown(p)
    assert "# Residual cleanup plan 1 (EXECUTED)" in md and "## Package" in md and "VIEW_REMOVED" in md or "ARCHIVED_TO_PACKAGE_AND_REMOVED" in md
    kinds = {e.action for e in session.execute(select(AuditEvent).where(AuditEvent.subject_id == m.id)).scalars().all()}
    assert {"CLEANUP_PLAN_CREATED", "CLEANUP_ITEM_DECIDED", "CLEANUP_PLAN_APPROVED", "CLEANUP_PACKAGE_EXPORTED", "CLEANUP_PLAN_EXECUTED"} <= kinds
    # an asset deal keeps the legal record: every item becomes a flag and nothing would change the source
    m.definition = {**m.definition, "deal_type": "ASSET_DEAL"}
    session.flush()
    p3 = cu.create_plan(session, m, "architect")
    assert p3.residual_rule == "RETAIN_AS_LEGAL_RECORD" and all(i["action"] == cu.KEEP_ACTION for i in p3.items) and p3.summary["deletes"] == 0
    assert residual_exposure_report(session, m)["deal"]["residual_rule"] == "RETAIN_AS_LEGAL_RECORD"
    m.definition = {k: v for k, v in m.definition.items() if k != "deal_type"}
    session.flush()
    # a source reached through the add-on is never changed by the platform
    src.connector = "RFC"
    session.flush()
    cu.approve_plan(session, p2, "approver")
    with pytest.raises(ValueError, match="read-only add-on"):
        cu.execute_plan(session, p2, "operator")
    src.connector = "SYNTHETIC"
    session.flush()


def test_cleanup_api_and_cli(client, tokens, session, slice_result, capsys, tmp_path):
    pid, mid = slice_result["project_id"], slice_result["manifest_id"]
    assert [d["id"] for d in client.get(f"{API}/carveout/deal-templates", headers=tokens["viewer"]).json()] == ["ASSET_DEAL", "SHARE_DEAL", "HIVE_DOWN"]
    base = client.get(f"{API}/manifests/{mid}", headers=tokens["viewer"]).json()["definition"]
    defn = {k: base[k] for k in ("name", "scenario_type", "source_system_id", "target_system_id", "company_codes", "target_ownership")}
    r = client.post(f"{API}/projects/{pid}/manifests/from-deal", json={"deal": "SHARE_DEAL", "definition": {**defn, "name": "share deal scenario"}}, headers=tokens["architect"])
    assert r.status_code == 201, r.text
    sd = r.json()
    assert sd["definition"]["deal_type"] == "SHARE_DEAL" and sd["definition"]["historical_policy"] == "FULL" and "Deal type: Share deal." in sd["definition"]["description"]
    assert client.post(f"{API}/projects/{pid}/manifests/from-deal", json={"deal": "HIVE_DOWN", "definition": defn}, headers=tokens["architect"]).status_code == 400
    assert client.post(f"{API}/projects/{pid}/manifests/from-deal", json={"deal": "MERGER", "definition": defn}, headers=tokens["architect"]).status_code == 422
    d = client.get(f"{API}/manifests/{sd['id']}/carveout/deal", headers=tokens["viewer"]).json()
    assert d["deal_type"] == "SHARE_DEAL" and d["deviations"] == [] and len(d["obligations"]) == 3
    assert client.get(f"{API}/manifests/{mid}/carveout/residual", headers=tokens["viewer"]).json()["deal"]["deal_type"] is None
    # plans on the shared slice manifest: created, decided, approved, exported; never executed here (the slice keeps its rows)
    assert client.post(f"{API}/manifests/{mid}/carveout/cleanup-plans", headers=tokens["viewer"]).status_code == 403
    p = client.post(f"{API}/manifests/{mid}/carveout/cleanup-plans", headers=tokens["architect"])
    assert p.status_code == 201, p.text
    plan = p.json()
    assert plan["status"] == "DRAFT" and plan["summary"]["included"] == len(plan["items"]) > 0
    item = plan["items"][0]["id"]
    assert client.post(f"{API}/carveout/cleanup-plans/{plan['id']}/items/{item}", json={"decision": "EXCLUDE"}, headers=tokens["architect"]).status_code == 409
    assert client.post(f"{API}/carveout/cleanup-plans/{plan['id']}/items/nope", json={"decision": "EXCLUDE", "note": "x"}, headers=tokens["architect"]).status_code == 404
    assert client.post(f"{API}/carveout/cleanup-plans/{plan['id']}/items/{item}", json={"decision": "EXCLUDE", "note": "TSA view"}, headers=tokens["architect"]).json()["summary"]["excluded"] == 1
    assert client.post(f"{API}/carveout/cleanup-plans/{plan['id']}/approve", json={"comment": "x"}, headers=tokens["architect"]).status_code == 403
    assert client.post(f"{API}/carveout/cleanup-plans/{plan['id']}/execute", headers=tokens["operator"]).status_code == 409
    assert client.get(f"{API}/carveout/cleanup-plans/{plan['id']}/package", headers=tokens["viewer"]).status_code == 404
    ex = client.post(f"{API}/carveout/cleanup-plans/{plan['id']}/export", headers=tokens["architect"]).json()
    assert ex["zip"].endswith(".zip") and "package.json" in ex["files"] and ex["missing_in_source"] == []
    z = client.get(f"{API}/carveout/cleanup-plans/{plan['id']}/package", headers=tokens["viewer"])
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    a = client.post(f"{API}/carveout/cleanup-plans/{plan['id']}/approve", json={"comment": "legal ok"}, headers=tokens["approver"])
    assert a.status_code == 200 and a.json()["status"] == "APPROVED" and a.json()["approved_by"] == "approver"
    lst = client.get(f"{API}/manifests/{mid}/carveout/cleanup-plans", headers=tokens["viewer"]).json()
    assert [x["id"] for x in lst] == [plan["id"]] and "items" not in lst[0]
    rep = client.get(f"{API}/carveout/cleanup-plans/{plan['id']}/report", headers=tokens["viewer"]).json()
    assert rep["markdown"].startswith("# Residual cleanup plan 1 (APPROVED)")
    # a draft manifest without a run cannot have its plan approved
    draft = client.post(f"{API}/projects/{pid}/manifests", json={**defn, "name": "draft for cleanup"}, headers=tokens["architect"]).json()
    p2 = client.post(f"{API}/manifests/{draft['id']}/carveout/cleanup-plans", headers=tokens["architect"]).json()
    refused = client.post(f"{API}/carveout/cleanup-plans/{p2['id']}/approve", json={}, headers=tokens["approver"])
    assert refused.status_code == 409 and "not APPROVED" in refused.json()["detail"] and "no completed run" in refused.json()["detail"]
    assert client.post(f"{API}/carveout/cleanup-plans/{p2['id']}/reject", json={"comment": "not yet"}, headers=tokens["approver"]).json()["status"] == "REJECTED"
    assert client.get(f"{API}/carveout/cleanup-plans/nope", headers=tokens["viewer"]).status_code == 404
    # CLI
    assert cli_main(["deal-templates"]) == 0 and "ASSET_DEAL: Asset deal" in capsys.readouterr().out
    assert cli_main(["residual-cleanup", "create", "--manifest", mid]) == 0
    out = capsys.readouterr().out
    assert out.startswith("plan 2 ") and "DRAFT" in out
    pid3 = out.split()[2].rstrip(":")
    assert cli_main(["residual-cleanup", "item", "--id", pid3, "--item", "C00001", "--decision", "EXCLUDE"]) == 2 and "needs a note" in capsys.readouterr().err
    assert cli_main(["residual-cleanup", "item", "--id", pid3, "--item", "C00001", "--decision", "EXCLUDE", "--note", "keep"]) == 0
    capsys.readouterr()
    assert cli_main(["residual-cleanup", "export", "--id", pid3, "--out", str(tmp_path)]) == 0 and "package written" in capsys.readouterr().out
    assert cli_main(["residual-cleanup", "approve", "--id", pid3, "--comment", "ok"]) == 0 and "approved" in capsys.readouterr().out
    assert cli_main(["residual-cleanup", "show", "--id", pid3]) == 0
    out = capsys.readouterr().out
    assert "APPROVED" in out and "C00001" in out and "EXCLUDE" in out
    assert cli_main(["residual-cleanup", "list", "--manifest", mid, "--json"]) == 0 and len([line for line in capsys.readouterr().out.splitlines() if '"sequence"' in line]) == 2
    assert cli_main(["residual-cleanup", "report", "--id", pid3, "--out", str(tmp_path / "plan.md")]) == 0 and (tmp_path / "plan.md").read_text().startswith("# Residual cleanup plan 2")
    capsys.readouterr()
    assert cli_main(["residual-cleanup", "show", "--id", "nope"]) == 2
