"""Live cutover execution: the timeline against the clock with timings observed from the platform's runs, the
downtime clock and projection, incidents with their escalation path blocking GO, task assignments; API and CLI."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from sdtf.cli import main as cli_main
from sdtf.cutover import execution as ex
from sdtf.cutover import rehearsal as reh
from sdtf.models import AuditEvent, CutoverRehearsal, ScopeManifest
from sdtf.runtime.pipeline import start_run

API = "/api/v1"


@pytest.fixture()
def rehearsal(session, slice_result):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    for r in session.execute(select(CutoverRehearsal).where(CutoverRehearsal.manifest_id == m.id)).scalars().all():
        session.delete(r)
    session.flush()
    r = reh.create_rehearsal(session, m, "Dress rehearsal", "DRESS", "architect")
    yield r
    for x in session.execute(select(CutoverRehearsal).where(CutoverRehearsal.manifest_id == m.id)).scalars().all():
        session.delete(x)
    session.flush()


def test_observed_timings_timeline_and_downtime_clock(session, rehearsal, slice_result):
    r = rehearsal
    obs = ex.observed_timings(session, r)
    assert set(obs) >= {"T04", "T08"} and all(v["observed"] and v["by"] == "platform" and v["actual_minutes"] >= 0 for v in obs.values())
    assert obs["T04"]["source"].startswith("run ") and "EXTRACT" in obs["T04"]["source"] and "RECONCILE" in obs["T08"]["source"]
    # before the start: every task waits; the run the platform did earlier is history on T04 / T08, not a task done here
    tl = ex.timeline(session, r)
    assert tl["status"] == "PLANNED" and tl["started_at"] is None and tl["counts"]["WAITING"] == len(r.runbook) and tl["planned_total_minutes"] > 0
    t04 = next(x for x in tl["tasks"] if x["id"] == "T04")
    assert tl["observed_tasks"] == [] and t04["status"] == "WAITING" and t04["history"]["source"].startswith("run ") and t04["history"]["observed"]
    assert tl["downtime"]["planned_minutes"] == round(sum(t["est_minutes"] for t in r.runbook if t["downtime"]), 1) and not tl["downtime"]["running"]
    # started: T01 and T02 are ready (no dependencies), T03 waits for T01; planned windows from the start
    reh.start_rehearsal(session, r, "operator")
    now = r.started_at + timedelta(minutes=5)
    tl = ex.timeline(session, r, now=now)
    by = {x["id"]: x for x in tl["tasks"]}
    assert by["T01"]["status"] == "READY" and by["T02"]["status"] == "READY" and by["T03"]["status"] == "WAITING" and by["T04"]["status"] == "WAITING"
    assert by["T01"]["planned_start"] == r.started_at.isoformat() and by["T03"]["planned_offset_min"] == 30 and tl["next"][:2] == ["T01", "T02"]
    assert tl["elapsed_minutes"] == 5.0 and tl["late"] == []
    # the platform runs the initial load during the rehearsal: T04 and the reconciliation are observed, done
    run = start_run(session, r.project_id, r.manifest_id, slice_result["ruleset_id"], "operator", "SIMULATED", 2)
    assert run.status == "COMPLETED"
    tl = ex.timeline(session, r, now=run.finished_at + timedelta(minutes=1))
    by = {x["id"]: x for x in tl["tasks"]}
    assert by["T04"]["status"] == "DONE" and by["T04"]["observed"] and by["T04"]["timed_by"] == "platform" and run.id[:8] in by["T04"]["source"] and by["T04"]["history"] is None
    assert "T04" in tl["observed_tasks"] and "T08" in tl["observed_tasks"] and by["T08"]["status"] == "DONE" and tl["counts"]["DONE"] == 2
    # a hand timing wins over the observation and the task is late against its plan
    reh.time_task(session, r, "T04", "start", "operator")
    tl = ex.timeline(session, r, now=r.started_at + timedelta(hours=40))
    by = {x["id"]: x for x in tl["tasks"]}
    assert by["T04"]["status"] == "RUNNING" and by["T04"]["observed"] is False and by["T04"]["late_minutes"] > 0 and "T04" in tl["late"] and by["T01"]["late_minutes"] > 0
    t = reh.time_task(session, r, "T04", "finish", "operator", "initial load done")
    tl = ex.timeline(session, r, now=r.started_at + timedelta(hours=40))
    by = {x["id"]: x for x in tl["tasks"]}
    assert by["T04"]["status"] == "DONE" and by["T04"]["actual_minutes"] == t["actual_minutes"] and by["T04"]["variance_minutes"] == round(t["actual_minutes"] - by["T04"]["est_minutes"], 1)
    assert ex.effective_timings(session, r)["T04"]["also_observed"]["observed"] is True
    # downtime clock runs from the first downtime task started
    reh.time_task(session, r, "T06", "start", "operator")
    started = datetime.fromisoformat(r.timings["T06"]["started_at"])
    tl = ex.timeline(session, r, now=started + timedelta(minutes=12))
    assert tl["downtime"]["running"] and tl["downtime"]["elapsed_minutes"] == 12.0 and tl["downtime"]["started_at"] == started.isoformat() and tl["downtime"]["projected_minutes"] > tl["downtime"]["elapsed_minutes"]
    assert datetime.fromisoformat(tl["projected_end"]) > started and tl["projected_total_minutes"] > tl["elapsed_minutes"]


def test_incidents_escalation_assignments_and_go(session, rehearsal):
    r = rehearsal
    reh.start_rehearsal(session, r, "operator")
    with pytest.raises(ValueError, match="not in the runbook"):
        ex.raise_incident(session, r, "T99", "LOW", "x", "operator")
    with pytest.raises(ValueError, match="severity"):
        ex.raise_incident(session, r, "T06", "URGENT", "x", "operator")
    with pytest.raises(ValueError, match="needs a title"):
        ex.raise_incident(session, r, "T06", "LOW", "  ", "operator")
    low = ex.raise_incident(session, r, "T06", "low", "IDoc queue still draining", "operator", "inbound queue has 40 IDocs")
    assert low["status"] == "OPEN" and low["severity"] == "LOW" and low["level"] == 0 and low["path"] == ex.escalation_path("Business/Basis") and low["owner"] == "Business/Basis"
    crit = ex.raise_incident(session, r, "T07", "CRITICAL", "final delta failed", "operator")
    assert crit["status"] == "ESCALATED" and crit["level"] == 1 and crit["escalations"][0]["to"] == "Migration lead" and crit["escalations"][0]["by"] == "platform"
    assert r.summary["incidents_open"] == 2 and r.summary["blocking_incidents"] == [crit["id"]] and r.summary["ready_for_go"] is False
    # GO is refused by the open critical incident even if the checklist allowed it
    with pytest.raises(ValueError, match="GO refused"):
        reh.complete_rehearsal(session, r, "GO", "approver")
    assert r.status == "IN_PROGRESS"
    # escalation walks the owner's path; a named target is recorded; beyond the last level is refused
    e1 = ex.escalate_incident(session, r, crit["id"], "operator", "no root cause after 20 min")
    assert e1["level"] == 2 and e1["escalations"][-1]["to"] == "Cutover manager" and e1["escalations"][-1]["note"] == "no root cause after 20 min"
    e2 = ex.escalate_incident(session, r, crit["id"], "operator", to="J. Doe (war room)")
    assert e2["level"] == 3 and e2["escalations"][-1]["to"] == "J. Doe (war room)"  # the path's last level is the steering committee
    with pytest.raises(ValueError, match="last escalation level"):
        ex.escalate_incident(session, r, crit["id"], "operator")
    with pytest.raises(LookupError):
        ex.escalate_incident(session, r, "nope", "operator")
    with pytest.raises(ValueError, match="needs a note"):
        ex.resolve_incident(session, r, crit["id"], "operator", " ")
    res = ex.resolve_incident(session, r, crit["id"], "operator", "delta replayed after the lock was released")
    assert res["status"] == "RESOLVED" and res["resolved_by"] == "operator" and r.summary["blocking_incidents"] == [] and r.summary["incidents_open"] == 1
    with pytest.raises(ValueError, match="already resolved"):
        ex.resolve_incident(session, r, crit["id"], "operator", "again")
    # assignments: set, read, clear; the summary names the downtime tasks without an assignee
    with pytest.raises(ValueError, match="not in the runbook"):
        ex.assign_task(session, r, "T99", "operator", "A")
    a = ex.assign_task(session, r, "T06", "operator", "Basis on-call", "Integration lead", "war room bridge")
    assert a["assignee"] == "Basis on-call" and r.assignments["T06"]["backup"] == "Integration lead" and r.summary["tasks_assigned"] == 1
    assert "T06" not in r.summary["unassigned_downtime"] and "T07" in r.summary["unassigned_downtime"]
    tab = {x["task"]: x for x in ex.assignment_table(r)}
    assert tab["T06"]["contact"] == "war room bridge" and tab["T07"]["assignee"] == ""
    assert ex.assign_task(session, r, "T06", "operator", "") == {} and "T06" not in (r.assignments or {})
    tl = ex.timeline(session, r)
    assert tl["incidents"] == {"open": 1, "blocking": [], "by_severity": {"LOW": 1, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0}} and tl["assignments"]["assigned"] == 0
    assert next(x for x in tl["tasks"] if x["id"] == "T06")["open_incidents"] == 1 and next(x for x in tl["tasks"] if x["id"] == "T06")["highest_open_severity"] == "LOW"
    md = reh.rehearsal_markdown(r)
    assert "## Incidents" in md and "final delta failed" in md and "Migration lead → Cutover manager → J. Doe (war room)" in md
    kinds = [e.action for e in session.execute(select(AuditEvent).where(AuditEvent.subject_id == r.manifest_id)).scalars().all()]
    assert {"CUTOVER_INCIDENT_RAISED", "CUTOVER_INCIDENT_ESCALATED", "CUTOVER_INCIDENT_RESOLVED", "CUTOVER_TASK_ASSIGNED"} <= set(kinds)
    # frozen after completion
    reh.complete_rehearsal(session, r, "NO_GO", "approver", "incident analysis first")
    with pytest.raises(ValueError, match="frozen"):
        ex.raise_incident(session, r, "T06", "LOW", "late", "operator")
    with pytest.raises(ValueError, match="frozen"):
        ex.assign_task(session, r, "T06", "operator", "A")


def test_execution_api_and_cli(client, tokens, session, slice_result, capsys):
    mid = slice_result["manifest_id"]
    for x in session.execute(select(CutoverRehearsal).where(CutoverRehearsal.manifest_id == mid)).scalars().all():
        session.delete(x)
    session.commit()
    r = client.post(f"{API}/manifests/{mid}/cutover/rehearsals", json={"name": "Go-live", "kind": "FINAL"}, headers=tokens["operator"])
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert client.post(f"{API}/cutover/rehearsals/{rid}/start", json={}, headers=tokens["operator"]).status_code == 200
    tl = client.get(f"{API}/cutover/rehearsals/{rid}/timeline", headers=tokens["viewer"]).json()
    assert tl["status"] == "IN_PROGRESS" and len(tl["tasks"]) == len(reh.TEMPLATE) and next(x for x in tl["tasks"] if x["id"] == "T04")["history"]["source"].startswith("run ")
    assert client.post(f"{API}/cutover/rehearsals/{rid}/incidents", json={"task": "T06", "severity": "HIGH", "title": "x"}, headers=tokens["viewer"]).status_code == 403
    assert client.post(f"{API}/cutover/rehearsals/{rid}/incidents", json={"task": "T06", "severity": "SEVERE", "title": "x"}, headers=tokens["operator"]).status_code == 422
    assert client.post(f"{API}/cutover/rehearsals/{rid}/incidents", json={"task": "T99", "severity": "HIGH", "title": "x"}, headers=tokens["operator"]).status_code == 409
    inc = client.post(f"{API}/cutover/rehearsals/{rid}/incidents", json={"task": "T06", "severity": "HIGH", "title": "interface stop list incomplete", "detail": "two IDoc partners missing"}, headers=tokens["operator"])
    assert inc.status_code == 201 and inc.json()["status"] == "OPEN"
    iid = inc.json()["id"]
    go = client.post(f"{API}/cutover/rehearsals/{rid}/complete", json={"verdict": "GO"}, headers=tokens["approver"])
    assert go.status_code == 409 and "GO refused" in go.json()["detail"]
    assert client.post(f"{API}/cutover/rehearsals/{rid}/incidents/nope/escalate", json={}, headers=tokens["operator"]).status_code == 404
    esc = client.post(f"{API}/cutover/rehearsals/{rid}/incidents/{iid}/escalate", json={"note": "owner unreachable"}, headers=tokens["operator"]).json()
    assert esc["level"] == 1 and esc["escalations"][0]["to"] == "Business cutover lead"
    assert client.post(f"{API}/cutover/rehearsals/{rid}/incidents/{iid}/resolve", json={"resolution": ""}, headers=tokens["operator"]).status_code == 409
    assert client.post(f"{API}/cutover/rehearsals/{rid}/incidents/{iid}/resolve", json={"resolution": "partners added"}, headers=tokens["operator"]).json()["status"] == "RESOLVED"
    a = client.put(f"{API}/cutover/rehearsals/{rid}/assignments/T06", json={"assignee": "Basis on-call", "contact": "bridge 1"}, headers=tokens["operator"])
    assert a.status_code == 200 and a.json()["assignment"]["assignee"] == "Basis on-call" and a.json()["summary"]["tasks_assigned"] == 1
    assert client.put(f"{API}/cutover/rehearsals/{rid}/assignments/T99", json={"assignee": "x"}, headers=tokens["operator"]).status_code == 409
    tab = client.get(f"{API}/cutover/rehearsals/{rid}/assignments", headers=tokens["viewer"]).json()
    assert next(x for x in tab if x["task"] == "T06")["assignee"] == "Basis on-call"
    full = client.get(f"{API}/cutover/rehearsals/{rid}", headers=tokens["viewer"]).json()
    assert len(full["incidents"]) == 1 and full["assignments"]["T06"]["contact"] == "bridge 1"
    tl = client.get(f"{API}/cutover/rehearsals/{rid}/timeline", headers=tokens["viewer"]).json()
    assert tl["incidents"]["open"] == 0 and tl["assignments"]["assigned"] == 1 and "T06" not in tl["assignments"]["unassigned_downtime"]
    # CLI
    assert cli_main(["cutover-rehearsal", "assign", "--id", rid, "--task", "T07", "--assignee", "Migration lead", "--backup", "M. Lee"]) == 0
    assert "T07: Migration lead (backup M. Lee)" in capsys.readouterr().out
    assert cli_main(["cutover-rehearsal", "incident", "--id", rid, "--action", "raise", "--task", "T07", "--severity", "CRITICAL", "--title", "final delta failed"]) == 0
    out = capsys.readouterr().out
    assert "raised on T07 (CRITICAL, ESCALATED, escalated to Migration lead)" in out
    cid = out.split()[0]
    assert cli_main(["cutover-rehearsal", "incident", "--id", rid, "--action", "escalate", "--incident", cid, "--note", "x"]) == 0
    assert "level 2: Cutover manager" in capsys.readouterr().out
    assert cli_main(["cutover-rehearsal", "incident", "--id", rid, "--action", "resolve", "--incident", "nope", "--note", "x"]) == 2
    assert "not found" in capsys.readouterr().err
    assert cli_main(["cutover-rehearsal", "incident", "--id", rid, "--action", "resolve", "--incident", cid, "--note", "replayed"]) == 0
    capsys.readouterr()
    assert cli_main(["cutover-rehearsal", "timeline", "--id", rid]) == 0
    out = capsys.readouterr().out
    assert "assigned 2/13" in out and "T04 WAITING" in out and "(history: run " in out
    assert cli_main(["cutover-rehearsal", "timeline", "--id", rid, "--json"]) == 0
    assert '"projected_end"' in capsys.readouterr().out
    assert cli_main(["cutover-rehearsal", "abort", "--id", rid, "--note", "test"]) == 0
    capsys.readouterr()
    for x in session.execute(select(CutoverRehearsal).where(CutoverRehearsal.manifest_id == mid)).scalars().all():
        session.delete(x)
    session.commit()
