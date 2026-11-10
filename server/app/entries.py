from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import Select, and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth import AUTH_ERROR, Db, Owner
from app.contracts import (
    AttachmentView,
    EntryCreate,
    EntryDetail,
    EntryUpdate,
    EntryView,
    ErrorDetail,
    ErrorResponses,
    Page,
    ProcessingView,
    RevisionView,
)
from app.ingestion import delete_entry_chunks
from app.jobs import ACTIVE, INGESTION, failed_step, processing_status
from app.models import Attachment, Entry, EntryRevision, Job, ProjectEntry
from app.projects import NOT_FOUND, Cursor, Limit, get_project, paginate

router = APIRouter(prefix="/v1", tags=["entries"], responses=AUTH_ERROR)
CONFLICT: ErrorResponses = {
    409: {
        "model": ErrorDetail,
        "description": "The request conflicts with current state.",
    }
}


def live_entries(owner_id: UUID) -> Select[Entry]:
    return select(Entry).where(Entry.owner_id == owner_id, Entry.deleted_at.is_(None))


def get_entry(
    db: Session, owner_id: UUID, entry_id: UUID, *, lock: bool = False
) -> Entry:
    query = live_entries(owner_id).where(Entry.id == entry_id)
    if lock:
        query = query.with_for_update()
    entry = db.scalar(query)
    if entry is None:
        raise HTTPException(404, "entry not found")
    return entry


def normalized_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def views(
    db: Session, owner_id: UUID, entries: list[Entry], *, detail: bool = False
) -> list[EntryView]:
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
    jobs = {
        entry_id: processing_status(status, indexed_at)
        for entry_id, status, indexed_at in db.execute(
            select(Job.entry_id, Job.status, Job.indexed_at)
            .join(
                Entry,
                and_(
                    Entry.owner_id == Job.owner_id,
                    Entry.id == Job.entry_id,
                    Entry.current_revision == Job.revision,
                ),
            )
            .where(
                Job.owner_id == owner_id, Job.entry_id.in_(ids), Job.kind == INGESTION
            )
        )
    }
    attachments = {
        attachment.entry_id: attachment
        for attachment in db.scalars(
            select(Attachment).where(
                Attachment.owner_id == owner_id, Attachment.entry_id.in_(ids)
            )
        )
    }
    projects: dict[UUID, list[UUID]] = defaultdict(list)
    for entry_id, project_id in db.execute(
        select(ProjectEntry.entry_id, ProjectEntry.project_id)
        .where(ProjectEntry.owner_id == owner_id, ProjectEntry.entry_id.in_(ids))
        .order_by(ProjectEntry.project_id)
    ):
        projects[entry_id].append(project_id)
    results = []
    for entry in entries:
        revision = revisions[entry.id]
        extract = revision.client_extract or {}
        attachment = attachments.get(entry.id)
        fields: dict[str, Any] = {
            "id": entry.id,
            "client_submission_id": entry.client_submission_id,
            "type": entry.type,
            "revision": revision.revision,
            "text": revision.text,
            "note": revision.note,
            "source_url": entry.source_url,
            "title": extract.get("title") or None,
            "attachment": attachment and AttachmentView.model_validate(attachment),
            "project_ids": projects[entry.id],
            "possible_duplicate": entry.possible_duplicate,
            "processing": jobs.get(entry.id, "queued"),
            "created_at": entry.created_at,
            "updated_at": revision.created_at,
        }
        if detail:
            fields["ocr_text"] = (revision.client_ocr or {}).get("text")
            fields["extract_text"] = extract.get("text") or None
        results.append(EntryDetail(**fields) if detail else EntryView(**fields))
    return results


def flag_duplicate(db: Session, owner_id: UUID, entry: Entry, fingerprint: str) -> None:
    # Serialize the duplicate check for identical content in this account.
    lock = sha256(owner_id.bytes + bytes.fromhex(fingerprint)).digest()[:8]
    db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(lock, signed=True))))
    entry.possible_duplicate = (
        db.scalar(
            select(EntryRevision.entry_id)
            .join(
                Entry,
                and_(
                    Entry.owner_id == EntryRevision.owner_id,
                    Entry.id == EntryRevision.entry_id,
                ),
            )
            .where(
                EntryRevision.owner_id == owner_id,
                EntryRevision.content_fingerprint == fingerprint,
                Entry.id != entry.id,
                Entry.deleted_at.is_(None),
            )
            .limit(1)
        )
        is not None
    )


