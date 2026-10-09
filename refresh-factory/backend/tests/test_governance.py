import copy

from rfactory.fullrefresh import runbook
from rfactory.sap.adapter import SapSystem
from rfactory.security.audit import AuditLog
from rfactory.security.auth import DEMO_USERS as U
from .conftest import ADMIN


def test_audit_chain_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(5):
        log.append("u", "act", f"r{i}", {"i": i})
    assert log.verify()["valid"]
    reloaded = AuditLog(tmp_path / "a.jsonl")
    assert reloaded.verify()["valid"] and len(reloaded.entries()) == 5
    log._entries[2]["details"]["i"] = 99
    assert log.verify() == {"valid": False, "broken_at": 3, "entries": 5}
    log2 = AuditLog(); [log2.append("u", "a", "r") for _ in range(4)]
    del log2._entries[1]
    assert not log2.verify()["valid"]


def test_agent_never_holds_destructive_permissions():
    perms = U["refresh.copilot"].permissions()
    assert not perms & {"plan:approve", "exception:approve", "run:execute", "masking:reidentify", "system:write"}
    assert "plan:approve" in U["carol.approver"].permissions()


def test_full_refresh_guard():
    prd = SapSystem(id="a", sid="EP1", client="100", role="PRD", owner="x")
    qa = SapSystem(id="b", sid="EQ1", client="200", role="QAS", owner="y")
    assert runbook.validate_pair(prd, qa)["ok"]
    r = runbook.validate_pair(qa, prd)
    assert not r["ok"] and "production" in r["blockers"][0].lower()
    hana = qa.model_copy(update={"db_type": "ORACLE", "id": "c"})
    assert any("Heterogeneous" in b for b in runbook.validate_pair(prd, hana)["blockers"])
    assert not runbook.validate_pair(qa, qa)["ok"]
    locked = qa.model_copy(update={"writable_target_allowed": False, "id": "d"})
    assert not runbook.validate_pair(prd, locked)["ok"]
    plan = runbook.plan_full_refresh(qa, prd)
    assert plan["executable"] is False and len(plan["phases"]) == 13
    assert all(p["status"] == "blocked" for p in plan["phases"] if p["no"] >= 3)


def test_post_copy_catalog_is_complete():
    required = {"id", "name", "versions", "prerequisites", "precheck", "action", "postcheck", "rollback", "evidence", "approval"}
    assert len(runbook.POST_COPY_TASKS) >= 10
    for t in runbook.POST_COPY_TASKS:
        assert required <= set(t) and t["never_reactivate_production_interfaces"]
        assert t["status"].startswith("catalogued")


def test_landscape_readiness_and_combinations(svc):
    assert svc.readiness(svc.tgt_id)["ready"]
    svc.adapters[svc.tgt_id].outbound_interfaces()[0]["active"] = True
    r = svc.readiness(svc.tgt_id)
    assert not r["ready"] and any(c["id"] == "T2" and not c["ok"] for c in r["checks"])
    combos = svc.refresh_combinations()
    to_prd = [c for c in combos if c["target"] == svc.src_id]
    assert to_prd and all(not c["selective"] and not c["full_system_refresh"] and c["blockers"] for c in to_prd)
    ok = [c for c in combos if c["target"] == svc.tgt_id][0]
    assert ok["selective"] and ok["full_system_refresh"]
