"""PostgreSQL state for several platform instances, against a REAL PostgreSQL (see tests/pgfix.py)."""
import json
import threading

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rfactory.api.main import create_app
from rfactory.persistence.backends import BackendError, PostgresBackend
from rfactory.persistence.store import StoreError

from .pgfix import pg_admin_url, pgdb, psycopg  # noqa: F401  (fixtures)

H = {"X-Demo-User": "alice.basis"}
ROOT = {"X-Demo-User": "root.admin"}


@pytest.fixture
def env(monkeypatch, pgdb):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("RFACTORY_DATABASE_URL", pgdb)
    monkeypatch.setenv("RFACTORY_STATE_KEY", key)
    monkeypatch.setenv("RFACTORY_AUDIT_KEY", "audit-key-for-tests")
    monkeypatch.setenv("RFACTORY_WRITE_LOCK_TIMEOUT", "60")
    return {"url": pgdb, "key": key}


def app(tmp_path, n):
    return create_app(tmp_path / f"i{n}")


def two(tmp_path):
    a, b = app(tmp_path, 1), app(tmp_path, 2)
    return a, b, TestClient(a), TestClient(b)


def ids(c):
    return {s["sid"]: s["id"] for s in c.get("/api/systems", headers=H).json()}


def raw(url):
    return psycopg.connect(url, autocommit=True)


