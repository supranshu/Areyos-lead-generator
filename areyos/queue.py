"""Postgres work queue. Any number of worker processes can claim jobs concurrently."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from areyos.config import get_settings
from areyos.models import Job

STALE_AFTER = timedelta(minutes=15)


def enqueue(session: Session, kind: str, payload: dict, run_id: int | None = None) -> Job:
    job = Job(kind=kind, payload=payload, run_id=run_id, max_attempts=get_settings().job_max_attempts)
    session.add(job)
    session.flush()
    return job


def claim(session: Session, worker_id: str) -> Job | None:
    # Requeue jobs whose worker died mid-flight.
    session.execute(
        update(Job)
        .where(Job.status == "running", Job.locked_at < datetime.now(timezone.utc) - STALE_AFTER)
        .values(status="queued", locked_by=None)
    )
    row = session.execute(
        text(
            """
            UPDATE jobs SET status = 'running', locked_by = :worker, locked_at = now(), attempts = attempts + 1
            WHERE id = (
                SELECT id FROM jobs
                WHERE status = 'queued' AND run_after <= now()
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id
            """
        ),
        {"worker": worker_id},
    ).first()
    session.commit()
    if row is None:
        return None
    return session.get(Job, row.id)


def complete(session: Session, job: Job) -> None:
    job.status = "done"
    job.finished_at = datetime.now(timezone.utc)
    job.last_error = None


def fail(session: Session, job: Job, error: str, retryable: bool) -> None:
    job.last_error = error[:4000]
    if retryable and job.attempts < job.max_attempts:
        job.status = "queued"
        job.run_after = datetime.now(timezone.utc) + timedelta(seconds=30 * 2 ** job.attempts)
    else:
        job.status = "failed"
        job.finished_at = datetime.now(timezone.utc)


def pending_count(session: Session, run_id: int) -> int:
    return session.scalar(
        select(func.count(Job.id)).where(Job.run_id == run_id, Job.status.in_(("queued", "running")))
    )
