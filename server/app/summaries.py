"""Project summaries: the current version and its history."""

from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs
from app.auth import AUTH_ERROR, Db, Owner
from app.contracts import (
    ProjectSummaries,
    SummaryCoverage,
    SummaryListItem,
    SummaryPoint,
    SummaryView,
)
from app.models import Job, ProjectEvent, SummaryRevision
from app.projects import NOT_FOUND, get_project
from app.sources import SourceRef, citation_views

router = APIRouter(prefix="/v1", tags=["summaries"], responses=AUTH_ERROR)
HISTORY = 50


def summary_view(db: Session, owner_id: UUID, revision: SummaryRevision) -> SummaryView:
    based_on = db.scalar(
        select(ProjectEvent.created_at).where(ProjectEvent.seq == revision.watermark)
    )
    content = revision.content
    return SummaryView(
        version=revision.version,
        understanding=content["understanding"],
        key_points=[SummaryPoint(**point) for point in content["key_points"]],
        uncertainties=content["uncertainties"],
        change_note=revision.change_note,
        sources=citation_views(
            db,
            owner_id,
            revision.project_id,
            [SourceRef.from_evidence(item) for item in revision.sources],
        ),
        coverage=SummaryCoverage(**revision.coverage),
        based_on=based_on,
        model=revision.model,
        created_at=revision.created_at,
    )


@router.get("/projects/{project_id}/summaries", responses=NOT_FOUND)
def project_summaries(project_id: UUID, owner: Owner, db: Db) -> ProjectSummaries:
    """The current AI summary, recent versions, and whether an update is pending."""
    get_project(db, owner.id, project_id)
    revisions = list(
        db.scalars(
            select(SummaryRevision)
            .where(
                SummaryRevision.owner_id == owner.id,
                SummaryRevision.project_id == project_id,
            )
            .order_by(SummaryRevision.version.desc())
            .limit(HISTORY)
        )
    )
    updating = (
        db.scalar(
            select(Job.id)
            .where(
                Job.owner_id == owner.id,
                Job.project_id == project_id,
                Job.kind == jobs.MAINTENANCE,
                Job.status.in_(jobs.ACTIVE),
            )
            .limit(1)
        )
        is not None
    )
    return ProjectSummaries(
        current=summary_view(db, owner.id, revisions[0]) if revisions else None,
        history=[
            SummaryListItem(
                version=r.version, change_note=r.change_note, created_at=r.created_at
            )
            for r in revisions
        ],
        updating=updating,
    )


@router.get("/projects/{project_id}/summaries/{version}", responses=NOT_FOUND)
def summary_version(
    project_id: UUID, version: int, owner: Owner, db: Db
) -> SummaryView:
    get_project(db, owner.id, project_id)
    revision = db.scalar(
        select(SummaryRevision).where(
            SummaryRevision.owner_id == owner.id,
            SummaryRevision.project_id == project_id,
            SummaryRevision.version == version,
        )
    )
    if revision is None:
        raise HTTPException(404, "summary not found")
    return summary_view(db, owner.id, revision)
