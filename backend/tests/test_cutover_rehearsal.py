"""Cutover rehearsal checklist: automatic readiness items from the platform state, manual items ticked by hand,
runbook task timings fed back into the forecast, lessons, approver verdict, API and CLI."""
import pytest
from sqlalchemy import select

from sdtf.agents.catalog import agent_catalog  # registers the agents
from sdtf.agents.framework import REGISTRY
from sdtf.cli import main as cli_main
from sdtf.cutover import rehearsal as reh
from sdtf.cutover.service import generate_runbook
from sdtf.models import (
    AgentDecision,
    ApprovalRecord,
    AuditEvent,
    CockpitAttempt,
    CockpitFeedback,
    CutoverRehearsal,
    SapSystem,
    ScopeManifest,
)
from sdtf.runtime.cockpit_export import export_cockpit_files
from sdtf.runtime.cockpit_feedback import import_feedback, sample_feedback

API = "/api/v1"


def _wipe(session, manifest_id, run_id):
    for r in session.execute(select(CutoverRehearsal).where(CutoverRehearsal.manifest_id == manifest_id)).scalars().all():
        session.delete(r)
    for a in session.execute(select(CockpitAttempt).where(CockpitAttempt.run_id == run_id)).scalars().all():
        session.delete(a)
    for f in session.execute(select(CockpitFeedback).where(CockpitFeedback.run_id == run_id)).scalars().all():
        session.delete(f)
    for a in session.execute(select(ApprovalRecord).where(ApprovalRecord.subject_type.in_(("RECONCILIATION", "REHEARSAL")), ApprovalRecord.subject_id == run_id)).scalars().all():
        session.delete(a)
    for d in session.execute(select(AgentDecision).where(AgentDecision.agent == "cutover_risk", AgentDecision.subject_id == manifest_id)).scalars().all():
        session.delete(d)  # other tests assess the risk of the shared manifest
    session.flush()


def _item(r, item_id):
    return next(i for i in r.items if i["id"] == item_id)


def test_checklist_template_covers_runbook_and_is_typed():
    assert any(a["name"] == "cutover_risk" for a in agent_catalog())
    tpl = reh.checklist_template()
    ids = [t["id"] for t in tpl]
    assert len(ids) == len(set(ids)) == len(reh.AUTO_ITEMS) + len(reh.MANUAL_ITEMS)
    tasks = {t[0] for t in reh.TEMPLATE}
    assert all(t["runbook_task"] in tasks for t in tpl)
    assert {t["kind"] for t in tpl} == {"AUTO", "MANUAL"} and all(t["phase"] in ("PREPARE", "INITIAL_LOAD", "SYNC", "DOWNTIME", "POST") for t in tpl)
    assert any(t["blocking"] for t in tpl if t["kind"] == "MANUAL") and any(not t["blocking"] for t in tpl)


