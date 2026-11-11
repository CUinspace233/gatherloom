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

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    and_,
    case,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Session

from app.contracts import ProcessingStatus
from app.models import Entry, Job, Message, Project

INGESTION = "entry_ingestion"
ANSWER = "project_answer"
MAINTENANCE = "project_maintenance"
REVIEW = "project_review"
KINDS = (ANSWER, INGESTION, MAINTENANCE, REVIEW)  # claim order: answers first
PROJECT_KINDS = (MAINTENANCE, REVIEW)
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
    """The subject is gone: entry deleted or replaced, or question removed."""


@dataclass(frozen=True)
class Claim:
    id: UUID
    owner_id: UUID
    kind: str
    entry_id: UUID | None
    revision: int | None
    message_id: UUID | None
    project_id: UUID | None
    generation: int
    attempts: int

    @property
    def thread_id(self) -> str:
        return f"{self.owner_id}:{self.id}:{self.generation}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def owner_lock_key(owner_id: UUID, kind: str, project_id: UUID | None = None) -> int:
    scope = project_id.bytes if project_id is not None else b""
    digest = sha256(b"jobs" + owner_id.bytes + kind.encode() + scope).digest()
    return int.from_bytes(digest[:8], signed=True)


def claimable(now: datetime) -> ColumnElement[bool]:
    return and_(
        Job.kind.in_(KINDS),
        or_(
            and_(Job.status == "queued", Job.run_after <= now),
            and_(Job.status == "running", Job.lease_expires_at < now),
        ),
    )


def claim(db: Session, worker_id: str, now: datetime) -> Claim | None:
    """Lease the next runnable job.

    At most one job runs per account and kind, or per project and kind for
    project jobs, so a project's maintenance is serial. Answers are claimed
    first so questions do not wait behind a backlog of entries.
    """
    candidates = db.execute(
        select(Job.id, Job.owner_id, Job.kind, Job.project_id)
        .where(claimable(now))
        .order_by(case((Job.kind == ANSWER, 0), else_=1), Job.run_after, Job.created_at)
        .limit(20)
    ).all()
    for job_id, owner_id, kind, project_id in candidates:
        scope = project_id if kind in PROJECT_KINDS else None
        # Serialize claims per scope so the busy check cannot race.
        if not db.scalar(
            select(
                func.pg_try_advisory_xact_lock(owner_lock_key(owner_id, kind, scope))
            )
        ):
            continue
        busy_filter = [
            Job.owner_id == owner_id,
            Job.kind == kind,
            Job.status == "running",
            Job.lease_expires_at >= now,
            Job.id != job_id,
        ]
        if scope is not None:
            busy_filter.append(Job.project_id == scope)
        busy = db.scalar(select(Job.id).where(*busy_filter).limit(1))
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
            id=job.id,
            owner_id=job.owner_id,
            kind=job.kind,
            entry_id=job.entry_id,
            revision=job.revision,
            message_id=job.message_id,
            project_id=job.project_id,
            generation=job.generation,
            attempts=job.attempts,
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
    """Lock the subject and job for writing; raise unless this worker owns it.

    Lock order matches the API (subject first, then job) to avoid deadlocks.
    """
    if claim.kind in PROJECT_KINDS:
        subject_live = (
            db.scalar(
                select(Project.id)
                .where(
                    Project.owner_id == claim.owner_id, Project.id == claim.project_id
                )
                .with_for_update(read=True)
            )
            is not None
        )
    elif claim.kind == ANSWER:
        subject_live = (
            db.scalar(
                select(Message.id)
                .where(
                    Message.owner_id == claim.owner_id, Message.id == claim.message_id
                )
                .with_for_update(read=True)
            )
            is not None
        )
    else:
        entry = db.scalar(
            select(Entry)
            .where(Entry.owner_id == claim.owner_id, Entry.id == claim.entry_id)
            .with_for_update(read=True)
        )
        subject_live = (
            entry is not None
            and entry.deleted_at is None
            and entry.current_revision == claim.revision
        )
    job = db.scalar(select(Job).where(Job.id == claim.id).with_for_update())
    if job is None or job.status != "running" or job.lease_owner != worker_id:
        raise LeaseLost
    if not subject_live:
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
