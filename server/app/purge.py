"""Deleting a project and everything derived from it.

Entries themselves are kept (they may belong to other projects or return to
the inbox). Model-usage records in ``runs`` are kept for cost history; only
their link to deleted jobs is cleared.
"""

from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.jobs import PROJECT_KINDS
from app.models import (
    Citation,
    Conversation,
    Job,
    Message,
    Project,
    ProjectEntry,
    ProjectEvent,
    ProjectQuestion,
    Relation,
    Run,
    SummaryRevision,
)

#: Extra tables owned by a project, deleted before the project itself.
PROJECT_TABLES: list[type] = [Relation, ProjectEvent, SummaryRevision]


def purge_project(db: Session, owner_id: UUID, project_id: UUID) -> None:
    messages = select(Message.id).where(
        Message.owner_id == owner_id,
        Message.conversation_id.in_(
            select(Conversation.id).where(
                Conversation.owner_id == owner_id,
                Conversation.project_id == project_id,
            )
        ),
    )
    project_jobs = select(Job.id).where(
        Job.owner_id == owner_id,
        or_(
            Job.message_id.in_(messages),
            (Job.project_id == project_id) & Job.kind.in_(PROJECT_KINDS),
        ),
    )
    db.execute(update(Run).where(Run.job_id.in_(project_jobs)).values(job_id=None))
    db.execute(delete(Citation).where(Citation.message_id.in_(messages)))
    db.execute(delete(Job).where(Job.id.in_(project_jobs)))
    db.execute(delete(Message).where(Message.id.in_(messages)))
    db.execute(
        delete(Conversation).where(
            Conversation.owner_id == owner_id, Conversation.project_id == project_id
        )
    )
    for model in [*PROJECT_TABLES, ProjectEntry, ProjectQuestion]:
        db.execute(
            delete(model).where(
                model.owner_id == owner_id,  # type: ignore[attr-defined]
                model.project_id == project_id,  # type: ignore[attr-defined]
            )
        )
    db.execute(
        delete(Project).where(Project.owner_id == owner_id, Project.id == project_id)
    )
