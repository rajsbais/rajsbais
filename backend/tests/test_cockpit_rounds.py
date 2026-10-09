"""Re-upload tracking of cockpit packages: rounds, manual upload/migration marks, per-instance outcomes from the
feedback, released instances, burn-down and convergence across retry rounds."""
import json

import pytest
from sqlalchemy import select

from sdtf.cli import main as cli_main
from sdtf.models import AuditEvent, CockpitAttempt, CockpitFeedback, MigrationRun
from sdtf.runtime.cockpit_attempts import attempts, burndown, current_attempt, mark_attempt, still_rejected
from sdtf.runtime.cockpit_export import export_cockpit_files
from sdtf.runtime.cockpit_feedback import clear_feedback, import_feedback, sample_feedback
from sdtf.staging import get_backend

API = "/api/v1"


def _wipe(session, run_id):
    for a in session.execute(select(CockpitAttempt).where(CockpitAttempt.run_id == run_id)).scalars().all():
        session.delete(a)
    for f in session.execute(select(CockpitFeedback).where(CockpitFeedback.run_id == run_id)).scalars().all():
        session.delete(f)
    session.flush()


def test_rounds_converge_across_retry_packages(session, slice_result, tmp_path):
    run = session.get(MigrationRun, slice_result["run_id"])
    _wipe(session, run.id)
    backend = get_backend(session=session)
    # round 1: the full package
    out1 = export_cockpit_files(session, run.id, out_dir=str(tmp_path), use_templates=False)
    r1 = current_attempt(session, run.id)
    assert out1["round"] == 1 and r1.sequence == 1 and r1.scope == "all" and r1.status == "EXPORTED" and r1.instances == out1["instances"] and len(r1.instance_keys) == r1.instances and r1.manifest_sha256 == out1["manifest_sha256"]
    # upload recorded by hand, then the simulation log; a round that was never uploaded cannot be migrated
    with pytest.raises(ValueError, match="cannot mark it MIGRATED"):
        mark_attempt(session, r1, "MIGRATED", "operator")
    with pytest.raises(ValueError, match="status must be"):
        mark_attempt(session, r1, "DONE", "operator")
    mark_attempt(session, r1, "UPLOADED", "operator", "project P1, transfer T-0001")
    assert r1.status == "UPLOADED" and r1.upload_note.startswith("project P1") and r1.uploaded_by == "operator"
    s1 = import_feedback(session, run.id, sample_feedback(session, run.id), "round1.csv", "operator")
    assert s1["round"] == 1 and s1["round_scope"] == "all" and r1.status == "SIMULATED" and r1.result["rejected"] == s1["rejected_instances"] and r1.result["accepted"] == s1["instances_in_log"] - s1["rejected_instances"] and r1.result["resolved"] == 0 and r1.result["not_in_log"] == 0
    assert s1["still_rejected"] == s1["rejected_instances"] > 1 and s1["rounds"] == 1 and s1["converged"] is False
    n_rejected = s1["rejected_instances"]
    assert {tuple(k.split("|", 1)) for k, v in r1.outcomes.items() if v == "rejected"} == still_rejected(session, run.id)
    # round 2: the retry package carries exactly the rejected instances and supersedes nothing (round 1 is simulated)
    out2 = export_cockpit_files(session, run.id, out_dir=str(tmp_path), use_templates=False, scope="rejected")
    r2 = current_attempt(session, run.id)
    assert out2["round"] == 2 and r2.sequence == 2 and r2.scope == "rejected" and r2.instances == n_rejected and set(map(tuple, r2.instance_keys)) == still_rejected(session, run.id) and r1.status == "SIMULATED"
    # a second export before simulating supersedes the open round
    out3 = export_cockpit_files(session, run.id, out_dir=str(tmp_path), use_templates=False, scope="rejected")
    r3 = current_attempt(session, run.id)
    assert out3["round"] == 3 and r3.sequence == 3 and session.get(CockpitAttempt, r2.id).status == "SUPERSEDED"
    # the retry round's sample log accepts all but one instance: outcomes, releases and burn-down
    log = sample_feedback(session, run.id)
    assert log.count("\r\n") - 2 == n_rejected  # header + trailer + one line per retried instance
    s3 = import_feedback(session, run.id, log, "round3.csv", "operator")
    assert s3["round"] == 3 and r3.status == "SIMULATED" and r3.result["rejected"] == 1 and r3.result["accepted"] == n_rejected - 1 and r3.result["resolved"] == n_rejected - 1 and r3.result["regressed"] == 0
    assert s3["still_rejected"] == 1 and s3["resolved_total"] == n_rejected - 1 and s3["rounds"] == 3 and s3["converged"] is False
    assert len(still_rejected(session, run.id)) == 1
    # released instances went back to LOADED with lineage; the one still rejected stays COCKPIT_ERROR
    (ot, key) = next(iter(still_rejected(session, run.id)))
    errored = [r for r in backend.iter_records(run.id) if r.load_status == "COCKPIT_ERROR"]
    assert errored and all(r.lineage[-1]["to"] == "COCKPIT_ERROR" for r in errored)
    released = [r for r in backend.iter_records(run.id) if r.load_status == "LOADED" and r.lineage and r.lineage[-1].get("rule") == "cockpit" and r.lineage[-1]["to"] == "LOADED" and r.lineage[-1].get("round") == 3]
    assert released  # rows of the instances this round released (other tests leave older cockpit lineage behind)
    bd = burndown(session, run.id)
    assert [r["sequence"] for r in bd["rounds"]] == [1, 2, 3] and [r["status"] for r in bd["rounds"]] == ["SIMULATED", "SUPERSEDED", "SIMULATED"]
    assert bd["line"] == [{"round": 1, "scope": "all", "retried": r1.instances, "rejected": n_rejected, "accepted": r1.result["accepted"], "not_in_log": 0, "resolved": 0, "regressed": 0}, {"round": 3, "scope": "rejected", "retried": n_rejected, "rejected": 1, "accepted": n_rejected - 1, "not_in_log": 0, "resolved": n_rejected - 1, "regressed": 0}]
    assert bd["remaining"] == 1 and bd["resolved_total"] == n_rejected - 1 and bd["ever_rejected"] == n_rejected and bd["converged"] is False and bd["current"]["sequence"] == 3
    sr = bd["still_rejected"][0]
    assert (sr["object_type"], sr["key"]) == (ot, key) and [h["outcome"] for h in sr["history"]] == ["rejected", "rejected"] and [h["round"] for h in sr["history"]] == [1, 3] and sr["messages"] and sr["messages"][-1]["round"] == 3
    # feedback rows carry their round; per-round exceptions; replace affects the round only
    assert {f.attempt_sequence for f in session.execute(select(CockpitFeedback).where(CockpitFeedback.run_id == run.id)).scalars().all()} == {1, 3}
    s3b = import_feedback(session, run.id, log, "round3b.csv", "operator", attempt_sequence=3)
    assert s3b["messages"] == s3["messages"] and {f.attempt_sequence for f in session.execute(select(CockpitFeedback).where(CockpitFeedback.run_id == run.id)).scalars().all()} == {1, 3}
    with pytest.raises(ValueError, match="round 9 does not exist"):
        import_feedback(session, run.id, log, "x.csv", "operator", attempt_sequence=9)
    # round 4 converges: a log that accepts the last instance
    export_cockpit_files(session, run.id, out_dir=str(tmp_path), use_templates=False, scope="rejected")
    r4 = current_attempt(session, run.id)
    assert r4.sequence == 4 and r4.instances == 1
    s4 = import_feedback(session, run.id, sample_feedback(session, run.id, errors_per_object=0).replace(";E;", ";S;"), "round4.csv", "operator")
    assert s4["still_rejected"] == 0 and s4["converged"] is True and r4.result["resolved"] == 1 and burndown(session, run.id)["converged"] is True
    assert not [r for r in backend.iter_records(run.id) if r.load_status == "COCKPIT_ERROR"]
    mark_attempt(session, r4, "MIGRATED", "operator", "migrated in the app")
    assert r4.status == "MIGRATED" and r4.migrated_by == "operator"
    actions = [e.action for e in session.execute(select(AuditEvent).where(AuditEvent.subject_id == run.id)).scalars().all()]
    assert {"COCKPIT_ROUND_EXPORTED", "COCKPIT_ROUND_UPLOADED", "COCKPIT_ROUND_SIMULATED", "COCKPIT_ROUND_MIGRATED"} <= set(actions)
    # clear forgets the results but keeps the rounds and their upload marks
    clear_feedback(session, run.id, "operator")
    rows = attempts(session, run.id)
    assert [a.status for a in rows] == ["UPLOADED", "SUPERSEDED", "EXPORTED", "EXPORTED"] and all(not a.outcomes for a in rows) and rows[0].upload_note.startswith("project P1")
    assert still_rejected(session, run.id) == set() and burndown(session, run.id)["remaining"] == 0
    _wipe(session, run.id)