def test_rehearsal_lifecycle_checks_timings_and_verdict(session, slice_result, tmp_path):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    run = reh._latest_completed_run(session, m)  # other tests re-run the manifest; the checklist looks at the latest completed run
    assert run is not None and run.manifest_id == m.id and run.status == "COMPLETED"
    _wipe(session, m.id, run.id)
    r = reh.create_rehearsal(session, m, "Mock cutover 1", "mock", "architect")
    assert r.sequence == 1 and r.kind == "MOCK" and r.status == "PLANNED" and len(r.items) == len(reh.checklist_template()) and [t["id"] for t in r.runbook] == [t[0] for t in reh.TEMPLATE]
    st = {i["id"]: i["status"] for i in r.items}
    # automatic items from the slice: approved manifest and ruleset, completed run, reconciliation PASS, evidence, target
    assert st["A01"] == st["A02"] == st["A03"] == st["A04"] == st["A05"] == st["A10"] == st["A12"] == st["A13"] == "PASS"
    assert _item(r, "A04")["evidence"]["run_id"] == run.id and _item(r, "A04")["evidence"]["load_mode"] == run.metrics.get("load_mode")
    assert st["A07"] == "FAIL" and "missing sign-off" in _item(r, "A07")["detail"]  # nobody signed the reconciliation off
    src = session.get(SapSystem, m.definition["source_system_id"])
    assert st["A08"] == ("NOT_APPLICABLE" if src.connector != "RFC" else "FAIL")  # no change capture on the synthetic source
    assert st["A09"] == "FAIL" and "no package exported" in _item(r, "A09")["detail"]  # cockpit rows loaded, nothing exported
    assert st["A11"] == "FAIL" and "not assessed" in _item(r, "A11")["detail"]
    assert all(i["status"] == "PENDING" for i in r.items if i["kind"] == "MANUAL") and all(i["checked_by"] == "platform" for i in r.items if i["kind"] == "AUTO")
    assert r.summary["ready_for_go"] is False and {"A07", "A09", "M01"} <= set(r.summary["blocking_open"])
    # GO is refused while blocking items are open; NO_GO is always possible (not taken here)
    with pytest.raises(ValueError, match="GO refused"):
        reh.complete_rehearsal(session, r, "GO", "approver")
    assert r.status == "PLANNED"
    # automatic items are not ticked by hand, only waived with a note; manual FAIL needs a note
    with pytest.raises(ValueError, match="evaluated by the platform"):
        reh.mark_item(session, r, "A07", "PASS", "operator")
    with pytest.raises(ValueError, match="needs a note"):
        reh.mark_item(session, r, "A11", "NOT_APPLICABLE", "operator")
    with pytest.raises(ValueError, match="needs a note"):
        reh.mark_item(session, r, "M03", "FAIL", "operator")
    with pytest.raises(ValueError, match="not found"):
        reh.mark_item(session, r, "M99", "PASS", "operator")
    waived = reh.mark_item(session, r, "A11", "NOT_APPLICABLE", "architect", "risk reviewed in the steering meeting")
    assert waived["status"] == "NOT_APPLICABLE" and waived["waived"] is True and waived["checked_by"] == "architect"
    # the platform state moves: sign-offs, cockpit rounds converged, risk assessed -> refresh picks it up
    session.add(ApprovalRecord(subject_type="RECONCILIATION", subject_id=run.id, decision="APPROVED", decided_by="approver", kind="TECHNICAL"))
    session.add(ApprovalRecord(subject_type="RECONCILIATION", subject_id=run.id, decision="APPROVED", decided_by="approver", kind="BUSINESS"))
    export_cockpit_files(session, run.id, out_dir=str(tmp_path), use_templates=False)
    res = reh.refresh_auto_items(session, r, "operator")
    assert {c["id"]: c["to"] for c in res["changed"]} == {"A07": "PASS"} and _item(r, "A09")["status"] == "FAIL" and "no round simulated yet" in _item(r, "A09")["detail"]
    assert _item(r, "A11")["status"] == "NOT_APPLICABLE"  # waived items stay waived across refreshes
    import_feedback(session, run.id, sample_feedback(session, run.id, errors_per_object=0).replace(";E;", ";S;"), "all-accepted.csv", "operator")
    REGISTRY["cutover_risk"]().run(session, slice_result["project_id"], {"manifest_id": m.id}, "architect")
    reh.mark_item(session, r, "A11", "PENDING", "architect", "un-waived")  # back to the platform's verdict
    a11 = _item(r, "A11")
    assert a11["waived"] is False and a11["status"] in ("PASS", "FAIL") and "risk" in a11["detail"] and a11["evidence"]["band"]
    assert _item(r, "A09")["status"] == "PASS" and _item(r, "A09")["evidence"]["converged"] is True
    # manual items ticked by hand; one fails with a note, then passes
    for i in r.items:
        if i["kind"] == "MANUAL" and i["blocking"]:
            reh.mark_item(session, r, i["id"], "PASS", "basis", f"{i['id']} done")
    failed = reh.mark_item(session, r, "M09", "FAIL", "business", "validation scripts missing for FI")
    assert failed["status"] == "FAIL" and failed["note"].startswith("validation scripts")
    assert r.summary["blocking_open"] == [] and r.summary["ready_for_go"] is True and r.summary["fail"] >= 1
    # timings need a started rehearsal
    with pytest.raises(ValueError, match="IN_PROGRESS"):
        reh.time_task(session, r, "T06", "start", "operator")
    reh.start_rehearsal(session, r, "operator", "window opened")
    assert r.status == "IN_PROGRESS" and r.started_by == "operator"
    with pytest.raises(ValueError, match="not in the runbook"):
        reh.time_task(session, r, "T99", "start", "operator")
    with pytest.raises(ValueError, match="was not started"):
        reh.time_task(session, r, "T06", "finish", "operator")
    reh.time_task(session, r, "T06", "start", "operator")
    with pytest.raises(ValueError, match="already started"):
        reh.time_task(session, r, "T06", "start", "operator")
    t = reh.time_task(session, r, "T06", "finish", "operator", "interfaces stopped in 3 minutes on the rehearsal landscape")
    assert t["actual_minutes"] is not None and 0 <= t["actual_minutes"] < 1 and r.summary["tasks_timed"] == 1 and r.summary["downtime_actual_minutes"] == t["actual_minutes"]
    reh.time_task(session, r, "T07", "start", "operator")
    reh.time_task(session, r, "T07", "finish", "operator")
    lesson = reh.add_lesson(session, r, "Stop the IDoc inbound queue before the RFC destinations", "integration", "T06")
    assert lesson["task"] == "T06" and r.lessons[-1]["text"].startswith("Stop the IDoc")
    with pytest.raises(ValueError, match="empty"):
        reh.add_lesson(session, r, "   ", "x")
    # verdict: GO by the approver, recorded as an approval, with the failed non-blocking item listed
    reh.complete_rehearsal(session, r, "GO", "approver", "go for the dress rehearsal")
    assert r.status == "COMPLETED" and r.verdict == "GO" and r.completed_by == "approver"
    appr = session.execute(select(ApprovalRecord).where(ApprovalRecord.subject_type == "REHEARSAL", ApprovalRecord.subject_id == r.id)).scalars().one()
    assert appr.decision == "APPROVED" and appr.kind == "TECHNICAL"
    with pytest.raises(ValueError, match="frozen"):
        reh.mark_item(session, r, "M09", "PASS", "business", "late")
    with pytest.raises(ValueError, match="frozen"):
        reh.refresh_auto_items(session, r, "operator")
    # the measured minutes replace the template estimate in the next runbook
    rb = generate_runbook(session, m)
    t06 = next(t for t in rb["tasks"] if t["id"] == "T06")
    assert t06["basis"] == "rehearsal 1" and t06["est_minutes"] == 1.0 and rb["rehearsal_timed_tasks"] == ["T06", "T07"]
    assert next(t for t in rb["tasks"] if t["id"] == "T01")["basis"] == "template"
    # report
    md = reh.rehearsal_markdown(r)
    assert "# Cutover rehearsal 1: Mock cutover 1 (MOCK)" in md and "verdict **GO**" in md and "| M09 |" in md and "validation scripts missing" in md and "## Task timings" in md and "## Lessons" in md
    # a second rehearsal: NO_GO is allowed with blocking items open; a third one is aborted
    r2 = reh.create_rehearsal(session, m, "", "DRESS", "architect")
    assert r2.sequence == 2 and r2.name == "Dress rehearsal 2" and _item(r2, "A07")["status"] == "PASS" and r2.summary["blocking_open"]
    reh.complete_rehearsal(session, r2, "NO_GO", "approver", "interfaces not ready")
    assert r2.verdict == "NO_GO" and session.execute(select(ApprovalRecord).where(ApprovalRecord.subject_id == r2.id)).scalars().one().decision == "REJECTED"
    r3 = reh.create_rehearsal(session, m, "Final", "FINAL", "architect")
    reh.abort_rehearsal(session, r3, "operator", "postponed")
    assert r3.status == "ABORTED"
    with pytest.raises(ValueError):
        reh.abort_rehearsal(session, r3, "operator")
    with pytest.raises(ValueError, match="kind must be"):
        reh.create_rehearsal(session, m, "x", "SMOKE", "architect")
    # the risk agent reads the rehearsals
    d = REGISTRY["cutover_risk"]().run(session, slice_result["project_id"], {"manifest_id": m.id}, "architect")
    f = d.proposal["factors"]
    assert f["rehearsals_completed"] == 2 and f["rehearsals_go"] == 1 and f["interface_plan"] == "PENDING" and f["latest_rehearsal"] == "3 ABORTED"
    crit = {c["criterion"]: c["met"] for c in d.proposal["go_no_go_criteria"]}
    assert crit["At least one completed cutover rehearsal with verdict GO"] is True
    assert any(e["kind"] == "REHEARSAL" for e in d.evidence)
    # audit trail
    actions = [e.action for e in session.execute(select(AuditEvent).where(AuditEvent.subject_id == m.id)).scalars().all()]
    assert {"CUTOVER_REHEARSAL_CREATED", "CUTOVER_ITEM_MARKED", "CUTOVER_ITEM_UNWAIVED", "CUTOVER_REHEARSAL_REFRESHED", "CUTOVER_REHEARSAL_STARTED", "CUTOVER_TASK_STARTED", "CUTOVER_TASK_FINISHED", "CUTOVER_LESSON_ADDED", "CUTOVER_REHEARSAL_COMPLETED", "CUTOVER_REHEARSAL_ABORTED"} <= set(actions)
    _wipe(session, m.id, run.id)


