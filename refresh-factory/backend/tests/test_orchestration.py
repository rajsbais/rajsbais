from datetime import datetime, timedelta, timezone

import pytest

from rfactory.orchestration import engine as oe
from rfactory.sap.adapter import TransientError
from rfactory.security.auth import DEMO_USERS as U, Forbidden
from rfactory.service import Conflict

from .conftest import ADMIN, ALICE, CAROL, make_project
from .test_delta import approved as approved_delta

SCHED, SVEN, COPILOT, TINA, ERIN = U["svc.scheduler"], U["sven.security"], U["refresh.copilot"], U["tina.tester"], U["erin.auditor"]
FRI_NOON = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
SAT_3AM = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)
SAT_7AM = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


def tick(svc, now=None, who=SCHED):
    return svc.orch.tick(who, now)


def agent_step(key, after=(), agent="landscape-discovery"):
    return {"key": key, "kind": "agent_run", "params": {"agent_id": agent}, "after": list(after)}


# ---- submission and authority
def test_submission_rules(svc):
    o = svc.orch
    with pytest.raises(Conflict):
        o.submit(ALICE, "format_disk")
    with pytest.raises(Conflict):
        o.submit(ALICE, "tdm_sweep", priority="urgent")
    with pytest.raises(Conflict):
        o.submit(ALICE, "agent_run", {"agent_id": "nope"})
    with pytest.raises(Conflict):
        o.submit(ALICE, "delta_run", {})
    for kind in ("tdm_sweep", "delta_run"):
        with pytest.raises(Forbidden):
            o.submit(TINA, kind, {"scenario_id": "x"})  # a tester lacks run:execute
        with pytest.raises(Forbidden):
            o.submit(COPILOT, kind, {"scenario_id": "x"})  # agents may not use the orchestrator at all
    assert o.submit(ERIN, "agent_run", {"agent_id": "landscape-discovery"}).status == "QUEUED"  # read-only work needs only view
    for fn in (lambda: o.tick(COPILOT), lambda: o.tick(TINA)):
        with pytest.raises(Forbidden):
            fn()


def test_idempotent_submission(svc):
    a = svc.orch.submit(ALICE, "tdm_sweep", idempotency_key="nightly-1")
    b = svc.orch.submit(ALICE, "tdm_sweep", idempotency_key="nightly-1")
    assert a.id == b.id and len(svc.orch.jobs) == 1


def test_jobs_run_by_priority_then_submission_order(svc):
    o = svc.orch
    low = o.submit(ALICE, "tdm_sweep", priority="low")
    n1 = o.submit(ALICE, "lean_sweep")
    n2 = o.submit(ALICE, "agent_run", {"agent_id": "performance-optimization"})
    hi = o.submit(ALICE, "agent_run", {"agent_id": "landscape-discovery"}, priority="high")
    out = tick(svc)
    assert [r["job"] for r in out["ran"]] == [hi.id, n1.id, n2.id, low.id]
    assert all(j.status == "SUCCEEDED" for j in o.jobs.values())
    assert hi.result["report_id"] in svc.agents.reports  # the agent really ran


def test_max_jobs_per_tick_and_serial_execution(svc):
    for _ in range(4):
        svc.orch.submit(ALICE, "tdm_sweep")
    assert len(tick(svc)["ran"]) == 4
    for _ in range(4):
        svc.orch.submit(ALICE, "tdm_sweep")
    assert len(svc.orch.tick(SCHED, max_jobs=3)["ran"]) == 3
    assert sum(j.status == "QUEUED" for j in svc.orch.jobs.values()) == 1


# ---- pipelines, dependencies, human gates
def test_pipeline_runs_in_dependency_order_and_cancels_dependents_of_failures(svc):
    o = svc.orch
    p = o.submit_pipeline(ALICE, {"name": "n", "steps": [agent_step("b", ["a"]), agent_step("a"), agent_step("c", ["b"])]})
    assert p["status"] == "RUNNING"
    tick(svc)
    assert o.pipeline(p["id"])["status"] == "SUCCEEDED"
    order = [j.step for j in sorted(o.jobs.values(), key=lambda j: j.finished)]
    assert order == ["a", "b", "c"]
    p2 = o.submit_pipeline(ALICE, {"steps": [{"key": "x", "kind": "agent_run", "params": {"agent_id": "refresh-documentation", "params": {"kind": "bogus", "id": "z"}}},
                                            agent_step("y", ["x"])]})
    tick(svc)
    st = {j["step"]: j for j in o.pipeline(p2["id"])["jobs_detail"]}
    assert st["x"]["status"] == "FAILED" and st["y"]["status"] == "CANCELLED" and "dependency" in st["y"]["reasons"][0]
    assert o.pipeline(p2["id"])["status"] == "FAILED"