def test_state_survives_a_restart_and_is_in_postgres(env, tmp_path):
    a = app(tmp_path, 1)
    ca = TestClient(a)
    assert ca.post("/api/demo/bootstrap", headers=ROOT).status_code == 200
    i = ids(ca)
    assert ca.post("/api/projects", json={"name": "p1", "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H).status_code == 201
    st = ca.get("/api/persistence/status", headers=H).json()
    assert st["backend"] == "postgresql" and st["multi_instance"] is True and st["aggregates"] >= 5 and "server_version" in st
    a.state.svc.store.backend.close()
    c2 = TestClient(app(tmp_path, 2))  # a brand-new instance, empty data dir: everything comes from PostgreSQL
    assert [p["name"] for p in c2.get("/api/projects", headers=H).json()] == ["p1"]
    assert set(ids(c2)) == {"EP1", "EQ1", "S4P", "S4Q"}
    assert c2.get("/api/health").json()["audit"]["valid"] is True
    with raw(env["url"]) as cx:
        tables = {r[0] for r in cx.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert {"rf_aggregates", "rf_meta", "rf_version", "rf_audit"} <= tables


def test_the_database_holds_only_ciphertext_and_a_wrapped_key(env, tmp_path):
    ca = TestClient(app(tmp_path, 1))
    ca.post("/api/demo/bootstrap", headers=ROOT)
    with raw(env["url"]) as cx:
        blobs = [bytes(r[0]) for r in cx.execute("SELECT blob FROM rf_aggregates")]
        meta = {r[0]: bytes(r[1]) for r in cx.execute("SELECT key, value FROM rf_meta")}
    assert blobs and all(b.startswith(b"gAAAA") for b in blobs)
    assert not any(b"Alpha Manufacturing" in b or b"BATCHUSR" in b for b in blobs)
    assert env["key"].encode() not in b"".join(meta.values())  # the key-encryption key is never stored
    f = Fernet(env["key"].encode())
    dek = f.decrypt(meta["wrapped_dek"])
    assert dek != env["key"].encode() and Fernet(dek).decrypt(blobs[0])  # envelope: the KEK wraps the DEK, the DEK opens the data
    with pytest.raises(Exception):
        f.decrypt(blobs[0])  # the KEK cannot read data


def test_a_wrong_key_or_missing_keys_refuse_to_start(env, tmp_path, monkeypatch):
    TestClient(app(tmp_path, 1)).post("/api/demo/bootstrap", headers=ROOT)
    monkeypatch.setenv("RFACTORY_STATE_KEY", Fernet.generate_key().decode())
    with pytest.raises(StoreError, match="state key does not match"):
        app(tmp_path, 2)
    monkeypatch.delenv("RFACTORY_STATE_KEY")
    with pytest.raises(RuntimeError, match="RFACTORY_STATE_KEY"):
        app(tmp_path, 3)
    monkeypatch.setenv("RFACTORY_STATE_KEY", env["key"])
    monkeypatch.delenv("RFACTORY_AUDIT_KEY")
    with pytest.raises(RuntimeError, match="RFACTORY_AUDIT_KEY"):
        app(tmp_path, 4)


def test_a_tampered_aggregate_is_refused(env, tmp_path):
    ca = TestClient(app(tmp_path, 1))
    ca.post("/api/demo/bootstrap", headers=ROOT)
    with raw(env["url"]) as cx:
        cx.execute("UPDATE rf_aggregates SET blob = overlay(blob placing '\\x41'::bytea from 40 for 1) WHERE kind = 'agents'")
    with pytest.raises(StoreError, match="failed authentication"):
        app(tmp_path, 2)


def test_two_instances_see_each_others_changes(env, tmp_path):
    a, b, ca, cb = two(tmp_path)
    ca.post("/api/demo/bootstrap", headers=ROOT)
    assert set(ids(cb)) == {"EP1", "EQ1", "S4P", "S4Q"}  # B never loaded anything, yet it sees A's landscape
    i = ids(cb)
    assert cb.post("/api/projects", json={"name": "made on B", "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H).status_code == 201
    assert [p["name"] for p in ca.get("/api/projects", headers=H).json()] == ["made on B"]
    pid = ca.get("/api/projects", headers=H).json()[0]["id"]
    assert ca.put(f"/api/projects/{pid}/manifest", json={"scope": {"object_type": "SALES_ORDER", "company_codes": ["1000"], "plants": []}, "last_days": 90,
                                                         "include_downstream": [], "masking_policy_id": "gdpr-standard", "conflict_policy": {}, "instance_overrides": {}}, headers=H).status_code == 200
    assert cb.get(f"/api/projects/{pid}", headers=H).json()["manifest"]["version"] == 1  # and B sees A's edit of B's own project
    assert cb.get("/api/persistence/status", headers=H).json()["aggregates_taken_from_other_instances"] > 0


def test_a_whole_refresh_runs_across_two_instances(env, tmp_path):
    """Create on A, plan on B, submit on A, approve on B (a different person), run on A: every step on whichever instance the balancer picked."""
    a, b, ca, cb = two(tmp_path)
    ca.post("/api/demo/bootstrap", headers=ROOT)
    i = ids(ca)
    pid = ca.post("/api/projects", json={"name": "split", "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H).json()["id"]
    assert cb.put(f"/api/projects/{pid}/manifest", json={"scope": {"object_type": "SALES_ORDER", "company_codes": ["1000"], "plants": []}, "last_days": 90,
                                                         "include_downstream": ["DELIVERY", "BILLING", "FI_DOCUMENT"], "masking_policy_id": "gdpr-standard",
                                                         "conflict_policy": {}, "instance_overrides": {}}, headers=H).status_code == 200
    assert ca.post(f"/api/projects/{pid}/plan", headers=H).status_code == 200
    assert cb.post(f"/api/projects/{pid}/conflicts/analyze", headers=H).status_code == 200
    adv = ca.get(f"/api/projects/{pid}/agents/masking", headers=H).json()
    assert cb.post(f"/api/projects/{pid}/masking/rules", json={"rules": adv["add_rules"]}, headers=H).status_code in (200, 201)
    assert ca.put(f"/api/projects/{pid}/conflicts/policy", json={"conflict_policy": {"DUPLICATE_DIFFERENT": "SKIP"}}, headers=H).status_code == 200
    assert cb.post(f"/api/projects/{pid}/plan", headers=H).status_code == 200
    assert ca.post(f"/api/projects/{pid}/conflicts/analyze", headers=H).status_code == 200
    assert cb.post(f"/api/projects/{pid}/submit", headers=H).status_code == 200
    carol = {"X-Demo-User": "carol.approver"}
    assert ca.post(f"/api/projects/{pid}/approve", json={"comment": "ok"}, headers=carol).status_code == 200
    assert cb.post(f"/api/projects/{pid}/approve", json={"comment": "again"}, headers=carol).status_code == 409  # already approved: the state B sees is A's
    r = cb.post(f"/api/projects/{pid}/execute", headers=H)
    assert r.status_code == 202, r.text
    run = ca.get(f"/api/runs/{r.json()['id']}", headers=H).json()
    assert run["status"] == "COMPLETED" and run["release"] == "RELEASED"
    rec = cb.get(f"/api/runs/{run['id']}/reconciliation", headers=H).json()
    assert not [c for c in rec["checks"] if c["status"] == "fail"]
    assert ca.get("/api/health").json()["audit"]["valid"] and cb.get("/api/health").json()["audit"]["valid"]
    # the separation of duties holds across instances: the submitter on B cannot approve what B submitted
    pid2 = ca.post("/api/projects", json={"name": "sod", "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H).json()["id"]
    assert pid2 != pid


def test_concurrent_writers_on_two_instances_lose_nothing(env, tmp_path):
    a, b = app(tmp_path, 1), app(tmp_path, 2)
    with TestClient(a) as ca, TestClient(b) as cb:
        ca.post("/api/demo/bootstrap", headers=ROOT)
        i = ids(ca)
        names, errors = [], []

        def worker(c, tag):
            for n in range(6):
                nm = f"{tag}-{n}"
                r = c.post("/api/projects", json={"name": nm, "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H)
                (names if r.status_code == 201 else errors).append(nm if r.status_code == 201 else (nm, r.status_code, r.text[:80]))
        ts = [threading.Thread(target=worker, args=(c, f"{t}{k}")) for k, c in enumerate((ca, cb)) for t in "xy"]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errors and len(names) == 24
        assert sorted(p["name"] for p in ca.get("/api/projects", headers=H).json()) == sorted(names)  # both instances saw every project, none was overwritten
        assert sorted(p["name"] for p in cb.get("/api/projects", headers=H).json()) == sorted(names)
        with raw(env["url"]) as cx:
            assert cx.execute("SELECT count(*) FROM rf_aggregates WHERE kind='project'").fetchone()[0] == 24
        assert ca.get("/api/health").json()["audit"]["valid"]


def test_the_write_lock_is_exclusive_across_instances(env, tmp_path):
    a, b, ca, cb = two(tmp_path)
    ca.post("/api/demo/bootstrap", headers=ROOT)
    sa, sb = a.state.svc.store, b.state.svc.store
    sa.begin_write()
    try:
        with pytest.raises(BackendError, match="another instance held the write lock"):
            sb.begin_write(timeout=0.3)
    finally:
        sa.end_write()
    sb.begin_write(timeout=2)  # free again
    sb.end_write()


def test_a_busy_instance_refuses_with_503_and_changes_nothing(env, tmp_path, monkeypatch):
    monkeypatch.setenv("RFACTORY_WRITE_LOCK_TIMEOUT", "0.4")
    a, b, ca, cb = two(tmp_path)
    ca.post("/api/demo/bootstrap", headers=ROOT)
    i = ids(ca)
    a.state.svc.store.begin_write()  # instance A is in the middle of a write
    try:
        r = cb.post("/api/projects", json={"name": "blocked", "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H)
        assert r.status_code == 503 and "write lock" in r.text
        assert cb.get("/api/projects", headers=H).json() == []  # reads are never blocked
    finally:
        a.state.svc.store.end_write()
    assert cb.post("/api/projects", json={"name": "after", "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H).status_code == 201
    assert [p["name"] for p in ca.get("/api/projects", headers=H).json()] == ["after"]


def test_a_crashed_holder_releases_the_lock(env, tmp_path):
    a, b, ca, cb = two(tmp_path)
    ca.post("/api/demo/bootstrap", headers=ROOT)
    sa, sb = a.state.svc.store, b.state.svc.store
    sa.begin_write()
    pid = sa.backend._lock_conn.info.backend_pid
    with pytest.raises(BackendError):
        sb.begin_write(timeout=0.2)
    with raw(env["url"]) as cx:
        cx.execute("SELECT pg_terminate_backend(%s)", (pid,))  # the instance's connection dies, as if the process had been killed
    sb.begin_write(timeout=3)  # PostgreSQL released the lock by itself
    sb.end_write()


def test_audit_entries_from_both_instances_form_one_valid_chain(env, tmp_path):
    a, b = app(tmp_path, 1), app(tmp_path, 2)
    la, lb = a.state.svc.audit, b.state.svc.audit
    base = len(la.entries())

    def worker(log, tag):
        for n in range(20):
            log.append(tag, "test.event", "res", {"n": n})
    ts = [threading.Thread(target=worker, args=(la, "A")), threading.Thread(target=worker, args=(lb, "B")), threading.Thread(target=worker, args=(la, "A2"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ea, eb = la.entries(), lb.entries()
    assert len(ea) == base + 60 and [e["seq"] for e in ea] == list(range(1, base + 61))
    assert ea == eb  # both instances read the same log
    v = la.verify()
    assert v["valid"] and v["signed"] and lb.verify()["valid"]
    assert {e["actor"] for e in ea if e["action"] == "test.event"} == {"A", "B", "A2"}
    h = la.head()
    assert h["seq"] == base + 60 and lb.verify(expected_head=h)["escrow_checked"]


def test_the_audit_table_is_append_only_and_tampering_is_detected(env, tmp_path):
    a = app(tmp_path, 1)
    log = a.state.svc.audit
    for n in range(5):
        log.append("x", "e", "r", {"n": n})
    with raw(env["url"]) as cx:
        for stmt in ("UPDATE rf_audit SET entry = '{}' WHERE seq = 2", "DELETE FROM rf_audit WHERE seq = 2", "TRUNCATE rf_audit"):
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                cx.execute(stmt)
        assert cx.execute("SELECT count(*) FROM rf_audit").fetchone()[0] >= 5
        # the database OWNER can still bypass the trigger: the signed chain is what then gives it away
        cx.execute("ALTER TABLE rf_audit DISABLE TRIGGER rf_audit_no_change")
        row = cx.execute("SELECT entry FROM rf_audit WHERE seq = 3").fetchone()[0]
        forged = json.loads(row)
        forged["details"] = {"n": 999}
        cx.execute("UPDATE rf_audit SET entry = %s WHERE seq = 3", (json.dumps(forged),))
    b = app(tmp_path, 2)
    v = b.state.svc.audit.verify()
    assert not v["valid"] and v["broken_at"] == 3


def test_the_data_key_can_be_rotated_while_another_instance_runs(env, tmp_path):
    a, b, ca, cb = two(tmp_path)
    ca.post("/api/demo/bootstrap", headers=ROOT)
    i = ids(cb)
    cb.post("/api/projects", json={"name": "before", "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H)
    r = ca.post("/api/persistence/rotate-data-key", headers=H)
    assert r.status_code == 200 and r.json()["blobs_reencrypted"] > 0
    # B still holds the old data key in memory; it notices at its next request and carries on, reading and writing
    assert [p["name"] for p in cb.get("/api/projects", headers=H).json()] == ["before"]
    assert cb.post("/api/projects", json={"name": "after", "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H).status_code == 201
    assert sorted(p["name"] for p in ca.get("/api/projects", headers=H).json()) == ["after", "before"]
    c = TestClient(app(tmp_path, 3))  # and a fresh instance opens everything with the same state key
    assert sorted(p["name"] for p in c.get("/api/projects", headers=H).json()) == ["after", "before"]


def test_instances_starting_together_on_an_empty_database_share_one_data_key(env, tmp_path, monkeypatch):
    import time
    from rfactory.persistence.store import StateStore
    real = StateStore._init_meta

    def slow(self):  # widen the window between "the database is empty" and "I created the keys"
        time.sleep(0.4)
        return real(self)
    monkeypatch.setattr(StateStore, "_init_meta", slow)
    out, errs = {}, []

    def start(n):
        try:
            out[n] = app(tmp_path, n)
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=start, args=(n,)) for n in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(out) == 4
    c = [TestClient(a) for a in out.values()]
    c[0].post("/api/demo/bootstrap", headers=ROOT)
    assert all(set(ids(x)) == {"EP1", "EQ1", "S4P", "S4Q"} for x in c)  # every one can read what one wrote: there is one data key
    with raw(env["url"]) as cx:
        assert cx.execute("SELECT count(*) FROM rf_meta WHERE key='wrapped_dek'").fetchone()[0] == 1


def test_only_changed_aggregates_are_written_and_the_version_moves_only_on_change(env, tmp_path):
    a, b, ca, cb = two(tmp_path)
    ca.post("/api/demo/bootstrap", headers=ROOT)
    sa = a.state.svc.store
    v0 = sa.backend.version()
    for _ in range(3):
        ca.get("/api/projects", headers=H)
        cb.get("/api/systems", headers=H)
    assert sa.backend.version() == v0  # reads never write
    i = ids(ca)
    cb.post("/api/projects", json={"name": "one", "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H)
    v1 = sa.backend.version()
    assert v1 == v0 + 1
    assert b.state.svc.store.last_save_stats["written"] <= 3  # the new project and what its creation touched, not the whole landscape
    ca.post("/api/persistence/checkpoint", headers=ROOT)
    assert sa.last_save_stats["written"] == 0  # nothing changed on A since it caught up: nothing is rewritten


def test_a_failed_save_rolls_back_completely(env, tmp_path):
    a = app(tmp_path, 1)
    ca = TestClient(a)
    ca.post("/api/demo/bootstrap", headers=ROOT)
    be = a.state.svc.store.backend
    before = (be.version(), be.list_hashes())
    with pytest.raises(psycopg.errors.NotNullViolation):
        be.commit([("project", "good", "h1", b"x"), ("project", "bad", "h2", None)], [("agents", "all")])
    assert (be.version(), be.list_hashes()) == before  # neither row was written, nothing deleted, the version did not move
    assert be.aggregate_count() == len(before[1])


def test_production_accepts_a_database_url_as_durable_state():
    from rfactory.api import hardening

    class A:
        mode = "oidc"
    probs = hardening.production_problems(A(), True, {"RFACTORY_STATE_KEY": "k", "RFACTORY_AUDIT_KEY": "a"})
    assert probs == []
    assert any("RFACTORY_DATABASE_URL" in p for p in hardening.production_problems(A(), False, {"RFACTORY_STATE_KEY": "k", "RFACTORY_AUDIT_KEY": "a"}))


def test_the_backend_reports_a_bad_database_clearly():
    with pytest.raises(BackendError, match="cannot connect"):
        PostgresBackend("postgresql://nobody@127.0.0.1:1/none")


def test_an_aggregate_deleted_by_one_instance_disappears_on_the_other(env, tmp_path):
    a, b, ca, cb = two(tmp_path)
    ca.post("/api/demo/bootstrap", headers=ROOT)
    i = ids(ca)
    pid = ca.post("/api/projects", json={"name": "temp", "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H).json()["id"]
    assert [p["name"] for p in cb.get("/api/projects", headers=H).json()] == ["temp"]
    sa = a.state.svc
    sa.store.begin_write()
    try:
        del sa.projects[pid]  # no API deletes a project today; the store must still carry a deletion between instances
        assert sa.checkpoint()["deleted"] == 1
    finally:
        sa.store.end_write()
    assert cb.get("/api/projects", headers=H).json() == []
    assert cb.get(f"/api/projects/{pid}", headers=H).status_code == 404


def test_offline_rotation_of_a_shared_database(env, tmp_path, monkeypatch):
    from rfactory.persistence import rotate as R
    ca = TestClient(app(tmp_path, 1))
    ca.post("/api/demo/bootstrap", headers=ROOT)
    i = ids(ca)
    ca.post("/api/projects", json={"name": "keep", "source_id": i["EP1"], "target_id": i["EQ1"]}, headers=H)
    with raw(env["url"]) as cx:
        old_blob = bytes(cx.execute("SELECT blob FROM rf_aggregates WHERE kind='agents'").fetchone()[0])
    out = R.rotate(None, rotate_data_key=True, database_url=env["url"])
    assert out["blobs_reencrypted"] > 5
    with raw(env["url"]) as cx:
        assert bytes(cx.execute("SELECT blob FROM rf_aggregates WHERE kind='agents'").fetchone()[0]) != old_blob
    new_kek = Fernet.generate_key()
    assert R.rotate(None, new_kek=new_kek, database_url=env["url"]) == {"kek_rotated": True}
    with pytest.raises(StoreError, match="state key does not match"):
        app(tmp_path, 2)  # the old key no longer opens the database
    monkeypatch.setenv("RFACTORY_STATE_KEY", new_kek.decode())
    c = TestClient(app(tmp_path, 3))
    assert [p["name"] for p in c.get("/api/projects", headers=H).json()] == ["keep"]  # rotated twice, nothing lost
    with pytest.raises(StoreError, match="no key file"):
        R.rotate(None, new_kek=new_kek, write_key_file=True, database_url=env["url"])
