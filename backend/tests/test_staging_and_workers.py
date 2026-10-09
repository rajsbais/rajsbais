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
from sdtf.runtime.worker import (
    JOB_STAGES,
    PARTITION_STAGES,
    Worker,
    advance_run_if_stage_complete,
    claim_job,
    job_summary,
    requeue_stale,
)
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
    partitions = summary["total"]
    assert partitions > 10 and summary["by_status"] == {"QUEUED": partitions} and set(summary["by_stage"]) == {"EXTRACT"}
    from sdtf.db import get_session_factory

    w1, w2 = Worker(get_session_factory(), "w1"), Worker(get_session_factory(), "w2")
    # alternate two workers until idle: every job is claimed exactly once, the last one finalises the run
    while True:
        a, b = w1.run_once(), w2.run_once()
        if not a and not b:
            break
    session.expire_all()
    summary = job_summary(session, run.id)
    # every partition stage ran as one job per partition, reconciliation as one job per table + functional + financial
    recon = summary["by_stage"]["RECONCILE"]["DONE"]
    assert summary["total"] == 3 * partitions + recon and summary["by_status"] == {"DONE": summary["total"]}
    assert {st: {"DONE": partitions} for st in PARTITION_STAGES} == {st: v for st, v in summary["by_stage"].items() if st != "RECONCILE"}
    recon_jobs = [j for j in summary["jobs"] if j["stage"] == "RECONCILE"]
    assert {j["partition"] for j in recon_jobs} >= {"functional", "financial", "technical:BKPF", "technical:BSEG"} and recon >= 20
    assert set(summary["workers"]) == {"w1", "w2"} and all(j["attempts"] == 1 for j in summary["jobs"])
    run = session.get(MigrationRun, run.id)
    assert run.status == "COMPLETED", run.status
    assert run.report["reconciliation"]["overall"] == "PASS", run.report["reconciliation"]["failures"][:5]
    assert run.report["exceptions"]["count"] == 0
    stages = {st.name: st.metrics for st in run.stages}
    for name in PARTITION_STAGES:
        assert stages[name]["execution"] == "DISTRIBUTED" and stages[name]["partitions_total"] == partitions and sorted(stages[name]["workers"]) == ["w1", "w2"], name
    assert stages["RECONCILE"]["overall"] == "PASS" and stages["RECONCILE"]["checks"] == run.report["reconciliation"]["checks"] and stages["RECONCILE"]["partitions_total"] == recon
    assert stages["TRANSFORM"]["records"] == stages["EXTRACT"]["records"] and stages["TRANSFORM"]["transformed"] > 0
    assert stages["LOAD"]["loaded"] + stages["LOAD"]["skipped_duplicate"] + stages["LOAD"]["matched_config"] == stages["TRANSFORM"]["records"] - stages["TRANSFORM"]["rejected"]
    assert stages["LOAD"]["conflicts"] == 0


def test_expired_lease_is_requeued_and_only_one_worker_finalizes(session, slice_result):
    run = _distributed_run(session, slice_result)
    session.commit()
    job = claim_job(session, "crashed-worker", lease_seconds=1)
    assert job is not None and job.status == "CLAIMED" and job.attempts == 1
    assert advance_run_if_stage_complete(session, run.id, "test") is None, "cannot advance with jobs outstanding"
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
    assert summary["by_status"] == {"DONE": summary["total"]} and set(summary["by_stage"]) == set(JOB_STAGES)
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
    assert set(summary["by_stage"]) == set(JOB_STAGES) and len(summary["workers"]) == 2, summary["workers"]
    run = session.get(MigrationRun, run.id)
    assert run.status == "COMPLETED" and run.report["reconciliation"]["overall"] == "PASS", run.report["reconciliation"]["failures"][:5]
    # concurrent load jobs from two processes produced no duplicate or conflicting target records
    load = next(st for st in run.stages if st.name == "LOAD").metrics
    assert load["conflicts"] == 0 and run.report["exceptions"]["count"] == 0


def _drain(workers):
    while True:
        if not any(w.run_once() for w in workers):
            break


