from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import Select, select, tuple_, update
from sqlalchemy.orm import Session

from app.auth import AUTH_ERROR, Db, Owner
from app.contracts import (
    ErrorDetail,
    ErrorResponses,
    Page,
    ProjectCreate,
    ProjectStatus,
    ProjectUpdate,
    ProjectView,
    QuestionCreate,
    QuestionUpdate,
    QuestionView,
)
from app.models import Entry, Project, ProjectQuestion

router = APIRouter(prefix="/v1", tags=["projects"], responses=AUTH_ERROR)
NOT_FOUND: ErrorResponses = {
    404: {"model": ErrorDetail, "description": "Resource not found for this account."}
}
BAD_CURSOR: ErrorResponses = {
    400: {"model": ErrorDetail, "description": "Invalid pagination cursor."}
}
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[
    str | None,
    Query(max_length=128, description="Use the preceding page's next_cursor."),
]


def owned_projects(owner_id: UUID) -> Select[Project]:
    return select(Project).where(Project.owner_id == owner_id)


def owned_questions(owner_id: UUID) -> Select[ProjectQuestion]:
    return select(ProjectQuestion).where(ProjectQuestion.owner_id == owner_id)


def get_project(db: Session, owner_id: UUID, project_id: UUID) -> Project:
    project = db.scalar(owned_projects(owner_id).where(Project.id == project_id))
    if project is None:
        raise HTTPException(404, "project not found")
    return project


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        date, id_text = (
            urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode().split("|")
        )
        created_at = datetime.fromisoformat(date)
        if created_at.tzinfo is None:
            raise ValueError
        return created_at, UUID(id_text)
    except (ValueError, Base64Error, OverflowError):
        raise HTTPException(400, "invalid pagination cursor") from None


def paginate[T: (Project, ProjectQuestion, Entry)](
    db: Session,
    query: Select[T],
    model: type[T],
    limit: int,
    cursor: str | None,
) -> tuple[list[T], str | None]:
    if cursor is not None:
        query = query.where(tuple_(model.created_at, model.id) < decode_cursor(cursor))
    rows = list(
        db.scalars(
            query.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
        )
    )
    if len(rows) <= limit:
        return rows, None
    last = rows[limit - 1]
    token = (
        urlsafe_b64encode(f"{last.created_at.isoformat()}|{last.id}".encode())
        .decode()
        .rstrip("=")
    )
    return rows[:limit], token


@router.get("/projects", responses=BAD_CURSOR)
def list_projects(
    owner: Owner,
    db: Db,
    limit: Limit = 25,
    cursor: Cursor = None,
    status: ProjectStatus | None = None,
) -> Page[ProjectView]:
    """List the current account's projects, newest first."""
    query = owned_projects(owner.id)
    if status is not None:
        query = query.where(Project.status == status)
    rows, next_cursor = paginate(db, query, Project, limit, cursor)
    return Page(
        items=[ProjectView.model_validate(row) for row in rows], next_cursor=next_cursor
    )


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, owner: Owner, db: Db) -> ProjectView:
    project = Project(owner_id=owner.id, name=body.name, goal=body.goal)
    db.add(project)
    db.commit()
    return ProjectView.model_validate(project)


@router.get("/projects/{project_id}", responses=NOT_FOUND)
def project_detail(project_id: UUID, owner: Owner, db: Db) -> ProjectView:
    return ProjectView.model_validate(get_project(db, owner.id, project_id))


@router.patch("/projects/{project_id}", responses=NOT_FOUND)
def update_project(
    project_id: UUID, body: ProjectUpdate, owner: Owner, db: Db
) -> ProjectView:
    project = db.scalar(
        update(Project)
        .where(Project.owner_id == owner.id, Project.id == project_id)
        .values(**body.model_dump(exclude_unset=True))
        .returning(Project)
    )
    if project is None:
        raise HTTPException(404, "project not found")
    db.commit()
    return ProjectView.model_validate(project)


@router.get("/projects/{project_id}/questions", responses={**NOT_FOUND, **BAD_CURSOR})
def list_questions(
    project_id: UUID, owner: Owner, db: Db, limit: Limit = 25, cursor: Cursor = None
) -> Page[QuestionView]:
    get_project(db, owner.id, project_id)
    query = owned_questions(owner.id).where(ProjectQuestion.project_id == project_id)
    rows, next_cursor = paginate(db, query, ProjectQuestion, limit, cursor)
    return Page(
        items=[QuestionView.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.post("/projects/{project_id}/questions", status_code=201, responses=NOT_FOUND)
def create_question(
    project_id: UUID, body: QuestionCreate, owner: Owner, db: Db
) -> QuestionView:
    get_project(db, owner.id, project_id)
    question = ProjectQuestion(owner_id=owner.id, project_id=project_id, text=body.text)
    db.add(question)
    db.commit()
    return QuestionView.model_validate(question)


@router.patch("/questions/{question_id}", responses=NOT_FOUND)
def update_question(
    question_id: UUID, body: QuestionUpdate, owner: Owner, db: Db
) -> QuestionView:
    question = db.scalar(
        update(ProjectQuestion)
        .where(ProjectQuestion.owner_id == owner.id, ProjectQuestion.id == question_id)
        .values(**body.model_dump(exclude_unset=True))
        .returning(ProjectQuestion)
    )
    if question is None:
        raise HTTPException(404, "question not found")
    db.commit()
    return QuestionView.model_validate(question)
