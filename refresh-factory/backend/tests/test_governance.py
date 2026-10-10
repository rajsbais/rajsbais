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
    assert log.verify() == {"valid": False, "broken_at": 3, "entries": 5, "reason": "hash chain broken"}
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
    assert plan["executable"] == "simulated" and len(plan["phases"]) == 13
    assert all(p["status"] == "blocked" for p in plan["phases"] if p["no"] >= 3)
    ok = runbook.plan_full_refresh(prd, qa)
    assert all(p["status"] == "executable-simulated" for p in ok["phases"] if p["no"] != 7)
    assert next(p for p in ok["phases"] if p["no"] == 7)["status"] == "simulated-mechanism"           # the real copy tooling is not built


def test_post_copy_catalog_is_complete_and_executable():
    required = {"id", "name", "versions", "prerequisites", "precheck", "action", "postcheck", "rollback", "evidence", "approval"}
    assert len(runbook.POST_COPY_TASKS) >= 15
    for t in runbook.POST_COPY_TASKS:
        assert required <= set(t) and t["never_reactivate_production_interfaces"]
        assert t["status"].startswith("implemented") and t["approval_label"] in ("none", "basis_lead", "integration_owner", "security_officer")


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


# ---- signed audit log, head escrow
def test_audit_entries_are_signed_and_an_edit_with_a_recomputed_chain_is_still_caught(tmp_path):
    key = b"audit-signing-key"
    log = AuditLog(tmp_path / "a.jsonl", key)
    for i in range(5):
        log.append("u", f"act{i}", "r", {"i": i})
    assert all("sig" in e for e in log.entries()) and log.verify()["valid"] and log.verify()["signed"]
    # an attacker without the key edits entry 3 and recomputes every hash after it: the chain is consistent again, the signatures are not
    es = log._entries
    es[2]["actor"] = "mallory"
    prev = es[1]["hash"]
    for e in es[2:]:
        e["prev"] = prev
        e["hash"] = AuditLog._digest(e)
        prev = e["hash"]
    r = log.verify()
    assert not r["valid"] and r["broken_at"] == 3 and "signature" in r["reason"]


def test_stripped_signatures_are_detected_but_legacy_unsigned_prefix_is_accepted(tmp_path):
    unsigned = AuditLog(tmp_path / "a.jsonl")
    unsigned.append("u", "old1", "r")
    unsigned.append("u", "old2", "r")
    upgraded = AuditLog(tmp_path / "a.jsonl", b"k")  # signing switched on later
    upgraded.append("u", "new1", "r")
    upgraded.append("u", "new2", "r")
    assert upgraded.verify() == {**upgraded.verify(), "valid": True, "legacy_unsigned": 2}
    del upgraded._entries[3]["sig"]
    r = upgraded.verify()
    assert not r["valid"] and "stripped" in r["reason"]


def test_a_signature_made_with_another_key_is_rejected(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", b"key-1")
    log.append("u", "a", "r")
    other = AuditLog(tmp_path / "a.jsonl", b"key-2")
    assert not other.verify()["valid"]


def test_escrowed_head_detects_truncation_and_rewrites(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", b"k")
    for i in range(6):
        log.append("u", f"a{i}", "r")
    head = log.head()
    assert head["seq"] == 6 and head["signed"] and log.verify(head)["escrow_checked"]
    log.append("u", "later", "r")
    assert log.verify(head)["valid"]  # growth is fine
    log._entries = log._entries[:4]  # the newest entries are dropped; the chain of what remains is still valid
    assert log.verify()["valid"]
    r = log.verify(head)
    assert not r["valid"] and "truncated" in r["reason"]
    forged = {**head, "head": "f" * 64}
    assert not AuditLog(None, b"k").verify(forged)["valid"]
    tampered_sig = {**head, "seq": 3, "head": log._entries[2]["hash"]}  # a real hash with a signature that does not cover it
    assert not log.verify(tampered_sig)["valid"]


def test_audit_key_file_and_environment_key(tmp_path, monkeypatch):
    import stat
    from rfactory.security.audit import load_audit_key
    k = load_audit_key(tmp_path)
    assert stat.S_IMODE((tmp_path / "audit.key").stat().st_mode) == 0o600 and load_audit_key(tmp_path) == k
    monkeypatch.setenv("RFACTORY_AUDIT_KEY", "from-env")
    assert load_audit_key(tmp_path) == b"from-env" and load_audit_key(None) == b"from-env"
    monkeypatch.delenv("RFACTORY_AUDIT_KEY")
    assert load_audit_key(None) is None


def test_audit_head_and_verify_over_http(tmp_path):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    c = TestClient(create_app(tmp_path, persist=False))
    H = lambda u: {"X-Demo-User": u}
    c.post("/api/demo/bootstrap", headers=H("root.admin"))
    assert c.get("/api/audit/head", headers=H("bob.steward")).status_code == 403
    head = c.get("/api/audit/head", headers=H("erin.auditor")).json()
    c.post("/api/agents/landscape-discovery/run", json={}, headers=H("alice.basis"))
    q = {"expected_seq": head["seq"], "expected_head": head["head"], "expected_ts": head["ts"], "expected_signature": head["signature"]}
    assert c.get("/api/audit/verify", params=q, headers=H("erin.auditor")).json()["escrow_checked"]
    bad = c.get("/api/audit/verify", params={**q, "expected_head": "0" * 64}, headers=H("erin.auditor")).json()
    assert bad["valid"] is False and "truncated" in bad["reason"]
