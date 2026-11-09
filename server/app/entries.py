from collections import defaultdict
from hashlib import sha256
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth import AUTH_ERROR, Db, Owner
from app.contracts import EntryCreate, EntryView, ErrorDetail, Page
from app.models import Entry, EntryRevision, Job, ProjectEntry
from app.projects import NOT_FOUND, Cursor, Limit, get_project, paginate

router = APIRouter(prefix="/v1", tags=["entries"], responses=AUTH_ERROR)


def views(db: Session, owner_id: UUID, entries: list[Entry]) -> list[EntryView]:
    if not entries:
        return []
    ids = [entry.id for entry in entries]
    revisions = {
        revision.entry_id: revision
        for revision in db.scalars(
            select(EntryRevision)
            .join(
                Entry,
                and_(
                    Entry.owner_id == EntryRevision.owner_id,
                    Entry.id == EntryRevision.entry_id,
                    Entry.current_revision == EntryRevision.revision,
                ),
            )
            .where(EntryRevision.owner_id == owner_id, EntryRevision.entry_id.in_(ids))
        )
    }
    projects: dict[UUID, list[UUID]] = defaultdict(list)
    for entry_id, project_id in db.execute(
        select(ProjectEntry.entry_id, ProjectEntry.project_id)
        .where(ProjectEntry.owner_id == owner_id, ProjectEntry.entry_id.in_(ids))
        .order_by(ProjectEntry.project_id)
    ):
        projects[entry_id].append(project_id)
    return [
        EntryView(
            id=entry.id,
            client_submission_id=entry.client_submission_id,
            type=entry.type,
            text=revisions[entry.id].text,
            note=revisions[entry.id].note,
            project_ids=projects[entry.id],
            possible_duplicate=entry.possible_duplicate,
            created_at=entry.created_at,
        )
        for entry in entries
    ]


@router.post(
    "/entries",
    status_code=201,
    responses={
        200: {
            "model": EntryView,
            "description": "Existing entry for this submission ID.",
        },
        **NOT_FOUND,
    },
)
def create_entry(
    body: EntryCreate, owner: Owner, db: Db, response: Response
) -> EntryView:
    """Save one text entry. Reusing a submission ID returns the original entry for this account."""
    entry = db.scalar(
        insert(Entry)
        .values(
            owner_id=owner.id,
            client_submission_id=body.client_submission_id,
            type=body.type,
        )
        .on_conflict_do_nothing(constraint="uq_entries_submission")
        .returning(Entry)
    )
    if entry is None:
        existing = db.scalars(
            select(Entry).where(
                Entry.owner_id == owner.id,
                Entry.client_submission_id == body.client_submission_id,
            )
        ).one()
        response.status_code = 200
        return views(db, owner.id, [existing])[0]

    if body.project_id is not None:
        get_project(db, owner.id, body.project_id)
        db.add(
            ProjectEntry(
                owner_id=owner.id, project_id=body.project_id, entry_id=entry.id
            )
        )

    fingerprint = sha256(body.text.encode()).hexdigest()
    # Serialize the duplicate check for identical text in this account.
    lock = sha256(owner.id.bytes + bytes.fromhex(fingerprint)).digest()[:8]
    db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(lock, signed=True))))
    entry.possible_duplicate = (
        db.scalar(
            select(EntryRevision.entry_id)
            .where(
                EntryRevision.owner_id == owner.id,
                EntryRevision.content_fingerprint == fingerprint,
            )
            .limit(1)
        )
        is not None
    )
    db.add(
        EntryRevision(
            owner_id=owner.id,
            entry_id=entry.id,
            revision=1,
            text=body.text,
            note=body.note,
            content_fingerprint=fingerprint,
        )
    )
    db.flush()
    db.add(
        Job(owner_id=owner.id, entry_id=entry.id, revision=1, kind="entry_ingestion")
    )
    result = views(db, owner.id, [entry])[0]
    db.commit()
    return result


@router.get(
    "/entries",
    responses={
        **NOT_FOUND,
        400: {
            "model": ErrorDetail,
            "description": "Invalid cursor or incompatible filters.",
        },
    },
)
def list_entries(
    owner: Owner,
    db: Db,
    limit: Limit = 25,
    cursor: Cursor = None,
    inbox: bool = False,
    project_id: UUID | None = None,
) -> Page[EntryView]:
    """List entries for the current account, optionally scoped to the inbox or one project."""
    query = select(Entry).where(Entry.owner_id == owner.id)
    links = select(ProjectEntry.entry_id).where(ProjectEntry.owner_id == owner.id)
    if project_id is not None:
        if inbox:
            raise HTTPException(400, "inbox and project_id are mutually exclusive")
        get_project(db, owner.id, project_id)
        query = query.where(
            Entry.id.in_(links.where(ProjectEntry.project_id == project_id))
        )
    elif inbox:
        query = query.where(Entry.id.not_in(links))
    entries, next_cursor = paginate(db, query, Entry, limit, cursor)
    return Page(items=views(db, owner.id, entries), next_cursor=next_cursor)


@router.get("/entries/{entry_id}", responses=NOT_FOUND)
def entry_detail(entry_id: UUID, owner: Owner, db: Db) -> EntryView:
    entry = db.scalar(
        select(Entry).where(Entry.owner_id == owner.id, Entry.id == entry_id)
    )
    if entry is None:
        raise HTTPException(404, "entry not found")
    return views(db, owner.id, [entry])[0]
