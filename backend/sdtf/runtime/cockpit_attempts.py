"""Re-upload tracking of the migration cockpit packages (rounds).

Every cockpit export is a *round* (`CockpitAttempt`): the full package (round 1) or a retry package of the
instances the previous simulation rejected. A round moves through `EXPORTED` -> `UPLOADED` (someone uploaded the
files in the *Migrate Your Data* app; note and who/when are recorded) -> `SIMULATED` (the simulation feedback
was imported and attached to the round: per instance `accepted`, `rejected` or `not_in_log`) -> `MIGRATED` (the
app's migration step was run for the round) -- or `SUPERSEDED` when a newer package replaced it before it was
simulated. The burn-down across rounds says how many instances were retried, resolved and are still rejected,
and for every still-rejected instance which rounds it went through with which messages.

The platform never talks to the app: uploads and migrations are recorded by hand (API, CLI or the Runs page),
and the feedback import is what moves a round to SIMULATED.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit.service import record_event
from ..models import CockpitAttempt, CockpitFeedback

OPEN = ("EXPORTED", "UPLOADED")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ikey(ot: str, key: str) -> str:
    return f"{ot}|{key}"


def attempts(session: Session, run_id: str) -> list[CockpitAttempt]:
    return list(session.execute(select(CockpitAttempt).where(CockpitAttempt.run_id == run_id).order_by(CockpitAttempt.sequence)).scalars().all())


def current_attempt(session: Session, run_id: str) -> CockpitAttempt | None:
    """The round feedback would attach to: the latest round not yet simulated, else the latest round."""
    rows = attempts(session, run_id)
    open_rows = [a for a in rows if a.status in OPEN]
    return (open_rows or rows or [None])[-1]


def create_attempt(session: Session, run_id: str, scope: str, instance_keys: list[tuple[str, str]], export_summary: dict, actor: str) -> CockpitAttempt:
    """Record an export as a new round; rounds still open are superseded by it."""
    rows = attempts(session, run_id)
    for a in rows:
        if a.status in OPEN:
            a.status = "SUPERSEDED"
    seq = (rows[-1].sequence + 1) if rows else 1
    a = CockpitAttempt(run_id=run_id, sequence=seq, scope=scope, status="EXPORTED", package_dir=export_summary.get("dir", ""), zip_path=export_summary.get("zip", ""), manifest_sha256=export_summary.get("manifest_sha256", ""), instances=len(instance_keys), rows=int(export_summary.get("rows", 0)), files=int(export_summary.get("files", 0)), instance_keys=[[ot, k] for ot, k in instance_keys], outcomes={}, result={}, exported_by=actor, exported_at=_now())
    session.add(a)
    session.flush()
    record_event(session, actor, "COCKPIT_ROUND_EXPORTED", "RUN", run_id, {"round": seq, "scope": scope, "instances": len(instance_keys), "superseded": [r.sequence for r in rows if r.status == "SUPERSEDED" and r.sequence != seq]})
    return a


def mark_attempt(session: Session, a: CockpitAttempt, status: str, actor: str, note: str = "") -> CockpitAttempt:
    """Record a manual step: UPLOADED (files uploaded in the app) or MIGRATED (the app's migration ran)."""
    allowed = {"UPLOADED": ("EXPORTED", "UPLOADED"), "MIGRATED": ("SIMULATED", "MIGRATED", "UPLOADED")}
    if status not in allowed:
        raise ValueError("status must be UPLOADED or MIGRATED")
    if a.status not in allowed[status]:
        raise ValueError(f"round {a.sequence} is {a.status}: cannot mark it {status}")
    if status == "UPLOADED":
        a.uploaded_at, a.uploaded_by, a.upload_note = _now(), actor, note[:400]
        a.status = "UPLOADED"
    else:
        a.migrated_at, a.migrated_by, a.migration_note = _now(), actor, note[:400]
        a.status = "MIGRATED"
    session.flush()
    record_event(session, actor, f"COCKPIT_ROUND_{status}", "RUN", a.run_id, {"round": a.sequence, "note": note[:200]})
    return a


def previous_outcomes(session: Session, run_id: str, before_sequence: int | None = None) -> dict[str, str]:
    """Latest outcome per instance over the simulated rounds (optionally only rounds before `before_sequence`)."""
    out: dict[str, str] = {}
    for a in attempts(session, run_id):
        if before_sequence is not None and a.sequence >= before_sequence:
            continue
        if a.status in ("SIMULATED", "MIGRATED") and a.outcomes:
            out.update({k: v for k, v in a.outcomes.items() if v in ("accepted", "rejected")})
    return out


def record_outcomes(session: Session, a: CockpitAttempt, rejected: set[tuple[str, str]], in_log: set[tuple[str, str]], actor: str, feedback_file: str = "") -> dict:
    """Attach a simulation result to a round: per instance of the round accepted / rejected / not_in_log; the
    result also counts the instances this round resolved (rejected in an earlier round, accepted now)."""
    before = previous_outcomes(session, a.run_id, a.sequence)
    outcomes: dict[str, str] = {}
    for ot, k in a.instance_keys or []:
        ik = _ikey(ot, k)
        outcomes[ik] = "rejected" if (ot, k) in rejected else ("accepted" if (ot, k) in in_log else "not_in_log")
    # messages for instances outside the round (e.g. a full log imported against a retry round) count too
    for ot, k in rejected:
        outcomes.setdefault(_ikey(ot, k), "rejected")
    for ot, k in in_log:
        outcomes.setdefault(_ikey(ot, k), "accepted")
    resolved = sorted(k for k, v in outcomes.items() if v == "accepted" and before.get(k) == "rejected")
    regressed = sorted(k for k, v in outcomes.items() if v == "rejected" and before.get(k) == "accepted")
    a.outcomes = outcomes
    a.result = {"rejected": sum(1 for v in outcomes.values() if v == "rejected"), "accepted": sum(1 for v in outcomes.values() if v == "accepted"), "not_in_log": sum(1 for v in outcomes.values() if v == "not_in_log"), "resolved": len(resolved), "regressed": len(regressed), "feedback_file": feedback_file}
    a.status = "SIMULATED"
    a.simulated_at, a.simulated_by = _now(), actor
    session.flush()
    record_event(session, actor, "COCKPIT_ROUND_SIMULATED", "RUN", a.run_id, {"round": a.sequence, **{k: v for k, v in a.result.items() if k != "feedback_file"}})
    return a.result


def still_rejected(session: Session, run_id: str) -> set[tuple[str, str]]:
    """Instances whose latest simulation outcome is rejected (from the rounds; falls back to the feedback rows
    when no round was simulated yet)."""
    last = previous_outcomes(session, run_id)
    if last:
        return {tuple(k.split("|", 1)) for k, v in last.items() if v == "rejected"}
    return {(f.object_type, f.matched_key) for f in session.execute(select(CockpitFeedback).where(CockpitFeedback.run_id == run_id, CockpitFeedback.matched.is_(True), CockpitFeedback.severity == "E")).scalars().all()}


def attempt_out(a: CockpitAttempt) -> dict:
    return {"id": a.id, "sequence": a.sequence, "scope": a.scope, "status": a.status, "instances": a.instances, "rows": a.rows, "files": a.files, "package_dir": a.package_dir, "zip_path": a.zip_path, "manifest_sha256": a.manifest_sha256, "exported_at": a.exported_at, "exported_by": a.exported_by, "uploaded_at": a.uploaded_at, "uploaded_by": a.uploaded_by, "upload_note": a.upload_note, "simulated_at": a.simulated_at, "simulated_by": a.simulated_by, "migrated_at": a.migrated_at, "migrated_by": a.migrated_by, "migration_note": a.migration_note, "result": a.result or {}}


def burndown(session: Session, run_id: str) -> dict:
    """Rounds with their results, the instances still rejected with their history, and the convergence line."""
    rows = attempts(session, run_id)
    history: dict[str, list[dict]] = {}
    for a in rows:
        for k, v in (a.outcomes or {}).items():
            history.setdefault(k, []).append({"round": a.sequence, "outcome": v})
    last = previous_outcomes(session, run_id)
    remaining = sorted(k for k, v in last.items() if v == "rejected")
    fb = {}
    if remaining:
        for f in session.execute(select(CockpitFeedback).where(CockpitFeedback.run_id == run_id, CockpitFeedback.matched.is_(True), CockpitFeedback.severity == "E")).scalars().all():
            fb.setdefault(_ikey(f.object_type, f.matched_key), []).append({"round": f.attempt_sequence, "message": f.message, "category": f.category, "sheet": f.sheet, "field": f.field})
    simulated = [a for a in rows if a.status in ("SIMULATED", "MIGRATED")]
    line = [{"round": a.sequence, "scope": a.scope, "retried": a.instances, **{k: a.result.get(k, 0) for k in ("rejected", "accepted", "not_in_log", "resolved", "regressed")}} for a in simulated]
    current = current_attempt(session, run_id)
    return {"rounds": [attempt_out(a) for a in rows], "current": attempt_out(current) if current else None, "line": line, "remaining": len(remaining), "resolved_total": sum(r["resolved"] for r in line), "ever_rejected": sum(1 for h in history.values() if any(x["outcome"] == "rejected" for x in h)), "still_rejected": [{"instance": k, "object_type": k.split("|", 1)[0], "key": k.split("|", 1)[1], "history": history.get(k, []), "messages": fb.get(k, [])[-3:]} for k in remaining[:500]], "converged": bool(simulated) and not remaining}


def reset_attempts(session: Session, run_id: str) -> int:
    """Forget the simulation results of every round (rounds and their uploads stay)."""
    n = 0
    for a in attempts(session, run_id):
        if a.status in ("SIMULATED", "MIGRATED"):
            a.status = "UPLOADED" if a.uploaded_at else "EXPORTED"
            a.outcomes, a.result, a.simulated_at, a.simulated_by = {}, {}, None, ""
            n += 1
    session.flush()
    return n