def test_pipeline_validation_is_all_or_nothing(svc):
    o = svc.orch
    for steps in ([], [agent_step("a", ["b"]), agent_step("b", ["a"])], [agent_step("a", ["zzz"])], [agent_step("a"), agent_step("a")]):
        with pytest.raises(Conflict):
            o.submit_pipeline(ALICE, {"steps": steps})
    with pytest.raises(Conflict):
        o.submit_pipeline(ALICE, {"steps": [agent_step("a"), {"key": "b", "kind": "nope", "after": ["a"]}]})
    assert not o.jobs and not o.pipelines


def test_human_gate_blocks_until_a_different_authorised_human_completes_it(svc):
    o = svc.orch
    p = o.submit_pipeline(ALICE, {"steps": [agent_step("scan"), {"key": "review", "kind": "human_gate", "params": {"note": "check scan"}, "after": ["scan"]},
                                           {"key": "doc", "kind": "tdm_sweep", "after": ["review"]}]})
    tick(svc)
    gate = next(j for j in o.jobs.values() if j.step == "review")
    assert gate.status == "WAITING_HUMAN" and next(j for j in o.jobs.values() if j.step == "doc").status == "WAITING_DEPENDENCY"
    assert any(e["kind"] == "gate.pending" for e in o.events)
    tick(svc)
    assert gate.status == "WAITING_HUMAN"  # a tick never completes a gate
    for who in (ALICE, COPILOT, TINA, SCHED):
        with pytest.raises(Forbidden):
            o.complete_gate(who, gate.id)
    o.complete_gate(CAROL, gate.id, "looks fine")
    tick(svc)
    assert o.pipeline(p["id"])["status"] == "SUCCEEDED"
    with pytest.raises(Conflict):
        o.complete_gate(CAROL, gate.id)


def test_gate_escalates_once(svc):
    o = svc.orch
    g = o.submit(ALICE, "human_gate", {"note": "n", "escalate_after_hours": 2})
    tick(svc)
    later = o.now() + timedelta(hours=3)
    assert tick(svc, later)["escalated"] == [g.id]
    assert tick(svc, later + timedelta(hours=1))["escalated"] == []


def test_cancel_rules(svc):
    o = svc.orch
    g = o.submit(ALICE, "human_gate", {})
    for who in (COPILOT, TINA):
        with pytest.raises(Forbidden):
            o.cancel(who, g.id)
    assert o.cancel(ALICE, g.id).status == "CANCELLED"
    with pytest.raises(Conflict):
        o.cancel(ALICE, g.id)


# ---- the orchestrator adds no authority
def test_governance_of_the_underlying_module_still_applies(svc):
    o = svc.orch
    sc = svc.delta.create(ALICE, __import__("tests.test_delta", fromlist=["spec"]).spec(svc))  # created, never approved
    j = o.submit(ALICE, "delta_run", {"scenario_id": sc.id})
    tick(svc)
    assert j.status == "FAILED" and j.attempts == 1 and "must be approved" in j.reasons[0] and "not retried" in j.reasons[1]
    p = make_project(svc)
    j2 = o.submit(ALICE, "selective_execute", {"project_id": p.id})
    tick(svc)
    assert j2.status == "FAILED" and "not approved" in j2.reasons[0]
    sc2 = approved_delta(svc)
    sc2.conflict_policy = {"DUPLICATE_DIFFERENT": "QUARANTINE"}  # edited after approval: the approval no longer covers the config
    j3 = o.submit(ALICE, "delta_run", {"scenario_id": sc2.id})
    tick(svc)
    assert j3.status == "FAILED"


def test_approved_delta_scenario_runs_through_the_queue(svc):
    sc = approved_delta(svc)
    j = svc.orch.submit(ALICE, "delta_run", {"scenario_id": sc.id})
    assert j.target_id == sc.target_id
    tick(svc)
    assert j.status in ("SUCCEEDED", "ATTENTION") and j.result["version"] == 1
    assert svc.delta.get(sc.id).history[-1]["trigger"] == "schedule"
    assert svc.audit.verify()["valid"]


