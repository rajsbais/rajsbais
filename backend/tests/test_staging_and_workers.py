"""Columnar staging parity and distributed extraction workers."""
import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import select

from sdtf import config
from sdtf.demo import run_vertical_slice
from sdtf.models import MigrationRun
from sdtf.runtime.pipeline import start_run
from sdtf.runtime.worker import Worker, claim_job, finalize_if_complete, job_summary, requeue_stale
from sdtf.staging import StagedRow
from sdtf.staging.columnar import ColumnarStaging


@pytest.fixture()
def columnar(tmp_path):
    return ColumnarStaging(str(tmp_path / "staging"))


def test_columnar_backend_contract(columnar):
    rows = [StagedRow("P1", "BKPF", f"5000|{i}|2024", {"BUKRS": "5000", "BELNR": str(i), "GJAHR": 2024}) for i in range(5)]
    assert columnar.write_partition("r1", "P1", rows) == 5
    # duplicates across partitions are ignored (first wins)
    assert columnar.write_partition("r1", "P2", rows[:2] + [StagedRow("P2", "BKPF", "5000|99|2024", {"BELNR": "99"})]) == 1
    assert columnar.keys("r1", "BKPF") == {r.record_key for r in rows} | {"5000|99|2024"}
    got = list(columnar.iter_records("r1", "BKPF"))
    assert len(got) == 6 and all(g.load_status == "STAGED" for g in got)
    for g in got[:3]:
        g.target_payload, g.target_key, g.load_status, g.lineage = {**g.source_payload, "BUKRS": "SP01"}, "SP01|x", "TRANSFORMED", [{"rule": "cc", "field": "BUKRS", "from": "5000", "to": "SP01"}]
    assert columnar.update_records("r1", got[:3]) == 3
    assert sorted(c["status"] for c in columnar.counts("r1")) == ["STAGED", "TRANSFORMED"]
    tr = list(columnar.iter_records("r1", status="TRANSFORMED"))
    assert len(tr) == 3 and tr[0].target_payload["BUKRS"] == "SP01" and tr[0].lineage[0]["rule"] == "cc"
    assert columnar.footprint("r1")["files"] == 2
    columnar.drop_run("r1")
    assert columnar.counts("r1") == []


def test_vertical_slice_on_columnar_staging(engine, tmp_path, monkeypatch):
    from sdtf.db import session_scope

    monkeypatch.setattr(config, "settings", config.Settings(database_url=config.settings.database_url, evidence_dir=config.settings.evidence_dir, staging_backend="columnar", staging_dir=str(tmp_path / "stg")))
    with session_scope() as s:
        out = run_vertical_slice(s, scale=1, seed=31)
        run = out["run"]
        assert run.status == "COMPLETED" and run.metrics["staging_backend"] == "columnar"
        assert run.report["reconciliation"]["overall"] == "PASS", run.report["reconciliation"]["failures"][:5]
        ex = next(st for st in run.stages if st.name == "EXTRACT").metrics
        assert ex["staging_backend"] == "columnar" and ex["records"] > 1000
        fp = ColumnarStaging(str(tmp_path / "stg")).footprint(run.id)
        assert fp["files"] > 20 and fp["bytes"] > 0
        # no rows went to the relational staging table for this run
        from sdtf.models import StagedRecord

        assert s.execute(select(StagedRecord).where(StagedRecord.run_id == run.id)).first() is None


def _distributed_run(session, slice_result, staging="relational"):
    return start_run(session, slice_result["project_id"], slice_result["manifest_id"], slice_result["ruleset_id"], "operator", execution="DISTRIBUTED", staging_backend=staging)


def test_distributed_run_is_processed_by_in_process_workers(session, slice_result):
    run = _distributed_run(session, slice_result)
    session.commit()
    assert run.status == "RUNNING" and next(st for st in run.stages if st.name == "EXTRACT").status == "RUNNING"
    summary = job_summary(session, run.id)
    assert summary["total"] > 10 and summary["by_status"] == {"QUEUED": summary["total"]}
    from sdtf.db import get_session_factory

    w1, w2 = Worker(get_session_factory(), "w1"), Worker(get_session_factory(), "w2")
    # alternate two workers until idle: every job is claimed exactly once, the last one finalises the run
    while True:
        a, b = w1.run_once(), w2.run_once()
        if not a and not b:
            break
    session.expire_all()
    summary = job_summary(session, run.id)
    assert summary["by_status"] == {"DONE": summary["total"]} and set(summary["workers"]) == {"w1", "w2"}
    assert all(j["attempts"] == 1 for j in summary["jobs"])
    run = session.get(MigrationRun, run.id)
    assert run.status == "COMPLETED", run.status
    assert run.report["reconciliation"]["overall"] == "PASS"
    ex = next(st for st in run.stages if st.name == "EXTRACT").metrics
    assert ex["execution"] == "DISTRIBUTED" and ex["partitions_total"] == summary["total"] and sorted(ex["workers"]) == ["w1", "w2"]


def test_expired_lease_is_requeued_and_only_one_worker_finalizes(session, slice_result):
    run = _distributed_run(session, slice_result)
    session.commit()
    job = claim_job(session, "crashed-worker", lease_seconds=1)
    assert job is not None and job.status == "CLAIMED" and job.attempts == 1
    assert finalize_if_complete(session, run.id, "test") is None, "cannot finalise with jobs outstanding"
    time.sleep(1.2)
    assert requeue_stale(session) >= 1
    session.commit()
    session.refresh(job)
    assert job.status == "QUEUED" and "lease expired" in job.error
    from sdtf.db import get_session_factory

    w = Worker(get_session_factory(), "w3")
    w.run(until_idle=True)
    session.expire_all()
    summary = job_summary(session, run.id)
    assert summary["by_status"] == {"DONE": summary["total"]}
    assert max(j["attempts"] for j in summary["jobs"]) == 2, "the re-queued job was attempted twice"
    assert session.get(MigrationRun, run.id).status == "COMPLETED"


def test_separate_worker_processes_share_the_queue(session, slice_result, db_url):
    """Two `sdtf worker` subprocesses against the same database and columnar staging dir."""
    run = _distributed_run(session, slice_result, staging="columnar")
    session.commit()
    env = {**os.environ, "SDTF_DATABASE_URL": db_url, "SDTF_STAGING_BACKEND": "columnar", "SDTF_STAGING_DIR": config.settings.staging_dir, "SDTF_EVIDENCE_DIR": config.settings.evidence_dir, "SDTF_WORKER_POLL_SECONDS": "0.2", "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}
    procs = [subprocess.Popen([sys.executable, "-m", "sdtf.cli", "worker", "--worker-id", f"proc-{i}", "--until-idle"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for i in range(2)]
    outs = [p.communicate(timeout=300)[0] for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    session.expire_all()
    summary = job_summary(session, run.id)
    assert summary["by_status"] == {"DONE": summary["total"]}, summary["by_status"]
    assert len(summary["workers"]) == 2, summary["workers"]
    run = session.get(MigrationRun, run.id)
    assert run.status == "COMPLETED" and run.report["reconciliation"]["overall"] == "PASS"