def test_rehearsal_api_and_cli(client, tokens, session, slice_result, capsys, tmp_path):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    run = reh._latest_completed_run(session, m)
    _wipe(session, m.id, run.id)
    session.commit()
    tpl = client.get(f"{API}/cutover/checklist-template", headers=tokens["viewer"]).json()
    assert len(tpl) == len(reh.checklist_template()) and tpl[0]["id"] == "A01"
    assert client.post(f"{API}/manifests/{m.id}/cutover/rehearsals", json={"name": "Mock 1"}, headers=tokens["viewer"]).status_code == 403
    assert client.post(f"{API}/manifests/{m.id}/cutover/rehearsals", json={"kind": "SMOKE"}, headers=tokens["operator"]).status_code == 422
    r = client.post(f"{API}/manifests/{m.id}/cutover/rehearsals", json={"name": "Mock 1", "kind": "MOCK"}, headers=tokens["operator"])
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["sequence"] == 1 and r.json()["status"] == "PLANNED" and {i["id"] for i in r.json()["items"]} == {t["id"] for t in tpl} and r.json()["summary"]["ready_for_go"] is False
    lst = client.get(f"{API}/manifests/{m.id}/cutover/rehearsals", headers=tokens["viewer"]).json()
    assert [x["id"] for x in lst] == [rid] and "items" not in lst[0]
    assert client.get(f"{API}/cutover/rehearsals/nope", headers=tokens["viewer"]).status_code == 404
    one = client.get(f"{API}/cutover/rehearsals/{rid}", headers=tokens["viewer"]).json()
    assert one["runbook"][0]["id"] == "T01" and one["items"][0]["status"] == "PASS"
    # marking: role, validation, business rule
    assert client.post(f"{API}/cutover/rehearsals/{rid}/items/M01", json={"status": "PASS"}, headers=tokens["viewer"]).status_code == 403
    assert client.post(f"{API}/cutover/rehearsals/{rid}/items/M01", json={"status": "DONE"}, headers=tokens["operator"]).status_code == 422
    assert client.post(f"{API}/cutover/rehearsals/{rid}/items/A01", json={"status": "PASS"}, headers=tokens["operator"]).status_code == 409
    r = client.post(f"{API}/cutover/rehearsals/{rid}/items/M01", json={"status": "PASS", "note": "transport lock set"}, headers=tokens["operator"])
    assert r.status_code == 200 and r.json()["item"]["status"] == "PASS" and r.json()["item"]["checked_by"] == "operator" and "M01" not in r.json()["summary"]["blocking_open"]
    # verdict: approver only; GO refused while blocking items are open; NO_GO accepted
    assert client.post(f"{API}/cutover/rehearsals/{rid}/complete", json={"verdict": "GO"}, headers=tokens["operator"]).status_code == 403
    r = client.post(f"{API}/cutover/rehearsals/{rid}/complete", json={"verdict": "GO"}, headers=tokens["approver"])
    assert r.status_code == 409 and "GO refused" in r.json()["detail"]
    # timings and lessons
    assert client.post(f"{API}/cutover/rehearsals/{rid}/tasks/T06", json={"action": "start"}, headers=tokens["operator"]).status_code == 409  # not started
    assert client.post(f"{API}/cutover/rehearsals/{rid}/start", json={"note": "window"}, headers=tokens["operator"]).json()["status"] == "IN_PROGRESS"
    assert client.post(f"{API}/cutover/rehearsals/{rid}/tasks/T06", json={"action": "start"}, headers=tokens["operator"]).status_code == 200
    r = client.post(f"{API}/cutover/rehearsals/{rid}/tasks/T06", json={"action": "finish", "note": "ok"}, headers=tokens["operator"])
    assert r.status_code == 200 and r.json()["actual_minutes"] is not None and r.json()["summary"]["tasks_timed"] == 1
    assert client.post(f"{API}/cutover/rehearsals/{rid}/lessons", json={"text": "lock users earlier", "task": "T06"}, headers=tokens["operator"]).status_code == 201
    r = client.post(f"{API}/cutover/rehearsals/{rid}/refresh", headers=tokens["operator"])
    assert r.status_code == 200 and "changed" in r.json()
    r = client.post(f"{API}/cutover/rehearsals/{rid}/complete", json={"verdict": "NO_GO", "note": "interfaces open"}, headers=tokens["approver"])
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED" and r.json()["verdict"] == "NO_GO"
    assert client.post(f"{API}/cutover/rehearsals/{rid}/items/M02", json={"status": "PASS"}, headers=tokens["operator"]).status_code == 409  # frozen
    rep = client.get(f"{API}/cutover/rehearsals/{rid}/report", headers=tokens["auditor"]).json()
    assert rep["markdown"].startswith("# Cutover rehearsal 1: Mock 1 (MOCK)") and "verdict **NO_GO**" in rep["markdown"] and "lock users earlier" in rep["markdown"]
    r2 = client.post(f"{API}/manifests/{m.id}/cutover/rehearsals", headers=tokens["architect"]).json()
    assert r2["sequence"] == 2 and r2["kind"] == "MOCK"
    assert client.post(f"{API}/cutover/rehearsals/{r2['id']}/abort", json={"note": "later"}, headers=tokens["operator"]).json()["status"] == "ABORTED"
    assert client.post(f"{API}/cutover/rehearsals/{r2['id']}/abort", headers=tokens["operator"]).status_code == 409
    rb = client.get(f"{API}/manifests/{m.id}/cutover/runbook", headers=tokens["viewer"]).json()
    assert rb["rehearsal_timed_tasks"] == ["T06"] and next(t for t in rb["tasks"] if t["id"] == "T06")["basis"] == "rehearsal 1"  # a completed rehearsal's measured minutes count whatever its verdict; the aborted one has none
    assert any("rehearsal checklist" in c["note"] for c in client.get(f"{API}/platform/capabilities", headers=tokens["viewer"]).json())
    # CLI
    session.expire_all()
    capsys.readouterr()
    assert cli_main(["cutover-rehearsal", "list", "--manifest", m.id]) == 0
    out = capsys.readouterr().out
    assert "rehearsal 1 [MOCK]" in out and "COMPLETED NO_GO" in out and "rehearsal 2 [MOCK]" in out and "ABORTED" in out
    assert cli_main(["cutover-rehearsal", "create", "--manifest", m.id, "--name", "Dress 1", "--kind", "DRESS"]) == 0
    out = capsys.readouterr().out
    assert "rehearsal 3 [DRESS]" in out and "Dress 1" in out
    rid3 = out.split("[DRESS] ")[1].split(":")[0]
    assert cli_main(["cutover-rehearsal", "mark", "--id", rid3, "--item", "M04", "--status", "PASS", "--note", "backups taken"]) == 0
    assert "M04" in capsys.readouterr().out
    assert cli_main(["cutover-rehearsal", "mark", "--id", rid3, "--item", "M04", "--status", "FAIL"]) == 2
    assert "needs a note" in capsys.readouterr().err
    assert cli_main(["cutover-rehearsal", "start", "--id", rid3]) == 0 and cli_main(["cutover-rehearsal", "task", "--id", rid3, "--task", "T01", "--action", "start"]) == 0 and cli_main(["cutover-rehearsal", "task", "--id", rid3, "--task", "T01", "--action", "finish"]) == 0
    assert "T01 finished" in capsys.readouterr().out
    assert cli_main(["cutover-rehearsal", "lesson", "--id", rid3, "--text", "label the transports"]) == 0
    assert cli_main(["cutover-rehearsal", "refresh", "--id", rid3]) == 0 and "refreshed" in capsys.readouterr().out
    assert cli_main(["cutover-rehearsal", "show", "--id", rid3]) == 0
    out = capsys.readouterr().out
    assert "M04 PASS" in out and "task T01" in out and "lesson: label the transports" in out and "A01 PASS" in out
    assert cli_main(["cutover-rehearsal", "complete", "--id", rid3, "--verdict", "GO"]) == 2 and "GO refused" in capsys.readouterr().err
    assert cli_main(["cutover-rehearsal", "complete", "--id", rid3, "--verdict", "NO_GO", "--note", "mock only"]) == 0
    assert "COMPLETED NO_GO" in capsys.readouterr().out
    out_md = tmp_path / "rehearsal.md"
    assert cli_main(["cutover-rehearsal", "report", "--id", rid3, "--out", str(out_md)]) == 0 and "Dress 1 (DRESS)" in out_md.read_text()
    assert cli_main(["cutover-rehearsal", "show", "--id", "nope"]) == 2 and cli_main(["cutover-rehearsal", "list", "--manifest", "nope"]) == 2
    session.expire_all()
    _wipe(session, m.id, run.id)
    session.commit()
