"""Distributed stage workers.

A DISTRIBUTED run is executed as stage jobs, one per partition and stage: EXTRACT -> TRANSFORM -> LOAD. Any number
of `sdtf worker` processes (pods) claim queued jobs with a lease, process them against the shared staging backend
and mark them DONE.

Pipelined mode (default): a partition's TRANSFORM job is enqueued as soon as its EXTRACT job is DONE, and its LOAD
job as soon as its TRANSFORM is DONE, so partitions flow through the stages independently; workers prefer later
stages to drain the pipeline. Barrier mode: a stage's jobs are enqueued only when the previous stage is complete.
In both modes a stage closes (aggregated metrics) when all partitions are through it, under an atomic status
transition (RUNNING -> ADVANCING) so exactly one worker closes it; reconciliation and the report run once LOAD is
closed. Expired leases are re-queued (crash recovery), attempts are counted, and idle workers repair missing
successor jobs so a crash between finishing a job and enqueueing its successor cannot stall a run.
"""
from __future__ import annotations

import socket
import time
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from .. import config
from .. import observability as obs
from ..audit.service import record_event
from ..catalog.store import RecordStore
from ..models import ExtractionJob, MigrationRun, RuleSet, SapSystem, ScopeManifest
from ..reconciliation.service import reconcile_partition, reconcile_partitions, run_summary
from ..rules.engine import parse_ruleset
from ..staging import get_backend
from .api_load import build_loader
from .extraction import ManifestExtractor, build_extractor, extract_partition
from .transform import run_transformation

JOB_STAGES = ["EXTRACT", "TRANSFORM", "LOAD", "RECONCILE"]
PARTITION_STAGES = ["EXTRACT", "TRANSFORM", "LOAD"]  # one job per extraction partition; RECONCILE has its own job list


def _now():
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _extractor_for(session: Session, run: MigrationRun, cache: dict | None = None) -> ManifestExtractor:
    cache = cache if cache is not None else {}
    key = ("ex", run.id)
    if key not in cache:
        m = session.get(ScopeManifest, run.manifest_id)
        src = session.get(SapSystem, run.source_system_id)
        cache[key] = build_extractor(session, src, m.selection.get("classification", {}), set(m.definition["company_codes"]))
    return cache[key]


def _ruleset_for(session: Session, run: MigrationRun, cache: dict | None = None):
    cache = cache if cache is not None else {}
    key = ("rs", run.ruleset_id)
    if key not in cache:
        cache[key] = parse_ruleset(session.get(RuleSet, run.ruleset_id).source_yaml)
    return cache[key]


def _stage(run: MigrationRun, name: str):
    return next(s for s in run.stages if s.name == name)


# ------------------------------------------------------------------------------------- enqueue / requeue
def enqueue_stage_jobs(session: Session, run: MigrationRun, stage: str) -> int:
    if stage == "RECONCILE":
        jobs = reconcile_partitions(session, run)
        for pid, otype, est in jobs:
            session.add(ExtractionJob(run_id=run.id, stage="RECONCILE", partition_id=pid, object_type=otype, est_rows=est))
        run.metrics = {**run.metrics, "stage_jobs": {**run.metrics.get("stage_jobs", {}), "RECONCILE": len(jobs)}}
        session.flush()
        return len(jobs)
    ex = _extractor_for(session, run)
    plan = ex.plan()
    n = 0
    for p in plan.partitions:
        session.add(ExtractionJob(run_id=run.id, stage=stage, partition_id=p.id, object_type=p.object_type, est_rows=p.est_rows))
        n += 1
    if stage == "EXTRACT":
        run.snapshot_id = plan.snapshot_id
        run.metrics = {**run.metrics, "partitions": n}
    session.flush()
    return n


def _next_stage(stage: str) -> str | None:
    i = JOB_STAGES.index(stage)
    return JOB_STAGES[i + 1] if i + 1 < len(JOB_STAGES) else None