@router.post(
    "/entries",
    status_code=201,
    responses={
        200: {
            "model": EntryView,
            "description": "Existing entry for this submission ID.",
        },
        **NOT_FOUND,
        **CONFLICT,
    },
)
def create_entry(
    body: EntryCreate, owner: Owner, db: Db, response: Response
) -> EntryView:
    """Save one thought, link or image.

    Reusing a submission ID returns the original entry for this account. Image
    entries reference an attachment uploaded with the same submission ID.
    """
    entry = db.scalar(
        insert(Entry)
        .values(
            owner_id=owner.id,
            client_submission_id=body.client_submission_id,
            type=body.type,
            source_url=body.url,
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
        if existing.deleted_at is not None:
            raise HTTPException(409, "entry for this submission was deleted")
        response.status_code = 200
        return views(db, owner.id, [existing])[0]

    if body.project_id is not None:
        get_project(db, owner.id, body.project_id)
        db.add(
            ProjectEntry(
                owner_id=owner.id, project_id=body.project_id, entry_id=entry.id
            )
        )

    if body.attachment_id is not None:
        attachment = db.scalar(
            select(Attachment)
            .where(
                Attachment.owner_id == owner.id,
                Attachment.id == body.attachment_id,
                Attachment.client_submission_id == body.client_submission_id,
            )
            .with_for_update()
        )
        if attachment is None:
            raise HTTPException(404, "attachment not found")
        if attachment.entry_id is not None:
            raise HTTPException(409, "attachment already belongs to an entry")
        attachment.entry_id = entry.id
        fingerprint = attachment.sha256
    elif body.url is not None:
        fingerprint = sha256(normalized_url(body.url).encode()).hexdigest()
    else:
        fingerprint = sha256(body.text.encode()).hexdigest()

    flag_duplicate(db, owner.id, entry, fingerprint)
    db.add(
        EntryRevision(
            owner_id=owner.id,
            entry_id=entry.id,
            revision=1,
            text=body.text,
            note=body.note,
            content_fingerprint=fingerprint,
            client_ocr=body.client_ocr and body.client_ocr.model_dump(mode="json"),
            client_extract=body.client_extract
            and body.client_extract.model_dump(mode="json"),
        )
    )
    db.flush()
    db.add(Job(owner_id=owner.id, entry_id=entry.id, revision=1, kind=INGESTION))
    db.flush()
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
    query = live_entries(owner.id)
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
def entry_detail(entry_id: UUID, owner: Owner, db: Db) -> EntryDetail:
    entry = get_entry(db, owner.id, entry_id)
    result = views(db, owner.id, [entry], detail=True)[0]
    assert isinstance(result, EntryDetail)
    return result


@router.patch("/entries/{entry_id}", responses={**NOT_FOUND, **CONFLICT})
def update_entry(entry_id: UUID, body: EntryUpdate, owner: Owner, db: Db) -> EntryView:
    """Edit the text or note. Each edit creates a new revision and processing job.

    base_revision must equal the entry's current revision; otherwise 409.
    """
    entry = get_entry(db, owner.id, entry_id, lock=True)
    if entry.current_revision != body.base_revision:
        raise HTTPException(409, "entry changed since base_revision")
    current = db.get_one(EntryRevision, (owner.id, entry.id, entry.current_revision))
    text = current.text if body.text is None else body.text
    if entry.type == "thought" and not text:
        raise HTTPException(422, "thought entries need text")
    fingerprint = current.content_fingerprint
    if entry.type == "thought" and text != current.text:
        fingerprint = sha256(text.encode()).hexdigest()
        flag_duplicate(db, owner.id, entry, fingerprint)
    revision = entry.current_revision + 1
    db.add(
        EntryRevision(
            owner_id=owner.id,
            entry_id=entry.id,
            revision=revision,
            text=text,
            note=current.note if body.note is None else body.note,
            content_fingerprint=fingerprint,
            client_ocr=current.client_ocr,
            client_extract=current.client_extract,
        )
    )
    entry.current_revision = revision
    db.flush()
    db.add(Job(owner_id=owner.id, entry_id=entry.id, revision=revision, kind=INGESTION))
    db.flush()
    result = views(db, owner.id, [entry])[0]
    db.commit()
    return result


@router.delete("/entries/{entry_id}", status_code=204, responses=NOT_FOUND)
def delete_entry(entry_id: UUID, owner: Owner, db: Db) -> Response:
    """Remove an entry from every list and cancel its pending processing.

    Stored files and derived data are purged by the deletion lifecycle (S6).
    """
    entry = get_entry(db, owner.id, entry_id, lock=True)
    now = datetime.now(UTC)
    entry.deleted_at = now
    # A worker holding a lease on a running job loses it here: its fenced
    # writes check the status and give up.
    db.execute(
        update(Job)
        .where(
            Job.owner_id == owner.id,
            Job.entry_id == entry.id,
            Job.status.in_(ACTIVE),
        )
        .values(
            status="cancelled",
            lease_owner=None,
            lease_expires_at=None,
            finished_at=now,
            updated_at=now,
        )
    )
    delete_entry_chunks(db, owner.id, entry.id)
    db.commit()
    return Response(status_code=204)


def current_job(db: Session, entry: Entry, *, lock: bool = False) -> Job:
    query = select(Job).where(
        Job.owner_id == entry.owner_id,
        Job.entry_id == entry.id,
        Job.revision == entry.current_revision,
        Job.kind == INGESTION,
    )
    return db.scalars(query.with_for_update() if lock else query).one()


def processing_view(job: Job) -> ProcessingView:
    status = processing_status(job.status, job.indexed_at)
    return ProcessingView(
        status=status,
        searchable=job.indexed_at is not None,
        stage=job.stage,
        failed_step=failed_step(job.error_step) if job.error_code else None,
        error_code=job.error_code,
        attempts=job.attempts,
        can_retry=job.status == "failed",
        updated_at=job.updated_at,
    )


@router.get("/entries/{entry_id}/processing", responses=NOT_FOUND)
def entry_processing(entry_id: UUID, owner: Owner, db: Db) -> ProcessingView:
    """Processing state of the entry's current revision."""
    entry = get_entry(db, owner.id, entry_id)
    return processing_view(current_job(db, entry))


@router.post("/entries/{entry_id}/retry", responses={**NOT_FOUND, **CONFLICT})
def retry_processing(entry_id: UUID, owner: Owner, db: Db) -> ProcessingView:
    """Run failed processing again. Steps that already finished are kept.

    A job that is still queued or running is returned unchanged; 409 when the
    current revision has already finished processing.
    """
    entry = get_entry(db, owner.id, entry_id, lock=True)
    job = current_job(db, entry, lock=True)
    if job.status == "failed":
        now = datetime.now(UTC)
        job.status = "queued"
        job.attempts = 0
        job.generation += 1
        job.run_after = now
        job.updated_at = now
        job.finished_at = None
        job.error_code = None
        job.error_step = None
    elif job.status not in ACTIVE:
        raise HTTPException(409, "processing already finished")
    result = processing_view(job)
    db.commit()
    return result


@router.get("/entries/{entry_id}/revisions", responses=NOT_FOUND)
def list_revisions(entry_id: UUID, owner: Owner, db: Db) -> list[RevisionView]:
    """List every revision of an entry, newest first."""
    get_entry(db, owner.id, entry_id)
    return [
        RevisionView.model_validate(revision)
        for revision in db.scalars(
            select(EntryRevision)
            .where(
                EntryRevision.owner_id == owner.id, EntryRevision.entry_id == entry_id
            )
            .order_by(EntryRevision.revision.desc())
        )
    ]


@router.put(
    "/projects/{project_id}/entries/{entry_id}", status_code=204, responses=NOT_FOUND
)
def add_to_project(project_id: UUID, entry_id: UUID, owner: Owner, db: Db) -> Response:
    """Add an entry to a project. Adding it again has no effect."""
    get_project(db, owner.id, project_id)
    get_entry(db, owner.id, entry_id)
    db.execute(
        insert(ProjectEntry)
        .values(owner_id=owner.id, project_id=project_id, entry_id=entry_id)
        .on_conflict_do_nothing()
    )
    db.commit()
    return Response(status_code=204)


@router.delete(
    "/projects/{project_id}/entries/{entry_id}", status_code=204, responses=NOT_FOUND
)
def remove_from_project(
    project_id: UUID, entry_id: UUID, owner: Owner, db: Db
) -> Response:
    """Remove an entry from a project. The entry itself is kept."""
    get_project(db, owner.id, project_id)
    get_entry(db, owner.id, entry_id)
    db.execute(
        delete(ProjectEntry).where(
            ProjectEntry.owner_id == owner.id,
            ProjectEntry.project_id == project_id,
            ProjectEntry.entry_id == entry_id,
        )
    )
    db.commit()
    return Response(status_code=204)