# ---- maintenance windows
def test_window_rules_overnight_wrap_and_blackouts(svc):
    o = svc.orch
    t = svc.tgt_id
    assert o.window_state(t, FRI_NOON)["open"]  # no rules: unrestricted
    o.set_windows(ALICE, t, {"allow": [{"weekdays": [4], "start": "22:00", "end": "06:00"}]})  # Friday night into Saturday
    at = lambda d, h, m=0: o.window_state(t, datetime(2026, 10, d, h, m, tzinfo=timezone.utc))
    assert not at(9, 12)["open"] and at(9, 23)["open"] and at(10, 3)["open"] and not at(10, 6)["open"] and not at(10, 7)["open"]
    assert at(17, 3)["open"] and not at(16, 3)["open"]  # the following Saturday morning belongs to the Friday rule
    assert at(9, 12)["next_open"].startswith("2026-10-09T22:00")
    o.set_windows(ALICE, t, {"allow": [{"weekdays": [4], "start": "22:00", "end": "06:00"}],
                             "blackouts": [{"from": "2026-10-10T00:00:00+00:00", "to": "2026-10-11T00:00:00+00:00", "reason": "quarter-end freeze"}]})
    s = at(10, 3)
    assert not s["open"] and "freeze" in s["reason"] and at(9, 23)["open"]
    o.set_windows(ALICE, t, {})
    assert at(10, 7)["open"]


def test_window_input_validation_and_authority(svc):
    o = svc.orch
    for bad in ({"allow": [{"start": "25:00", "end": "01:00"}]}, {"allow": [{"start": "01:00", "end": "01:00"}]}, {"allow": [{"weekdays": [9], "start": "01:00", "end": "02:00"}]},
                {"allow": [{"start": "bad", "end": "02:00"}]}, {"blackouts": [{"from": "2026-10-10T00:00:00", "to": "2026-10-09T00:00:00"}]}):
        with pytest.raises(Conflict):
            o.set_windows(ALICE, svc.tgt_id, bad)
    with pytest.raises(Conflict):
        o.set_windows(ALICE, svc.src_id, {})  # production
    for who in (COPILOT, CAROL, TINA, SCHED):
        with pytest.raises(Forbidden):
            o.set_windows(who, svc.tgt_id, {})


def test_job_waits_for_its_window_and_runs_when_it_opens(svc):
    o = svc.orch
    sc = approved_delta(svc)
    o.set_windows(ALICE, sc.target_id, {"allow": [{"weekdays": [5], "start": "02:00", "end": "06:00"}]})
    j = o.submit(ALICE, "delta_run", {"scenario_id": sc.id})
    out = tick(svc, FRI_NOON)
    assert j.status == "WAITING_WINDOW" and out["waiting"][0]["why"].startswith("window closed") and not svc.delta.get(sc.id).history
    tick(svc, FRI_NOON + timedelta(hours=1))
    assert sum(e["kind"] == "job.waiting_window" for e in o.events) == 1  # announced once, not every tick
    tick(svc, SAT_3AM)
    assert j.status in ("SUCCEEDED", "ATTENTION") and svc.delta.get(sc.id).history
    j2 = o.submit(ALICE, "delta_run", {"scenario_id": sc.id})
    tick(svc, SAT_7AM)
    assert j2.status == "WAITING_WINDOW"  # closed again


def test_simulated_clock_moves_windows(svc):
    o = svc.orch
    o.set_windows(ALICE, svc.tgt_id, {"blackouts": [{"from": (o.now() - timedelta(hours=1)).isoformat(), "to": (o.now() + timedelta(hours=5)).isoformat()}]})
    assert not o.window_state(svc.tgt_id)["open"]
    for who in (COPILOT, TINA, CAROL):
        with pytest.raises(Forbidden):
            o.advance_clock(who, 6)
    o.advance_clock(ALICE, 6)
    s = o.window_state(svc.tgt_id)
    assert s["open"] and s["simulated_clock"]
    assert o.summary()["simulated_clock"]


# ---- leases
def test_a_leased_target_blocks_jobs_until_released(svc):
    o = svc.orch
    sc = approved_delta(svc)
    o.acquire(sc.target_id, "fr-test", "full_refresh")
    with pytest.raises(Conflict):
        o.acquire(sc.target_id, "someone-else", "job")
    o.acquire(sc.target_id, "fr-test", "full_refresh")  # re-entrant for the holder
    j = o.submit(ALICE, "delta_run", {"scenario_id": sc.id})
    out = tick(svc)
    assert j.status == "WAITING_LEASE" and "fr-test" in out["waiting"][0]["why"]
    o.release("fr-test")
    tick(svc)
    assert j.status in ("SUCCEEDED", "ATTENTION")
    assert not o.leases  # a job's own lease is dropped when it ends


