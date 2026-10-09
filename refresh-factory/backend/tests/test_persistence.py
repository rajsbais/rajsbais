import json
import os
import pickle
import sqlite3
import stat

import pytest
from cryptography.fernet import Fernet

from rfactory.fullrefresh.engine import _digest
from rfactory.persistence import store as st
from rfactory.security.auth import DEMO_USERS as U
from rfactory.service import RefreshService

from .conftest import ADMIN, ALICE, CAROL, approved_project, make_project, ready_project
from .test_delta import approved as approved_delta
from .test_execution import fault_after
from .test_fullrefresh import approve_postcopy, prepare
from .test_orchestration import agent_step, tick

BASTIAN = U["bastian.lead"]


@pytest.fixture
def dsvc(tmp_path):
    s = RefreshService(tmp_path, persist=True)
    b = s.bootstrap_demo(ADMIN)
    s.src_id, s.tgt_id = b["source"]["id"], b["target"]["id"]
    s.s4_src, s.s4_tgt = b["s4_source"]["id"], b["s4_target"]["id"]
    return s


def restart(s):
    """Stop the process after a normal request boundary and start a new one on the same data directory."""
    s.checkpoint()
    n = RefreshService(s.data_dir, persist=True)
    for a in ("src_id", "tgt_id", "s4_src", "s4_tgt"):
        setattr(n, a, getattr(s, a))
    return n


def pub(objs):
    def one(o):
        if hasattr(o, "public"):
            return o.public()
        return {"id": o.id, "status": o.status, "manifest": o.manifest.content_hash(), "approval": o.approval, "runs": o.runs,   # a Project
                "plan": (o.plan.manifest_hash, len(o.plan.instances)) if o.plan else None,
                "decisions": {k: v.value for k, v in o.report.decisions.items()} if o.report else None,
                "masking": sorted(o.masking_policy.fields()) if o.masking_policy else None}
    return json.dumps([one(o) for o in objs], sort_keys=True, default=str)


# ---- everything the platform holds comes back
def test_all_module_state_round_trips(dsvc):
    s = dsvc
    from datetime import datetime, timezone
    at = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    p = approved_project(s)
    run = s.execute(ALICE, p.id)
    sc = approved_delta(s)
    s.delta.run(U["svc.scheduler"], sc.id)
    s.postcopy.capture_profile(ALICE, s.s4_tgt, "s4 baseline")
    prepare(s)
    s.orch.submit(ALICE, "tdm_sweep", idempotency_key="k1")
    s.orch.set_windows(ALICE, s.tgt_id, {"allow": [{"weekdays": [5], "start": "02:00", "end": "06:00"}]})
    s.agents.run(ALICE, "landscape-discovery")
    before = {
        "projects": pub(s.projects.values()), "runs": pub(s.runs.values()), "delta": pub(s.delta.scenarios.values()), "profiles": pub(s.postcopy.profiles.values()),
        "programs": pub(s.full.programs.values()), "jobs": pub(s.orch.jobs.values()), "reports": json.dumps([r.public() for r in s.agents.reports.values()], sort_keys=True, default=str),
        "window": s.orch.window_state(s.tgt_id, at), "audit": s.audit.verify()["head"],
        "data": {k: _digest(a.data) for k, a in s.adapters.items()}, "tech": {k: a.tech.digest() for k, a in s.adapters.items()}}
    n = restart(s)
    after = {
        "projects": pub(n.projects.values()), "runs": pub(n.runs.values()), "delta": pub(n.delta.scenarios.values()), "profiles": pub(n.postcopy.profiles.values()),
        "programs": pub(n.full.programs.values()), "jobs": pub(n.orch.jobs.values()), "reports": json.dumps([r.public() for r in n.agents.reports.values()], sort_keys=True, default=str),
        "window": n.orch.window_state(n.tgt_id, at), "audit": n.audit.verify()["head"],
        "data": {k: _digest(a.data) for k, a in n.adapters.items()}, "tech": {k: a.tech.digest() for k, a in n.adapters.items()}}
    assert before == after
    assert n.runs[run.id].release == run.release and n.audit.verify()["valid"]
    assert all(a.system is n.systems[k] for k, a in n.adapters.items())  # adapter and system are still one object