def enqueue_successor(session: Session, job: ExtractionJob) -> bool:
    """Pipelined mode: queue the next stage's job for this partition (idempotent under the unique constraint).
    RECONCILE jobs are table-partitioned and are enqueued when LOAD closes, never per extraction partition."""
    nxt = _next_stage(job.stage)
    if nxt is None or nxt == "RECONCILE":
        return False
    exists = session.execute(select(ExtractionJob.id).where(ExtractionJob.run_id == job.run_id, ExtractionJob.stage == nxt, ExtractionJob.partition_id == job.partition_id)).first()
    if exists:
        return False
    run = session.get(MigrationRun, job.run_id)
    st = _stage(run, nxt)
    if st.status == "PENDING":
        st.status, st.started_at = "RUNNING", _now()
        st.metrics = {"execution": "DISTRIBUTED", "pipelined": True}
    session.add(ExtractionJob(run_id=job.run_id, stage=nxt, partition_id=job.partition_id, object_type=job.object_type, est_rows=job.est_rows))
    try:
        session.commit()
        return True
    except Exception:  # noqa: BLE001 - another worker queued it first
        session.rollback()
        return False


def repair_pipeline(session: Session, run_id: str) -> int:
    """Enqueue successor jobs that are missing for DONE partitions of a pipelined run (crash between finishing a
    job and enqueueing its successor). Idempotent; returns the number of jobs added."""
    run = session.get(MigrationRun, run_id)
    if run is None or run.status != "RUNNING" or not run.metrics.get("pipelined", True):
        return 0
    jobs = session.execute(select(ExtractionJob).where(ExtractionJob.run_id == run_id)).scalars().all()
    have = {(j.stage, j.partition_id) for j in jobs}
    n = 0
    for j in jobs:
        nxt = _next_stage(j.stage)
        if j.status == "DONE" and nxt and (nxt, j.partition_id) not in have:
            if enqueue_successor(session, j):
                n += 1
                have.add((nxt, j.partition_id))
    return n


def repair_stalled_runs(session: Session, actor: str = "worker") -> int:
    """Idle-time self-healing: enqueue missing successors and close stages whose last job finished while another
    worker held the advancement lock. Returns the number of repairs / advancements performed."""
    runs = session.execute(select(MigrationRun.id).where(MigrationRun.status == "RUNNING")).scalars().all()
    n = 0
    for rid in runs:
        n += repair_pipeline(session, rid)
        if advance_run_if_stage_complete(session, rid, actor) is not None:
            n += 1
    return n


def enqueue_extraction_jobs(session: Session, run: MigrationRun) -> int:  # backwards-compatible name
    return enqueue_stage_jobs(session, run, "EXTRACT")


def requeue_jobs(session: Session, run_id: str, statuses=("FAILED",)) -> int:
    res = session.execute(update(ExtractionJob).where(ExtractionJob.run_id == run_id, ExtractionJob.status.in_(list(statuses))).values(status="QUEUED", worker_id=None, lease_until=None, error=""))
    session.flush()
    return res.rowcount or 0


def requeue_stale(session: Session, now: datetime | None = None) -> int:
    """Re-queue CLAIMED jobs whose lease expired (worker died or hung)."""
    now = now or _now()
    stale = [j for j in session.execute(select(ExtractionJob).where(ExtractionJob.status == "CLAIMED")).scalars() if _aware(j.lease_until) and _aware(j.lease_until) < now]
    for j in stale:
        j.status, j.worker_id, j.lease_until, j.error = "QUEUED", None, None, f"lease expired (previous worker {j.worker_id})"
    session.flush()
    if stale:
        obs.counter("sdtf.jobs.requeued", len(stale), reason="lease_expired")
    return len(stale)