def test_full_refresh_holds_the_target_lease_and_blocks_orchestrated_jobs(svc):
    from .test_fullrefresh import prepare, approve_postcopy
    p = prepare(svc)
    sc = approved_delta(svc)  # same target (EQ1)
    assert sc.target_id == p.target_id
    svc.full.run(ALICE, p.id)
    assert svc.orch.leases[p.target_id]["holder"] == p.id
    j = svc.orch.submit(ALICE, "delta_run", {"scenario_id": sc.id})
    tick(svc)
    assert j.status == "WAITING_LEASE"
    svc.full.rollback(ALICE, p.id)
    assert p.target_id not in svc.orch.leases
    tick(svc)
    assert j.status in ("SUCCEEDED", "ATTENTION", "FAILED")  # ran (it may fail on its own merits, but it was no longer blocked)
    assert j.attempts == 1


def test_two_full_refresh_programs_cannot_hold_one_target(svc):
    from .test_fullrefresh import prepare
    a, b = prepare(svc), prepare(svc)
    svc.full.run(ALICE, a.id)
    with pytest.raises(Conflict, match="leased"):
        svc.full.run(ALICE, b.id)
    svc.full.rollback(ALICE, a.id)
    svc.full.run(ALICE, b.id)
    assert svc.orch.leases[b.target_id]["holder"] == b.id


def test_full_refresh_waits_for_its_maintenance_window_before_the_copy(svc):
    from .test_fullrefresh import prepare, approve_postcopy
    p = prepare(svc)
    o = svc.orch
    o.set_windows(ALICE, p.target_id, {"blackouts": [{"from": (o.now() - timedelta(hours=1)).isoformat(), "to": (o.now() + timedelta(hours=8)).isoformat(), "reason": "freeze"}]})
    tgt = svc.adapters[p.target_id]
    before = tgt.data["KNA1"][0]["NAME1"]
    svc.full.run(ALICE, p.id)
    assert p.status == "WAITING" and p.checkpoint == 6 and "window closed" in p.waiting_for[0] and not p.unmasked_target
    assert tgt.data["KNA1"][0]["NAME1"] == before  # nothing was copied
    o.advance_clock(ALICE, 9)
    svc.full.run(ALICE, p.id)
    assert p.checkpoint == 7 and p.unmasked_target


# ---- retry
def test_transient_errors_retry_with_backoff_then_succeed(svc, monkeypatch):
    o = svc.orch
    calls = {"n": 0}

    def flaky(actor, now=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("RFC timeout")
        return {"reservations_released": [], "datasets_expired": []}
    monkeypatch.setattr(svc.tdm, "sweep", flaky)
    j = o.submit(ALICE, "tdm_sweep")
    t0 = o.now()
    tick(svc, t0)
    assert j.status == "RETRY_WAIT" and j.next_attempt_at == t0 + timedelta(seconds=60)
    assert tick(svc, t0 + timedelta(seconds=30))["ran"] == []  # too early
    tick(svc, t0 + timedelta(seconds=61))
    assert j.status == "RETRY_WAIT" and j.attempts == 2 and j.next_attempt_at == t0 + timedelta(seconds=61 + 120)
    tick(svc, t0 + timedelta(seconds=61 + 121))
    assert j.status == "SUCCEEDED" and j.attempts == 3
    assert sum(e["kind"] == "job.retry" for e in o.events) == 2


def test_persistent_transient_error_dead_letters(svc, monkeypatch):
    monkeypatch.setattr(svc.lean, "sweep", lambda actor, now=None: (_ for _ in ()).throw(TimeoutError("storage")))
    j = svc.orch.submit(ALICE, "lean_sweep", max_attempts=2)
    t = svc.orch.now()
    tick(svc, t)
    tick(svc, t + timedelta(minutes=5))
    assert j.status == "DEAD" and j.attempts == 2 and "persisted" in j.reasons[0]
    assert any(e["kind"] == "job.dead" and e["severity"] == "error" for e in svc.orch.events)


# ---- schedules
def sched_spec(**kw):
    return {"name": "nightly hygiene", "schedule": {"kind": "daily", "hour": 2, "window_hours": 3},
            "template": {"type": "pipeline", "steps": [{"key": "t", "kind": "tdm_sweep"}, agent_step("a", ["t"], "compliance-verification")]}, **kw}


def test_schedule_needs_a_different_approver_and_fires_once_per_occurrence(svc):
    o = svc.orch
    s = o.create_schedule(ALICE, sched_spec())
    due = datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc)
    assert tick(svc, due)["fired"] == []  # unapproved: never fires
    for who in (ALICE, COPILOT, TINA, SCHED):
        with pytest.raises(Forbidden):
            o.approve_schedule(who, s.id)
    o.skew = datetime(2026, 10, 9, 12, tzinfo=timezone.utc) - datetime.now(timezone.utc)
    o.approve_schedule(CAROL, s.id)
    assert s.next_due == due
    assert tick(svc, due - timedelta(minutes=1))["fired"] == []
    out = tick(svc, due + timedelta(minutes=5))
    assert len(out["fired"]) == 1 and len(o.jobs) == 2
    assert [r["status"] for r in out["ran"]] == ["SUCCEEDED", "SUCCEEDED"]
    assert tick(svc, due + timedelta(minutes=10))["fired"] == [] and len(o.jobs) == 2
    assert s.next_due == due + timedelta(days=1)