def test_pipelined_run_overlaps_stages_and_barrier_mode_does_not(session, slice_result):
    from sdtf.db import get_session_factory

    results = {}
    for pipelined in (True, False):
        run = start_run(session, slice_result["project_id"], slice_result["manifest_id"], slice_result["ruleset_id"], "operator", execution="DISTRIBUTED", pipelined=pipelined)
        session.commit()
        _drain([Worker(get_session_factory(), f"p{int(pipelined)}-a"), Worker(get_session_factory(), f"p{int(pipelined)}-b")])
        session.expire_all()
        run = session.get(MigrationRun, run.id)
        assert run.status == "COMPLETED" and run.report["reconciliation"]["overall"] == "PASS"
        stages = {st.name: st for st in run.stages}
        summary = job_summary(session, run.id)
        assert summary["by_status"] == {"DONE": 3 * run.metrics["partitions"] + run.metrics["stage_jobs"]["RECONCILE"]}
        assert stages["RECONCILE"].started_at >= stages["LOAD"].finished_at, "reconciliation waits for every load job"
        results[pipelined] = (run, stages)
    pr, ps = results[True]
    assert pr.metrics["pipelined"] is True and ps["TRANSFORM"].metrics["pipelined"] is True
    # pipelined: transformation (and load) started before extraction had closed
    assert ps["TRANSFORM"].started_at < ps["EXTRACT"].finished_at and ps["LOAD"].started_at < ps["EXTRACT"].finished_at
    assert pr.metrics["pipeline_overlap_s"] > 0
    br, bs = results[False]
    assert br.metrics["pipelined"] is False and br.metrics["pipeline_overlap_s"] == 0
    assert bs["TRANSFORM"].started_at >= bs["EXTRACT"].finished_at and bs["LOAD"].started_at >= bs["TRANSFORM"].finished_at


def test_idle_worker_repairs_missing_successor_and_lost_wakeup(session, slice_result):
    """A worker that dies between finishing a job and enqueueing its successor must not stall the run."""
    from sdtf.db import get_session_factory
    from sdtf.runtime.worker import process_job, repair_pipeline

    run = start_run(session, slice_result["project_id"], slice_result["manifest_id"], slice_result["ruleset_id"], "operator", execution="DISTRIBUTED")
    session.commit()
    job = claim_job(session, "dying-worker")
    process_job(session, job)  # DONE, but no TRANSFORM successor enqueued (simulated crash right after)
    assert job_summary(session, run.id)["by_stage"].get("TRANSFORM") is None
    assert repair_pipeline(session, run.id) == 1
    assert job_summary(session, run.id)["by_stage"]["TRANSFORM"] == {"QUEUED": 1}
    assert repair_pipeline(session, run.id) == 0, "idempotent"
    w = Worker(get_session_factory(), "healer")
    w.run(until_idle=True)
    session.expire_all()
    run = session.get(MigrationRun, run.id)
    assert run.status == "COMPLETED" and run.report["reconciliation"]["overall"] == "PASS"
    assert job_summary(session, run.id)["by_status"] == {"DONE": 3 * run.metrics["partitions"] + run.metrics["stage_jobs"]["RECONCILE"]}


def test_columnar_staging_on_object_store_url(engine, monkeypatch):
    """The same backend contract over an fsspec object-store URL (in-memory filesystem stands in for S3/GCS)."""
    from sdtf.db import session_scope

    url = "memory://sdtf-staging/prefix"
    monkeypatch.setattr(config, "settings", config.Settings(database_url=config.settings.database_url, evidence_dir=config.settings.evidence_dir, staging_backend="columnar", staging_dir=url))
    stg = ColumnarStaging(url)
    assert stg.object_store and stg.protocol == "memory"
    rows = [StagedRow("P1", "BKPF", f"5000|{i}|2024", {"BELNR": str(i)}) for i in range(3)]
    assert stg.write_partition("r-mem", "P1", rows) == 3 and stg.keys("r-mem", "BKPF") == {r.record_key for r in rows}
    assert stg.write_partition("r-mem", "P2", rows) == 0, "duplicates across partitions are ignored on object stores too"
    got = list(stg.iter_records("r-mem", partition="P1"))
    got[0].load_status, got[0].target_key = "LOADED", "x"
    stg.update_records("r-mem", got[:1])
    assert sorted(c["status"] for c in stg.counts("r-mem")) == ["LOADED", "STAGED", "STAGED"] or {c["status"] for c in stg.counts("r-mem")} == {"LOADED", "STAGED"}
    fp = stg.footprint("r-mem")
    assert fp["files"] == 1 and fp["protocol"] == "memory" and fp["location"] == url
    stg.drop_run("r-mem")
    assert stg.counts("r-mem") == []
    # full slice with staging on the object-store URL
    with session_scope() as s:
        out = run_vertical_slice(s, scale=1, seed=37)
        run = out["run"]
        assert run.status == "COMPLETED" and run.report["reconciliation"]["overall"] == "PASS"
        assert ColumnarStaging(url).footprint(run.id)["files"] > 20


def test_columnar_staging_file_url_and_missing_driver(tmp_path):
    stg = ColumnarStaging(f"file://{tmp_path}/stg")
    assert not stg.object_store and stg.protocol in ("file", "local")
    assert stg.write_partition("r", "P", [StagedRow("P", "KNA1", "1", {"KUNNR": "1"})]) == 1
    assert (tmp_path / "stg" / "r" / "KNA1" / "P.parquet").exists()
    with pytest.raises(RuntimeError, match="s3fs"):
        ColumnarStaging("s3://bucket/prefix")
