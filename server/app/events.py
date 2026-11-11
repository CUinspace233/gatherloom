"""Project events and debounced maintenance scheduling (product-plan §4.3).

Every user-made change to a project's material writes an event in the same
transaction as the change, and makes sure one maintenance job is queued for
the project. A queued job is not moved, so changes within the debounce
window are merged into one run.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.jobs import MAINTENANCE
from app.models import Job, ProjectEntry, ProjectEvent

DEBOUNCE = timedelta(minutes=2)

ENTRY_READY = "entry_ready"
ENTRY_ADDED = "entry_added"
ENTRY_REMOVED = "entry_removed"
ENTRY_DELETED = "entry_deleted"
QUESTION_ADDED = "question_added"
QUESTION_CHANGED = "question_changed"
DECISION_CONFIRMED = "decision_confirmed"
DECISION_REVISED = "decision_revised"
DECISION_SUPERSEDED = "decision_superseded"


def schedule_maintenance(
    db: Session, owner_id: UUID, project_id: UUID, delay: timedelta = DEBOUNCE
) -> None:
    db.execute(
        insert(Job)
        .values(
            owner_id=owner_id,
            project_id=project_id,
            kind=MAINTENANCE,
            run_after=datetime.now(UTC) + delay,
        )
        .on_conflict_do_nothing(
            index_elements=["project_id"],
            index_where=text("kind = 'project_maintenance' AND status = 'queued'"),
        )
    )


def record(
    db: Session,
    owner_id: UUID,
    project_id: UUID,
    kind: str,
    subject_id: UUID | None = None,
) -> None:
    db.add(
        ProjectEvent(
            owner_id=owner_id, project_id=project_id, kind=kind, subject_id=subject_id
        )
    )
    schedule_maintenance(db, owner_id, project_id)


def record_for_entry(db: Session, owner_id: UUID, entry_id: UUID, kind: str) -> None:
    """One event per project the entry belongs to."""
    for project_id in db.scalars(
        select(ProjectEntry.project_id).where(
            ProjectEntry.owner_id == owner_id, ProjectEntry.entry_id == entry_id
        )
    ):
        record(db, owner_id, project_id, kind, entry_id)


def latest_seq(db: Session, project_id: UUID) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.max(ProjectEvent.seq), 0)).where(
                ProjectEvent.project_id == project_id
            )
        )
        or 0
    )