def test_rounds_api_and_cli(client, tokens, session, slice_result, tmp_path, capsys):
    run = session.get(MigrationRun, slice_result["run_id"])
    _wipe(session, run.id)
    session.commit()
    r = client.get(f"{API}/runs/{run.id}/cockpit-rounds", headers=tokens["viewer"])
    assert r.status_code == 200 and r.json()["rounds"] == [] and r.json()["current"] is None and r.json()["converged"] is False
    r = client.post(f"{API}/runs/{run.id}/cockpit-export", json={"use_templates": False}, headers=tokens["operator"])
    assert r.status_code == 201 and r.json()["round"] == 1
    assert client.post(f"{API}/runs/{run.id}/cockpit-rounds/1/mark", json={"status": "UPLOADED", "note": "T-1"}, headers=tokens["viewer"]).status_code == 403
    assert client.post(f"{API}/runs/{run.id}/cockpit-rounds/7/mark", json={"status": "UPLOADED"}, headers=tokens["operator"]).status_code == 404
    assert client.post(f"{API}/runs/{run.id}/cockpit-rounds/1/mark", json={"status": "MIGRATED"}, headers=tokens["operator"]).status_code == 409
    r = client.post(f"{API}/runs/{run.id}/cockpit-rounds/1/mark", json={"status": "UPLOADED", "note": "T-1"}, headers=tokens["operator"])
    assert r.status_code == 200 and r.json()["status"] == "UPLOADED" and r.json()["upload_note"] == "T-1"
    sample = client.get(f"{API}/runs/{run.id}/cockpit-feedback/sample", headers=tokens["viewer"]).text
    r = client.post(f"{API}/runs/{run.id}/cockpit-feedback/import", json={"content": sample, "filename": "r1.csv"}, headers=tokens["operator"])
    assert r.status_code == 201 and r.json()["round"] == 1 and r.json()["still_rejected"] > 0
    n = r.json()["still_rejected"]
    r = client.post(f"{API}/runs/{run.id}/cockpit-export", json={"scope": "rejected", "use_templates": False}, headers=tokens["operator"])
    assert r.status_code == 201 and r.json()["round"] == 2 and r.json()["instances"] == n
    sample2 = client.get(f"{API}/runs/{run.id}/cockpit-feedback/sample", headers=tokens["viewer"]).text
    r = client.post(f"{API}/runs/{run.id}/cockpit-feedback/import", json={"content": sample2, "filename": "r2.csv", "round": 2}, headers=tokens["operator"])
    assert r.status_code == 201 and r.json()["round"] == 2 and r.json()["still_rejected"] == 1 and r.json()["resolved_total"] == n - 1
    assert client.post(f"{API}/runs/{run.id}/cockpit-feedback/import", json={"content": sample2, "round": 9}, headers=tokens["operator"]).status_code == 422
    r = client.get(f"{API}/runs/{run.id}/cockpit-rounds", headers=tokens["viewer"])
    bd = r.json()
    assert [x["status"] for x in bd["rounds"]] == ["SIMULATED", "SIMULATED"] and bd["remaining"] == 1 and bd["line"][1]["resolved"] == n - 1 and len(bd["still_rejected"]) == 1 and bd["still_rejected"][0]["history"][-1]["round"] == 2
    r = client.get(f"{API}/runs/{run.id}/cockpit-feedback?round=1&severity=E", headers=tokens["viewer"])
    assert r.status_code == 200 and len(r.json()["messages"]) == n and all(m["round"] == 1 for m in r.json()["messages"])
    r = client.post(f"{API}/runs/{run.id}/cockpit-rounds/2/mark", json={"status": "MIGRATED", "note": "done"}, headers=tokens["operator"])
    assert r.status_code == 200 and r.json()["status"] == "MIGRATED"
    assert any("re-upload tracking" in c["note"] for c in client.get(f"{API}/platform/capabilities", headers=tokens["viewer"]).json())
    # CLI
    session.expire_all()
    capsys.readouterr()
    assert cli_main(["cockpit-feedback", "rounds", "--run", run.id]) == 0
    out = capsys.readouterr().out
    assert "2 round(s), 1 instance(s) still rejected" in out and "round 1 [all] SIMULATED" in out and "uploaded by operator (T-1)" in out and "still rejected:" in out
    assert cli_main(["cockpit-feedback", "mark", "--run", run.id, "--round", "9", "--status", "UPLOADED"]) == 2
    assert cli_main(["cockpit-export", "--run", run.id, "--out", str(tmp_path), "--scope", "rejected"]) == 0
    capsys.readouterr()
    assert cli_main(["cockpit-feedback", "mark", "--run", run.id, "--round", "3", "--status", "UPLOADED", "--note", "T-3"]) == 0
    assert "round 3 is now UPLOADED" in capsys.readouterr().out
    f = tmp_path / "r3.csv"
    assert cli_main(["cockpit-feedback", "sample", "--run", run.id, "--out", str(f)]) == 0
    capsys.readouterr()
    assert cli_main(["cockpit-feedback", "import", "--run", run.id, "--file", str(f), "--round", "3"]) == 0
    assert "round 3:" in capsys.readouterr().out
    assert cli_main(["cockpit-feedback", "rounds", "--run", run.id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rounds"][2]["status"] == "SIMULATED"
    session.expire_all()
    clear_feedback(session, run.id, "cli")
    _wipe(session, run.id)
    session.commit()
