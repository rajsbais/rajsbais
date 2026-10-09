"""Distributed extraction workers.

A DISTRIBUTED run enqueues one ExtractionJob per partition. Any number of `sdtf worker` processes (pods) claim jobs
with a lease, extract the partition into the shared staging backend, and mark the job DONE. The worker that
completes the last job finalises the run (transformation, load, reconciliation, report) under an atomic status
transition so exactly one worker does it. Expired leases are re-queued (crash recovery); attempts are counted.
"""
from __future__ import annotations

import socket
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import config
from ..audit.service import record_event
from ..catalog.store import RecordStore
from ..models import ExtractionJob, MigrationRun, ScopeManifest
from ..staging import get_backend
from .extraction import SyntheticStoreExtractor, extract_partition


def _now():
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _extractor_for(session: Session, run: MigrationRun, cache: dict | None = None) -> SyntheticStoreExtractor:
    cache = cache if cache is not None else {}
    if run.id not in cache:
        m = session.get(ScopeManifest, run.manifest_id)
        store = RecordStore.load(session, run.source_system_id)
        cache[run.id] = SyntheticStoreExtractor(store, m.selection.get("classification", {}), set(m.definition["company_codes"]))
    return cache[run.id]


def enqueue_extraction_jobs(session: Session, run: MigrationRun) -> int:
    ex = _extractor_for(session, run)
    plan = ex.plan()
    n = 0
    for p in plan.partitions:
        session.add(ExtractionJob(run_id=run.id, partition_id=p.id, object_type=p.object_type, est_rows=p.est_rows))
        n += 1
    run.snapshot_id = plan.snapshot_id
    session.flush()
    return n


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
    return len(stale)


def claim_job(session: Session, worker_id: str, lease_seconds: int | None = None) -> ExtractionJob | None:
    """Atomically claim one QUEUED job: the conditional UPDATE succeeds for exactly one worker per job."""
    lease = lease_seconds or config.settings.job_lease_seconds
    for _ in range(5):
        cand = session.execute(select(ExtractionJob).where(ExtractionJob.status == "QUEUED").order_by(ExtractionJob.created_at, ExtractionJob.est_rows.desc()).limit(1)).scalars().first()
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
    run = session.get(MigrationRun, job.run_id)
    ex = _extractor_for(session, run, cache)
    part = next(p for p in ex.plan().partitions if p.id == job.partition_id)
    backend = get_backend(run.metrics.get("staging_backend"), session=session)
    try:
        n = extract_partition(ex, part, run.id, backend)
    except Exception as e:  # noqa: BLE001
        job.status, job.error, job.finished_at = "FAILED", f"{e}\n{traceback.format_exc(limit=3)}", _now()
        session.commit()
        raise
    job.status, job.records, job.finished_at, job.error = "DONE", n, _now(), ""
    session.commit()
    return n


def finalize_if_complete(session: Session, run_id: str, actor: str) -> MigrationRun | None:
    """If every job of the run is DONE, flip the run to FINALIZING atomically and execute the remaining stages."""
    jobs = session.execute(select(ExtractionJob).where(ExtractionJob.run_id == run_id)).scalars().all()
    if not jobs or any(j.status != "DONE" for j in jobs):
        return None
    res = session.execute(update(MigrationRun).where(MigrationRun.id == run_id, MigrationRun.status == "RUNNING").values(status="FINALIZING"))
    session.commit()
    if res.rowcount != 1:
        return None  # another worker is finalising
    run = session.get(MigrationRun, run_id)
    st = next(s for s in run.stages if s.name == "EXTRACT")
    backend = get_backend(run.metrics.get("staging_backend"), session=session)
    counts = backend.counts(run_id)
    st.metrics = {**(st.metrics or {}), "records": sum(c["count"] for c in counts), "by_table": {c["table"]: c["count"] for c in counts}, "partitions_total": len(jobs), "workers": sorted({j.worker_id for j in jobs if j.worker_id}), "duration_s": round((max(_aware(j.finished_at) for j in jobs) - min(_aware(j.claimed_at) for j in jobs)).total_seconds(), 3), "snapshot_id": run.snapshot_id, "staging_backend": backend.name}
    st.checkpoint = {"partitions_done": sorted(j.partition_id for j in jobs)}
    st.status, st.finished_at = "DONE", _now()
    run.status = "RUNNING"
    session.commit()
    from .pipeline import execute_run

    return execute_run(session, run, actor)


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
                return False
            run_id = job.run_id
            try:
                process_job(session, job, self.cache)
            except Exception:  # noqa: BLE001 - recorded on the job; the worker keeps serving other jobs
                return True
            self.processed += 1
            finalize_if_complete(session, run_id, f"worker:{self.worker_id}")
            session.commit()
            record_event(session, f"worker:{self.worker_id}", "JOB_DONE", "RUN", run_id, {"partition": job.partition_id, "records": job.records})
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
    by = {}
    for j in jobs:
        by[j.status] = by.get(j.status, 0) + 1
    return {"total": len(jobs), "by_status": by, "workers": sorted({j.worker_id for j in jobs if j.worker_id}), "jobs": [{"id": j.id, "partition": j.partition_id, "object_type": j.object_type, "status": j.status, "worker": j.worker_id, "attempts": j.attempts, "records": j.records, "claimed_at": j.claimed_at, "finished_at": j.finished_at, "error": j.error[:200]} for j in jobs]}