def test_edited_schedule_stops_firing_until_reapproved(svc):
    o = svc.orch
    s = o.create_schedule(ALICE, sched_spec())
    o.approve_schedule(CAROL, s.id)
    s.template["steps"].append({"key": "x", "kind": "lean_sweep"})
    assert not s.public()["approved"]
    assert tick(svc, s.next_due + timedelta(minutes=1))["fired"] == []
    o.approve_schedule(CAROL, s.id)
    assert s.public()["approved"]


def test_missed_occurrence_is_recorded_not_replayed(svc):
    o = svc.orch
    s = o.create_schedule(ALICE, sched_spec())
    o.approve_schedule(CAROL, s.id)
    due = s.next_due
    out = tick(svc, due + timedelta(hours=9))  # scheduler was down far longer than window_hours
    assert out["fired"] == [] and out["missed"][0]["due"] == due.isoformat() and not o.jobs
    assert any(e["kind"] == "schedule.missed" for e in o.events)
    assert s.next_due > due + timedelta(hours=9)


def test_paused_schedule_does_not_fire_and_resume_recomputes(svc):
    o = svc.orch
    s = o.create_schedule(ALICE, {**sched_spec(), "template": {"type": "job", "kind": "tdm_sweep"}})
    o.approve_schedule(CAROL, s.id)
    o.pause_schedule(ALICE, s.id, True)
    assert tick(svc, s.next_due + timedelta(minutes=1))["fired"] == []
    o.pause_schedule(ALICE, s.id, False)
    assert s.next_due > o.now()


def test_schedule_validation(svc):
    o = svc.orch
    for bad in ({"schedule": {"kind": "cron"}}, {"schedule": {"kind": "daily", "hour": 30}}, {"schedule": {}},
                {"template": {"type": "job", "kind": "human_gate"}}, {"template": {"type": "job", "kind": "nope"}}, {"template": {"type": "x"}},
                {"template": {"type": "pipeline", "steps": []}}):
        with pytest.raises(Conflict):
            o.create_schedule(ALICE, {**sched_spec(), **bad})
    with pytest.raises(Forbidden):
        o.create_schedule(COPILOT, sched_spec())


# ---- notifications
def test_notifications_filtered_delivered_and_free_of_data(svc):
    o = svc.orch
    sub = o.subscribe(ALICE, {"channel": "webhook", "destination": "https://hooks.example.test/refresh", "events": ["job.failed", "job.succeeded"], "min_severity": "error"})
    o.submit(ALICE, "agent_run", {"agent_id": "refresh-documentation", "params": {"kind": "bogus", "id": "x"}})
    o.submit(ALICE, "tdm_sweep")
    tick(svc)
    assert len(o.outbox) == 1 and o.outbox[0]["subscription"] == sub["id"]  # only the error, not the success
    assert o.outbox[0]["status"] == "DELIVERED_SIMULATED"
    assert "KNA1" not in o.outbox[0]["body"] and "NAME1" not in str(o.outbox)
    assert any(e["kind"] == "job.succeeded" for e in o.events)  # the in-app feed still has everything