# ------------------------------------------------------------------------------------- claim / process
def claim_job(session: Session, worker_id: str, lease_seconds: int | None = None) -> ExtractionJob | None:
    """Atomically claim one QUEUED job: the conditional UPDATE succeeds for exactly one worker per job."""
    lease = lease_seconds or config.settings.job_lease_seconds
    for _ in range(5):
        priority = case((ExtractionJob.stage == "RECONCILE", 0), (ExtractionJob.stage == "LOAD", 1), (ExtractionJob.stage == "TRANSFORM", 2), else_=3)
        cand = session.execute(select(ExtractionJob).where(ExtractionJob.status == "QUEUED").order_by(priority, ExtractionJob.created_at, ExtractionJob.est_rows.desc()).limit(1)).scalars().first()
        if cand is None:
            return None
        now = _now()
        res = session.execute(update(ExtractionJob).where(ExtractionJob.id == cand.id, ExtractionJob.status == "QUEUED").values(status="CLAIMED", worker_id=worker_id, claimed_at=now, lease_until=now + timedelta(seconds=lease), attempts=ExtractionJob.attempts + 1))
        session.commit()
        if res.rowcount == 1:
            session.refresh(cand)
            return cand
    return None


def process_job(session: Session, job: ExtractionJob, cache: dict | None = None) -> int:
    with obs.timed("sdtf.job", run_id=job.run_id, stage=job.stage, partition=job.partition_id, worker_id=job.worker_id, attempt=job.attempts):
        return _process_job(session, job, cache)


def _process_job(session: Session, job: ExtractionJob, cache: dict | None = None) -> int:
    run = session.get(MigrationRun, job.run_id)
    backend = get_backend(run.metrics.get("staging_backend"), session=session)
    try:
        if job.stage == "EXTRACT":
            ex = _extractor_for(session, run, cache)
            part = next(p for p in ex.plan().partitions if p.id == job.partition_id)
            n = extract_partition(ex, part, run.id, backend)
            metrics = {"records": n}
        elif job.stage == "TRANSFORM":
            metrics = run_transformation(session, run.id, _ruleset_for(session, run, cache), backend=backend, partition=job.partition_id)
            n = metrics["records"]
        elif job.stage == "LOAD":
            tgt = session.get(SapSystem, run.target_system_id)
            metrics = build_loader(session, tgt, run.id, backend=backend, load_mode=run.metrics.get("load_mode")).load(partition=job.partition_id)
            n = metrics["loaded"] + metrics["skipped_duplicate"] + metrics["matched_config"]
        elif job.stage == "RECONCILE":
            key = ("tgt", run.target_system_id, run.id)
            if cache is not None and key not in cache:
                cache[key] = RecordStore.load(session, run.target_system_id)
            metrics = reconcile_partition(session, run, job.partition_id, backend=backend, target=cache[key] if cache is not None else None)
            n = metrics["checks"]
        else:  # pragma: no cover
            raise ValueError(f"unknown job stage {job.stage}")
    except Exception as e:  # noqa: BLE001
        session.rollback()
        job = session.get(ExtractionJob, job.id)
        job.status, job.error, job.finished_at = "FAILED", f"{e}\n{traceback.format_exc(limit=3)}", _now()
        session.commit()
        obs.counter("sdtf.jobs", 1, stage=job.stage, status="FAILED")
        raise
    job.status, job.records, job.finished_at, job.error, job.metrics = "DONE", n, _now(), "", metrics
    session.commit()
    obs.counter("sdtf.jobs", 1, stage=job.stage, status="DONE")
    obs.counter("sdtf.job.records", n, stage=job.stage)
    return n