def test_second_save_without_changes_writes_nothing_and_deletions_are_stored(dsvc):
    s = dsvc
    s.checkpoint()
    assert s.checkpoint()["written"] == 0
    approved_project(s)
    assert s.checkpoint()["written"] >= 1
    pid = next(iter(s.projects))
    del s.projects[pid]
    assert s.checkpoint()["deleted"] == 1
    assert pid not in restart(s).projects


# ---- work continues after a restart
def test_approved_plan_executes_after_a_restart_and_matches_a_run_without_one(dsvc, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("RF_MASKING_KEY", "fixed-key-for-comparison")  # otherwise every engine invents its own pseudonym key
    p = approved_project(dsvc)
    n = restart(dsvc)
    run = n.execute(ALICE, p.id)
    assert run.status == "COMPLETED" and run.release == "RELEASED"
    ref = RefreshService(tmp_path_factory.mktemp("ref"))
    b = ref.bootstrap_demo(ADMIN)
    ref.src_id, ref.tgt_id = b["source"]["id"], b["target"]["id"]
    ref.s4_src, ref.s4_tgt = b["s4_source"]["id"], b["s4_target"]["id"]
    rp = approved_project(ref)
    ref.execute(ALICE, rp.id)
    from .conftest import norm
    assert norm(n.adapters[n.tgt_id].data) == norm(ref.adapters[ref.tgt_id].data)


def test_failed_run_resumes_after_a_restart(dsvc):
    p = approved_project(dsvc)
    run = dsvc.execute(ALICE, p.id, fault_injector=fault_after(20, RuntimeError("lock table overflow")))
    assert run.status == "FAILED"
    n = restart(dsvc)
    resumed = n.resume(ALICE, run.id)
    assert resumed.status == "COMPLETED" and resumed.release == "RELEASED"


def test_masking_keys_survive_so_pseudonyms_stay_stable(dsvc):
    p = approved_project(dsvc)
    dsvc.execute(ALICE, p.id)
    rule = next(r for r in dsvc.engines[p.id].policy.rules if r.field == "NAME1")
    sc = approved_delta(dsvc)
    n = restart(dsvc)
    assert n.engines[p.id].mask_value(rule, "Acme GmbH") == dsvc.engines[p.id].mask_value(rule, "Acme GmbH")
    assert n.delta.scenarios[sc.id].mask_key == sc.mask_key
    assert n.delta.scenarios[sc.id].public()["config_hash"] == sc.public()["config_hash"]  # the standing approval is still valid


def test_delta_refresh_continues_from_its_watermark(dsvc):
    sc = approved_delta(dsvc)
    first = dsvc.delta.run(U["svc.scheduler"], sc.id)
    n = restart(dsvc)
    again = n.delta.run(U["svc.scheduler"], sc.id)
    assert n.delta.get(sc.id).watermark == dsvc.delta.get(sc.id).watermark
    assert again["new"] == 0 and again["changed"] == 0 and first["new"] > 0  # nothing re-copied after the restart


def test_full_refresh_survives_a_restart_mid_program_and_still_rolls_back_exactly(dsvc):
    p = prepare(dsvc)
    tgt = dsvc.adapters[p.target_id]
    d_before = _digest(tgt.data)
    dsvc.full.run(ALICE, p.id)
    assert p.status == "WAITING" and p.unmasked_target
    n = restart(dsvc)
    q = n.full.get(p.id)
    assert q.unmasked_target and q.backup is not None
    assert n.orch.leases[q.target_id]["holder"] == q.id  # the lease came back too
    n.full.rollback(ALICE, q.id)
    assert _digest(n.adapters[q.target_id].data) == d_before and q.target_id not in n.orch.leases


def test_full_refresh_completes_across_two_restarts(dsvc):
    p = prepare(dsvc)
    dsvc.full.run(ALICE, p.id)
    n = restart(dsvc)
    approve_postcopy(n, n.full.get(p.id))
    n.full.run(ALICE, p.id)
    m = restart(n)
    m.full.sign_off(U["sven.security"], p.id)
    m.full.run(ALICE, p.id)
    m.full.release(CAROL, p.id)
    q = m.full.get(p.id)
    assert q.status == "RELEASED" and not q.unmasked_target and m.audit.verify()["valid"]


def test_orchestration_queue_schedule_window_and_events_survive(dsvc):
    s = dsvc
    j = s.orch.submit(ALICE, "tdm_sweep", idempotency_key="nightly")
    pl = s.orch.submit_pipeline(ALICE, {"steps": [agent_step("a"), {"key": "g", "kind": "human_gate", "after": ["a"], "params": {"note": "x"}}]})
    sch = s.orch.create_schedule(ALICE, {"schedule": {"kind": "daily", "hour": 2}, "template": {"type": "job", "kind": "lean_sweep"}})
    s.orch.approve_schedule(CAROL, sch.id)
    s.orch.subscribe(ALICE, {"channel": "email", "destination": "ops@example.test"})
    n = restart(s)
    assert n.orch.submit(ALICE, "tdm_sweep", idempotency_key="nightly").id == j.id  # idempotency survives
    assert n.orch.schedules[sch.id].public()["approved"] and len(n.orch.subs) == 1 and n.orch.events
    out = tick(n)
    assert {r["job"] for r in out["ran"]} >= {j.id}
    assert n.orch.pipeline(pl["id"])["status"] == "RUNNING"  # the gate still waits for a human
    gate = next(x for x in n.orch.jobs.values() if x.kind == "human_gate")
    n.orch.complete_gate(CAROL, gate.id)
    tick(n)
    assert n.orch.pipeline(pl["id"])["status"] == "SUCCEEDED"


def test_audit_chain_continues_and_stays_valid(dsvc):
    ready_project(dsvc)
    n_before = len(dsvc.audit.entries())
    n = restart(dsvc)
    assert len(n.audit.entries()) == n_before
    n.agents.run(ALICE, "landscape-discovery")
    assert len(n.audit.entries()) == n_before + 1 and n.audit.verify()["valid"]


# ---- crash consistency
def test_a_crash_in_the_middle_of_a_request_loses_that_request_as_a_whole(dsvc):
    s = dsvc
    p = ready_project(s)
    s.submit(ALICE, p.id)
    s.checkpoint()  # end of a request
    s.approve(CAROL, p.id)  # a request that is in progress ... the process dies before its boundary
    crashed = RefreshService(s.data_dir, persist=True)
    q = crashed.projects[p.id]
    assert q.status == "PENDING_APPROVAL" and q.approval is None  # the approval is gone as a whole, not half-applied
    actions = [e["action"] for e in crashed.audit.entries()]
    assert "plan.approved" in actions  # the audit log still shows the attempt: it records what was tried, the store what is true
    crashed.approve(CAROL, p.id)  # and the user can simply repeat it
    assert crashed.projects[p.id].status == "APPROVED"


def test_work_found_in_progress_at_load_is_reported_not_hidden(dsvc):
    p = ready_project(dsvc)
    p.status = "RUNNING"
    n = restart(dsvc)
    assert any(f"project {p.id}" in x for x in n.store.status()["interrupted_work"])
    assert any(e["action"] == "persistence.interrupted_work_found" for e in n.audit.entries())


# ---- protection of the stored state
def read_all_bytes(path):
    out = b""
    for suffix in ("", "-wal", "-shm"):
        f = path.with_name(path.name + suffix)
        if f.exists():
            out += f.read_bytes()
    return out


def test_state_is_encrypted_at_rest(dsvc):
    p = prepare(dsvc)
    dsvc.full.run(ALICE, p.id)  # the target now holds unmasked production data and a pre-refresh backup is stored
    marker = "owner-marker-8d41c0e6b7a95f12e3"  # long enough that a chance match in random ciphertext is impossible
    dsvc.system(p.target_id).owner = marker
    dsvc.checkpoint()
    assert marker.encode() in st._dumps(st.collect(dsvc)[("system", p.target_id)])  # the marker IS in the plaintext state ...
    raw = read_all_bytes(dsvc.store.path)
    assert marker.encode() not in raw and b"KNA1" * 3 not in raw and b"rfactory.sap" not in raw  # ... and nowhere on disk
    kf = dsvc.store.path.with_suffix(".key")
    assert stat.S_IMODE(kf.stat().st_mode) == 0o600


def test_tampered_state_is_refused(dsvc):
    ready_project(dsvc)
    dsvc.checkpoint()
    db = sqlite3.connect(dsvc.store.path)
    kind, k, blob = db.execute("SELECT kind, id, blob FROM aggregates WHERE kind='project'").fetchone()
    flipped = bytearray(blob)
    flipped[len(flipped) // 2] ^= 0x01
    with db:
        db.execute("UPDATE aggregates SET blob=? WHERE kind=? AND id=?", (bytes(flipped), kind, k))
    db.close()
    with pytest.raises(st.StoreError, match="failed authentication"):
        RefreshService(dsvc.data_dir, persist=True)


def test_wrong_key_is_a_clear_error_not_an_empty_platform(dsvc, monkeypatch):
    ready_project(dsvc)
    dsvc.checkpoint()
    dsvc.store.path.with_suffix(".key").unlink()
    with pytest.raises(st.StoreError, match="key does not match"):
        RefreshService(dsvc.data_dir, persist=True)  # a fresh key file is generated: it cannot open the old database
    monkeypatch.setenv("RFACTORY_STATE_KEY", Fernet.generate_key().decode())
    with pytest.raises(st.StoreError, match="key does not match"):
        RefreshService(dsvc.data_dir, persist=True)


def test_environment_key_is_used_and_must_be_kept(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("RFACTORY_STATE_KEY", key)
    s = RefreshService(tmp_path, persist=True)
    s.bootstrap_demo(ADMIN)
    s.checkpoint()
    assert "environment" in s.store.status()["key_source"] and not s.store.path.with_suffix(".key").exists()
    assert len(RefreshService(tmp_path, persist=True).systems) == 4


def test_schema_mismatch_and_missing_marker_are_refused(dsvc):
    ready_project(dsvc)
    dsvc.checkpoint()
    db = sqlite3.connect(dsvc.store.path)
    with db:
        db.execute("UPDATE meta SET value=? WHERE key='schema'", (b"99",))
    with pytest.raises(st.StoreError, match="version 99"):
        RefreshService(dsvc.data_dir, persist=True)
    with db:
        db.execute("DELETE FROM meta WHERE key='schema'")
    db.close()
    with pytest.raises(st.StoreError, match="no schema marker"):
        RefreshService(dsvc.data_dir, persist=True)


def test_unpickler_allow_list_blocks_code_execution_even_with_the_key(dsvc):
    if os.path.exists("/tmp/rfactory-pwned"):
        os.unlink("/tmp/rfactory-pwned")  # left behind only if the allow-list was ever disabled
    ready_project(dsvc)
    dsvc.checkpoint()

    class Evil:
        def __reduce__(self):
            return (os.system, ("echo pwned > /tmp/rfactory-pwned",))
    f = dsvc.store._fernet  # an attacker who holds the data key
    plain = pickle.dumps(Evil())
    import hashlib
    db = sqlite3.connect(dsvc.store.path)
    with db:
        db.execute("INSERT INTO aggregates VALUES ('run','evil',?,?,'now')", (hashlib.sha256(plain).hexdigest(), f.encrypt(plain)))
    db.close()
    with pytest.raises(st.StoreError, match="not in the allow-list"):
        RefreshService(dsvc.data_dir, persist=True)
    assert not os.path.exists("/tmp/rfactory-pwned")


def test_ephemeral_service_is_unchanged(tmp_path):
    s = RefreshService(tmp_path)
    assert s.store is None and s.checkpoint() is None and not (tmp_path / "state.db").exists()


# ---- HTTP
def test_durable_api_keeps_state_across_a_restart_and_saves_only_after_writes(tmp_path):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    H = lambda u: {"X-Demo-User": u}
    c = TestClient(create_app(tmp_path, persist=True))
    assert c.get("/api/persistence/status", headers=H("erin.auditor")).json()["durable"]
    b = c.post("/api/demo/bootstrap", headers=H("root.admin")).json()
    p = c.post("/api/projects", json={"name": "durable", "source_id": b["source"]["id"], "target_id": b["target"]["id"]}, headers=H("alice.basis")).json()
    stat1 = c.get("/api/persistence/status", headers=H("erin.auditor")).json()
    assert stat1["aggregates"] >= 5 and stat1["by_kind"]["project"] == 1 and stat1["encrypted_at_rest"]
    c.get("/api/projects", headers=H("alice.basis"))
    assert c.get("/api/persistence/status", headers=H("erin.auditor")).json()["last_saved"] == stat1["last_saved"]  # reads never save
    c2 = TestClient(create_app(tmp_path, persist=True))  # "restart"
    assert [x["name"] for x in c2.get("/api/projects", headers=H("alice.basis")).json()] == ["durable"]
    assert len(c2.get("/api/systems", headers=H("alice.basis")).json()) == 4
    assert c2.get("/api/audit/verify", headers=H("erin.auditor")).json()["valid"]
    assert c2.post("/api/persistence/checkpoint", headers=H("tina.tester")).status_code == 403
    assert c2.post("/api/persistence/checkpoint", headers=H("alice.basis")).status_code == 200


def test_a_failed_save_is_reported_not_swallowed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    app = create_app(tmp_path, persist=True)
    c = TestClient(app)
    monkeypatch.setattr(app.state.svc.store, "save", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    r = c.post("/api/demo/bootstrap", headers={"X-Demo-User": "root.admin"})
    assert r.status_code == 500 and "could not be saved" in r.json()["detail"] and "disk full" in r.json()["detail"]


def test_non_durable_api_says_so(tmp_path):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    c = TestClient(create_app(tmp_path, persist=False))
    r = c.get("/api/persistence/status", headers={"X-Demo-User": "erin.auditor"}).json()
    assert r["durable"] is False and "RFACTORY_DATA_DIR" in r["note"]
    assert c.post("/api/persistence/checkpoint", headers={"X-Demo-User": "alice.basis"}).status_code == 409


def test_environment_variable_turns_durability_on(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    monkeypatch.setenv("RFACTORY_DATA_DIR", str(tmp_path / "data"))
    c = TestClient(create_app())
    c.post("/api/demo/bootstrap", headers={"X-Demo-User": "root.admin"})
    assert (tmp_path / "data" / "state.db").exists()
    assert len(TestClient(create_app()).get("/api/systems", headers={"X-Demo-User": "alice.basis"}).json()) == 4


# ---- envelope encryption and key rotation
def test_data_key_is_wrapped_and_the_kek_alone_opens_the_database(dsvc):
    ready_project(dsvc)
    dsvc.checkpoint()
    db = sqlite3.connect(dsvc.store.path)
    wrapped = db.execute("SELECT value FROM meta WHERE key='wrapped_dek'").fetchone()[0]
    db.close()
    kek = dsvc.store.path.with_suffix(".key").read_bytes()
    dek = Fernet(kek).decrypt(wrapped)
    assert dek != kek and dek != dsvc.store.path.with_suffix(".key").read_bytes()  # two different keys: envelope, not direct
    with pytest.raises(Exception):
        Fernet(kek).decrypt(sqlite3.connect(dsvc.store.path).execute("SELECT blob FROM aggregates LIMIT 1").fetchone()[0])  # the KEK cannot read data
    assert dsvc.store.status()["envelope_encryption"] and dsvc.store.status()["key_provider"] == "local-fernet-kek"


def test_data_key_rotation_reencrypts_everything_and_keeps_working(dsvc):
    p = ready_project(dsvc)
    dsvc.checkpoint()
    db = sqlite3.connect(dsvc.store.path)
    before = {r[0]: r[1] for r in db.execute("SELECT id, blob FROM aggregates")}
    n = dsvc.store.rotate_dek()
    after = {r[0]: r[1] for r in db.execute("SELECT id, blob FROM aggregates")}
    db.close()
    assert n == len(before) and all(before[k] != after[k] for k in before)
    q = restart(dsvc)  # restores with the same KEK (the wrapped DEK was replaced)
    assert q.projects[p.id].status == dsvc.projects[p.id].status
    old_dek_fernet = Fernet(Fernet.generate_key())
    with pytest.raises(Exception):
        old_dek_fernet.decrypt(next(iter(after.values())))


def test_a_data_key_rotation_that_fails_midway_changes_nothing(dsvc, monkeypatch):
    ready_project(dsvc)
    dsvc.checkpoint()
    calls = {"n": 0}
    real = dsvc.store._fernet

    class Flaky:
        def __init__(self, f): self.f = f
        def decrypt(self, b):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("disk error")
            return self.f.decrypt(b)
    monkeypatch.setattr(dsvc.store, "_fernet", Flaky(real))
    with pytest.raises(RuntimeError):
        dsvc.store.rotate_dek()
    monkeypatch.setattr(dsvc.store, "_fernet", real)
    assert len(restart(dsvc).projects) == 1  # still opens with the original data key


def test_kek_rotation_rewraps_without_touching_blobs_and_the_old_kek_stops_working(dsvc, monkeypatch):
    ready_project(dsvc)
    dsvc.checkpoint()
    db = sqlite3.connect(dsvc.store.path)
    blobs = db.execute("SELECT id, blob FROM aggregates ORDER BY id").fetchall()
    old = dsvc.store.path.with_suffix(".key").read_bytes()
    new = Fernet.generate_key()
    from rfactory.persistence.rotate import rotate
    out = rotate(dsvc.data_dir, new_kek=new, write_key_file=True)
    assert out == {"kek_rotated": True}
    assert db.execute("SELECT id, blob FROM aggregates ORDER BY id").fetchall() == blobs  # nothing re-encrypted
    db.close()
    assert dsvc.store.path.with_suffix(".key").read_bytes() == new
    assert stat.S_IMODE(dsvc.store.path.with_suffix(".key").stat().st_mode) == 0o600
    assert len(RefreshService(dsvc.data_dir, persist=True).projects) == 1  # opens with the new KEK
    dsvc.store.path.with_suffix(".key").write_bytes(old)
    with pytest.raises(st.StoreError, match="key does not match"):
        RefreshService(dsvc.data_dir, persist=True)  # the retired KEK no longer opens it


def test_rotation_tool_with_environment_keys_and_error_cases(tmp_path, monkeypatch, capsys):
    from rfactory.persistence import rotate as rot
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("RFACTORY_STATE_KEY", k1)
    s = RefreshService(tmp_path, persist=True)
    s.bootstrap_demo(ADMIN)
    s.checkpoint()
    assert rot.main(["--data-dir", str(tmp_path), "--rotate-data-key"]) == 0
    monkeypatch.setenv("NEW_KEK", k2)
    assert rot.main(["--data-dir", str(tmp_path), "--new-key-env", "NEW_KEK"]) == 0
    with pytest.raises(st.StoreError):
        RefreshService(tmp_path, persist=True)  # RFACTORY_STATE_KEY still holds the retired key
    monkeypatch.setenv("RFACTORY_STATE_KEY", k2)
    assert len(RefreshService(tmp_path, persist=True).systems) == 4
    assert rot.main(["--data-dir", str(tmp_path)]) == 1 and "nothing to do" in capsys.readouterr().err
    monkeypatch.setenv("NEW_KEK", "not-a-fernet-key")
    with pytest.raises(Exception):
        rot.rotate(tmp_path, new_kek=b"not-a-fernet-key")
    monkeypatch.setenv("RFACTORY_STATE_KEY", Fernet.generate_key().decode())
    assert rot.main(["--data-dir", str(tmp_path), "--rotate-data-key"]) == 1  # wrong current key


def test_rotate_data_key_over_http_is_human_only_and_audited(tmp_path):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    H = lambda u: {"X-Demo-User": u}
    app = create_app(tmp_path, persist=True)
    c = TestClient(app)
    c.post("/api/demo/bootstrap", headers=H("root.admin"))
    assert c.post("/api/persistence/rotate-data-key", headers=H("tina.tester")).status_code == 403
    assert c.post("/api/persistence/rotate-data-key", headers=H("refresh.copilot")).status_code == 403
    r = c.post("/api/persistence/rotate-data-key", headers=H("alice.basis"))
    assert r.status_code == 200 and r.json()["blobs_reencrypted"] >= 5
    assert any(e["action"] == "persistence.data_key_rotated" and e["actor"] == "alice.basis" for e in app.state.svc.audit.entries())
    assert len(TestClient(create_app(tmp_path, persist=True)).get("/api/systems", headers=H("alice.basis")).json()) == 4