def test_failing_channel_is_retried_then_dropped_without_stopping_the_queue(svc):
    o = svc.orch

    class Broken(oe.Notifier):
        name = "broken"

        def send(self, *a):
            raise ConnectionError("smtp down")
    o.notifier = Broken()
    o.subscribe(ALICE, {"channel": "email", "destination": "ops@example.test", "events": ["job.failed"]})
    o.submit(ALICE, "agent_run", {"agent_id": "refresh-documentation", "params": {"kind": "bogus", "id": "x"}})
    for _ in range(4):
        tick(svc)
        o.submit(ALICE, "tdm_sweep")  # queue keeps working
    assert o.outbox[0]["status"] == "DROPPED" and o.outbox[0]["attempts"] == 3
    assert sum(j.status == "SUCCEEDED" for j in o.jobs.values()) >= 3
    assert any(e["action"] == "orchestration.notification_dropped" for e in svc.audit.entries())


def test_subscription_validation_and_authority(svc):
    o = svc.orch
    for bad in ({"channel": "pigeon", "destination": "x"}, {"channel": "email", "destination": ""}, {"channel": "email", "destination": "a", "events": ["nope"]},
                {"channel": "email", "destination": "a", "min_severity": "loud"}):
        with pytest.raises(Conflict):
            o.subscribe(ALICE, bad)
    for who in (COPILOT, TINA, CAROL):
        with pytest.raises(Forbidden):
            o.subscribe(who, {"channel": "email", "destination": "a"})


def test_agent_findings_raise_an_event(svc):
    svc.orch.submit(ALICE, "agent_run", {"agent_id": "landscape-discovery"})  # demo landscape has writable production -> high priority rec
    tick(svc)
    assert any(e["kind"] == "agent.findings" for e in svc.orch.events)


def test_orchestration_over_http(tmp_path):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    c = TestClient(create_app(tmp_path))
    H = lambda u: {"X-Demo-User": u}
    b = c.post("/api/demo/bootstrap", headers=H("root.admin")).json()
    tgt = b["target"]["id"]
    assert c.post("/api/orchestration/jobs", json={"kind": "tdm_sweep"}, headers=H("refresh.copilot")).status_code == 403
    assert c.post("/api/orchestration/jobs", json={"kind": "nope"}, headers=H("alice.basis")).status_code == 409
    assert c.post("/api/orchestration/jobs", json={"kind": "delta_run", "params": {}}, headers=H("alice.basis")).status_code in (409, 422)
    r = c.post("/api/orchestration/pipelines", json={"name": "p", "steps": [{"key": "a", "kind": "tdm_sweep"},
                                                                            {"key": "g", "kind": "human_gate", "after": ["a"], "params": {"note": "ok?"}}]}, headers=H("alice.basis"))
    assert r.status_code == 201
    assert c.post("/api/orchestration/tick", headers=H("refresh.copilot")).status_code == 403
    out = c.post("/api/orchestration/tick", headers=H("svc.scheduler")).json()
    assert len(out["ran"]) == 1
    gate = next(j for j in c.get("/api/orchestration/jobs", headers=H("erin.auditor")).json() if j["kind"] == "human_gate")
    assert c.post(f"/api/orchestration/jobs/{gate['id']}/complete", json={}, headers=H("alice.basis")).status_code == 403
    assert c.post(f"/api/orchestration/jobs/{gate['id']}/complete", json={"note": "fine"}, headers=H("carol.approver")).json()["status"] == "SUCCEEDED"
    w = c.put(f"/api/orchestration/windows/{tgt}", json={"allow": [{"weekdays": [0], "start": "01:00", "end": "02:00"}]}, headers=H("alice.basis"))
    assert w.status_code == 200 and "open" in w.json()
    assert c.put(f"/api/orchestration/windows/{b['source']['id']}", json={}, headers=H("alice.basis")).status_code == 409
    s = c.post("/api/orchestration/schedules", json={"schedule": {"kind": "daily", "hour": 3}, "template": {"type": "job", "kind": "lean_sweep"}}, headers=H("alice.basis")).json()
    assert c.post(f"/api/orchestration/schedules/{s['id']}/approve", headers=H("alice.basis")).status_code == 403
    assert c.post(f"/api/orchestration/schedules/{s['id']}/approve", headers=H("carol.approver")).json()["approved"]
    assert c.post("/api/orchestration/subscriptions", json={"channel": "email", "destination": "ops@example.test"}, headers=H("alice.basis")).status_code == 201
    summ = c.get("/api/orchestration/summary", headers=H("erin.auditor")).json()
    assert summ["notifier"] == "recording-stand-in" and summ["limits"] and "tdm_sweep" in summ["kinds"]
    assert c.get("/api/orchestration/events", headers=H("erin.auditor")).json()
