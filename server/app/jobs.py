"""Background job leases: claim, heartbeat, retry and fencing.

Execution is at-least-once. A worker claims a job with a time-limited lease and
keeps it alive with heartbeats; when the lease expires another worker may take
over. Every write a worker makes goes through ``fence``, which checks under a
row lock that the job is still running and leased to that worker, so a worker
that lost its lease cannot publish anything.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, CursorResult, and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.contracts import ProcessingStatus
from app.models import Entry, Job

INGESTION = "entry_ingestion"
LEASE = timedelta(seconds=60)
MAX_ATTEMPTS = 3
BACKOFF = timedelta(seconds=30)
ACTIVE = ("queued", "running")


INDEX_STAGES = ("load", "chunk", "embed")


def processing_status(status: str, indexed_at: datetime | None) -> ProcessingStatus:
    """The client-facing state of a job (product-plan §3.4)."""
    if status in ACTIVE and indexed_at is not None:
        return "searchable"
    if status == "failed" and indexed_at is not None:
        return "partial"
    return cast(ProcessingStatus, status)


def failed_step(stage: str | None) -> Literal["index", "card"]:
    """Which part failed: building the search index, or the knowledge card."""
    return "index" if stage is None or stage in INDEX_STAGES else "card"


class JobError(Exception):
    """A failure the job records; ``code`` is safe to show and log."""

    retryable = False

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class Retryable(JobError):
    """Transient: network errors, timeouts, rate limits, server errors."""

    retryable = True


class Permanent(JobError):
    """Retrying will not help: bad credentials, quota, invalid model output."""


class LeaseLost(Exception):
    """Another worker owns the job now, or it was cancelled."""


class Cancelled(Exception):
    """The entry was deleted or replaced by a newer revision."""


@dataclass(frozen=True)
class Claim:
    id: UUID
    owner_id: UUID
    entry_id: UUID
    revision: int
    generation: int
    attempts: int

    @property
    def thread_id(self) -> str:
        return f"{self.owner_id}:{self.id}:{self.generation}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def owner_lock_key(owner_id: UUID) -> int:
    return int.from_bytes(sha256(b"jobs" + owner_id.bytes).digest()[:8], signed=True)


def claimable(now: datetime) -> ColumnElement[bool]:
    return and_(
        Job.kind == INGESTION,
        or_(
            and_(Job.status == "queued", Job.run_after <= now),
            and_(Job.status == "running", Job.lease_expires_at < now),
        ),
    )


def claim(db: Session, worker_id: str, now: datetime) -> Claim | None:
    """Lease the next runnable job, at most one running job per account."""
    candidates = db.execute(
        select(Job.id, Job.owner_id)
        .where(claimable(now))
        .order_by(Job.run_after, Job.created_at)
        .limit(20)
    ).all()
    for job_id, owner_id in candidates:
        # Serialize claims per account so the busy check below cannot race.
        if not db.scalar(
            select(func.pg_try_advisory_xact_lock(owner_lock_key(owner_id)))
        ):
            continue
        busy = db.scalar(
            select(Job.id)
            .where(
                Job.owner_id == owner_id,
                Job.status == "running",
                Job.lease_expires_at >= now,
                Job.id != job_id,
            )
            .limit(1)
        )
        if busy is not None:
            continue
        job = db.scalar(
            select(Job)
            .where(Job.id == job_id, claimable(now))
            .with_for_update(skip_locked=True)
        )
        if job is None:
            continue
        if job.status == "running" and job.attempts >= MAX_ATTEMPTS:
            # The last attempt's worker died; do not start a fourth.
            settle(job, "failed", now, code="worker_lost", step=job.stage)
            db.commit()
            continue
        job.status = "running"
        job.attempts += 1
        job.lease_owner = worker_id
        job.lease_expires_at = now + LEASE
        job.updated_at = now
        result = Claim(
            job.id,
            job.owner_id,
            job.entry_id,
            job.revision,
            job.generation,
            job.attempts,
        )
        db.commit()
        return result
    db.rollback()
    return None


def heartbeat(db: Session, claim: Claim, worker_id: str, now: datetime) -> bool:
    """Extend the lease. False means the worker must stop."""
    extended: CursorResult[Any] = db.execute(  # type: ignore[assignment]
        update(Job)
        .where(
            Job.id == claim.id, Job.status == "running", Job.lease_owner == worker_id
        )
        .values(lease_expires_at=now + LEASE)
    )
    db.commit()
    return extended.rowcount == 1


def fence(db: Session, claim: Claim, worker_id: str) -> Job:
    """Lock the entry and job for writing; raise unless this worker owns it.

    Lock order matches the API (entry first, then job) to avoid deadlocks.
    """
    entry = db.scalar(
        select(Entry)
        .where(Entry.owner_id == claim.owner_id, Entry.id == claim.entry_id)
        .with_for_update(read=True)
    )
    job = db.scalar(select(Job).where(Job.id == claim.id).with_for_update())
    if job is None or job.status != "running" or job.lease_owner != worker_id:
        raise LeaseLost
    if (
        entry is None
        or entry.deleted_at is not None
        or entry.current_revision != claim.revision
    ):
        raise Cancelled
    return job


def settle(
    job: Job,
    status: str,
    now: datetime,
    *,
    code: str | None = None,
    step: str | None = None,
) -> None:
    job.status = status
    job.lease_owner = None
    job.lease_expires_at = None
    job.error_code = code
    job.error_step = step
    job.updated_at = now
    if status in ("succeeded", "failed", "cancelled"):
        job.finished_at = now


def record_failure(
    db: Session, claim: Claim, worker_id: str, error: JobError, now: datetime
) -> str:
    """Schedule a retry or fail the job. Returns the new status."""
    job = db.scalar(select(Job).where(Job.id == claim.id).with_for_update())
    if job is None or job.status != "running" or job.lease_owner != worker_id:
        db.rollback()
        raise LeaseLost
    step = job.stage
    if error.retryable and job.attempts < MAX_ATTEMPTS:
        settle(job, "queued", now, code=error.code, step=step)
        job.run_after = now + BACKOFF * 4 ** (job.attempts - 1)
    else:
        settle(job, "failed", now, code=error.code, step=step)
    status = job.status
    db.commit()
    return status


def record_cancelled(db: Session, claim: Claim, worker_id: str, now: datetime) -> None:
    db.execute(
        update(Job)
        .where(
            Job.id == claim.id, Job.status == "running", Job.lease_owner == worker_id
        )
        .values(
            status="cancelled",
            lease_owner=None,
            lease_expires_at=None,
            finished_at=now,
            updated_at=now,
        )
    )
    db.commit()