# ------------------------------------------------------------------------------------- stage advancement
def _aggregate(jobs: list[ExtractionJob]) -> dict:
    """Sum numeric job metrics, merge dict metrics by key."""
    agg: dict = defaultdict(float)
    dicts: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for j in jobs:
        for k, v in (j.metrics or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                agg[k] += v
            elif isinstance(v, dict):
                for kk, vv in v.items():
                    if isinstance(vv, (int, float)):
                        dicts[k][kk] += vv
    out = {k: (int(v) if float(v).is_integer() else round(v, 3)) for k, v in agg.items()}
    out.update({k: {kk: int(vv) if float(vv).is_integer() else vv for kk, vv in d.items()} for k, d in dicts.items()})
    return out


def advance_run_if_stage_complete(session: Session, run_id: str, actor: str) -> MigrationRun | None:
    """Close every job stage whose jobs are all DONE (one job per partition), under an atomic RUNNING -> ADVANCING
    transition. Barrier mode enqueues the next stage on close; pipelined mode enqueues successors per job instead.
    When LOAD closes, reconciliation and the report run (once)."""
    run = session.get(MigrationRun, run_id)
    if run is None or run.status != "RUNNING":
        return None
    partitions = run.metrics.get("partitions") or 0
    pipelined = run.metrics.get("pipelined", True)
    expected = {st: (run.metrics.get("stage_jobs", {}).get(st) if st == "RECONCILE" else partitions) for st in JOB_STAGES}
    counts = {st: n for st, n in session.execute(select(ExtractionJob.stage, func.count()).where(ExtractionJob.run_id == run_id, ExtractionJob.status == "DONE").group_by(ExtractionJob.stage))}
    closable = [st for st in JOB_STAGES if _stage(run, st).status == "RUNNING" and expected[st] and counts.get(st, 0) >= expected[st]]
    if not closable:
        return None
    res = session.execute(update(MigrationRun).where(MigrationRun.id == run_id, MigrationRun.status == "RUNNING").values(status="ADVANCING"))
    session.commit()
    if res.rowcount != 1:
        return None  # another worker is advancing
    run = session.get(MigrationRun, run_id)
    reconcile_closed = False
    for current in closable:
        st = _stage(run, current)
        if st.status != "RUNNING":
            continue
        jobs = session.execute(select(ExtractionJob).where(ExtractionJob.run_id == run_id, ExtractionJob.stage == current)).scalars().all()
        if any(j.status != "DONE" for j in jobs):
            continue
        metrics = _aggregate(jobs)
        first = min(_aware(j.claimed_at) for j in jobs)
        last = max(_aware(j.finished_at) for j in jobs)
        metrics.update({"partitions_total": len(jobs), "workers": sorted({j.worker_id for j in jobs if j.worker_id}), "duration_s": round((last - first).total_seconds(), 3), "execution": "DISTRIBUTED", "pipelined": pipelined, "attempts": sum(j.attempts for j in jobs)})
        if current == "EXTRACT":
            backend = get_backend(run.metrics.get("staging_backend"), session=session)
            cnt = backend.counts(run_id)
            metrics.update({"records": sum(c["count"] for c in cnt), "by_table": {c["table"]: c["count"] for c in cnt}, "snapshot_id": run.snapshot_id, "staging_backend": backend.name})
            metrics["records_per_second"] = round(metrics["records"] / metrics["duration_s"], 1) if metrics["duration_s"] else None
        if current == "RECONCILE":
            metrics.update(run_summary(session, run_id))
        st.metrics, st.status, st.finished_at = metrics, "DONE", _now()
        st.duration_ms = round(metrics["duration_s"] * 1000, 1)
        st.checkpoint = {"partitions_done": sorted(j.partition_id for j in jobs)}
        obs.histogram("sdtf.stage.duration", st.duration_ms, stage=current, execution="DISTRIBUTED")
        obs.counter("sdtf.stage.closed", 1, stage=current, pipelined=pipelined)
        nxt = _next_stage(current)
        if nxt is None:
            reconcile_closed = True
        elif nxt == "RECONCILE" or not pipelined:  # reconciliation is table-partitioned: enqueued at LOAD close in both modes
            if current == "LOAD":
                run.metrics = {**run.metrics, "pipeline_overlap_s": _overlap(run)}
            ns = _stage(run, nxt)
            ns.status, ns.started_at = "RUNNING", _now()
            ns.metrics = {"jobs": enqueue_stage_jobs(session, run, nxt), "execution": "DISTRIBUTED", "pipelined": pipelined}
        session.commit()
        record_event(session, actor, "STAGE_CLOSED", "RUN", run_id, {"stage": current, "next": nxt, "pipelined": pipelined})
        session.commit()
    run.status = "RUNNING"
    session.commit()
    if reconcile_closed:
        from .pipeline import execute_run

        return execute_run(session, run, actor)  # only REPORT is left
    # jobs may have completed while this worker held the ADVANCING lock: re-evaluate before returning
    return advance_run_if_stage_complete(session, run_id, actor) or run


def _overlap(run: MigrationRun) -> float:
    """Seconds by which TRANSFORM started before EXTRACT finished (pipelining effect; 0 in barrier mode)."""
    ex, tr = _stage(run, "EXTRACT"), _stage(run, "TRANSFORM")
    if not (ex.finished_at and tr.started_at):
        return 0.0
    return round(max(0.0, (_aware(ex.finished_at) - _aware(tr.started_at)).total_seconds()), 3)


def finalize_if_complete(session: Session, run_id: str, actor: str) -> MigrationRun | None:  # backwards-compatible name
    return advance_run_if_stage_complete(session, run_id, actor)


# ------------------------------------------------------------------------------------- worker loop
class Worker:
    def __init__(self, session_factory, worker_id: str | None = None, lease_seconds: int | None = None, poll_seconds: float | None = None):
        self.session_factory = session_factory
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.lease = lease_seconds
        self.poll = poll_seconds or config.settings.worker_poll_seconds
        self.cache: dict = {}
        self.processed = 0

    def run_once(self) -> bool:
        """Claim and process one job. Returns False when no job was available."""
        session = self.session_factory()
        try:
            requeue_stale(session)
            session.commit()
            job = claim_job(session, self.worker_id, self.lease)
            if job is None:
                if repair_stalled_runs(session, f"worker:{self.worker_id}"):
                    session.commit()
                    return True
                return False
            run_id, partition, stage = job.run_id, job.partition_id, job.stage
            try:
                n = process_job(session, job, self.cache)
            except Exception:  # noqa: BLE001 - recorded on the job; the worker keeps serving other jobs
                return True
            self.processed += 1
            record_event(session, f"worker:{self.worker_id}", "JOB_DONE", "RUN", run_id, {"stage": stage, "partition": partition, "records": n})
            session.commit()
            run = session.get(MigrationRun, run_id)
            if run.metrics.get("pipelined", True):
                enqueue_successor(session, job)
            advance_run_if_stage_complete(session, run_id, f"worker:{self.worker_id}")
            session.commit()
            return True
        finally:
            session.close()

    def run(self, until_idle: bool = False, max_jobs: int | None = None) -> int:
        idle_polls = 0
        while True:
            worked = self.run_once()
            if max_jobs and self.processed >= max_jobs:
                break
            if not worked:
                idle_polls += 1
                if until_idle and idle_polls >= 2:
                    break
                time.sleep(self.poll)
            else:
                idle_polls = 0
        return self.processed


def job_summary(session: Session, run_id: str) -> dict:
    jobs = session.execute(select(ExtractionJob).where(ExtractionJob.run_id == run_id).order_by(ExtractionJob.created_at)).scalars().all()
    by: dict[str, int] = {}
    by_stage: dict[str, dict[str, int]] = {}
    for j in jobs:
        by[j.status] = by.get(j.status, 0) + 1
        by_stage.setdefault(j.stage, {})[j.status] = by_stage.setdefault(j.stage, {}).get(j.status, 0) + 1
    return {"total": len(jobs), "by_status": by, "by_stage": by_stage, "workers": sorted({j.worker_id for j in jobs if j.worker_id}), "jobs": [{"id": j.id, "stage": j.stage, "partition": j.partition_id, "object_type": j.object_type, "status": j.status, "worker": j.worker_id, "attempts": j.attempts, "records": j.records, "claimed_at": j.claimed_at, "finished_at": j.finished_at, "error": j.error[:200]} for j in jobs]}
