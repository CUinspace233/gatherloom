"""Deletion lifecycle (product plan §7.4, ADR 0006).

Deleting an entry happens in two steps:

1. Right away, in the request (``mark_entry_deleted``): the entry leaves every
   list and search, its unfinished processing is cancelled, its chunks are
   removed and the deletion is written to the ledger and the deletion log.
2. Later, once any worker still holding a lease has lost it (``purge_due``,
   run by the scheduler): every copy of its content is removed. That covers
   revisions, attachment files, cards, relations, citation snippets, evidence
   kept in answer traces, job payloads and the sources of summaries, reviews
   and decisions, and the LangGraph checkpoints of the jobs that read it.
   Answers that cited it are masked. What the user wrote about it (decision
   text, questions) and the dates stay, with stale markers.

The deletion log (one JSON line per deletion) lives outside the database so
that, after restoring an older backup, ``replay`` deletes again everything the
user deleted since.
"""

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import cast, delete, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app import events, storage
from app.ingestion import delete_entry_chunks
from app.jobs import ACTIVE
from app.models import (
    Artifact,
    Attachment,
    Citation,
    Decision,
    Deletion,
    Entry,
    EntryRevision,
    Job,
    Message,
    Project,
    ProjectEntry,
    Relation,
    Review,
    SummaryRevision,
)

#: Longer than a job lease, so no worker can still write for a deleted entry.
PURGE_DELAY = timedelta(minutes=2)
BATCH = 50
MASKED_ANSWER = "（这条回答引用的资料已被删除，回答内容已隐藏。）"
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def log_path() -> Path:
    return Path(os.environ.get("GATHERLOOM_DELETION_LOG", "var/deletions.jsonl"))


def append_log(owner_id: UUID, kind: str, subject_id: UUID, at: datetime) -> None:
    """Written before the deletion commits: a logged deletion that then fails
    is replayed as a deletion the user asked for, never the reverse."""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "kind": kind,
            "owner_id": str(owner_id),
            "id": str(subject_id),
            "at": at.isoformat(),
        }
    )
    with open(path, "a", encoding="utf-8") as log:
        log.write(line + "\n")
        log.flush()
        os.fsync(log.fileno())


def record(
    db: Session,
    owner_id: UUID,
    kind: str,
    subject_id: UUID,
    now: datetime,
    *,
    purged: bool = False,
) -> None:
    db.execute(
        insert(Deletion)
        .values(
            owner_id=owner_id,
            kind=kind,
            subject_id=subject_id,
            requested_at=now,
            purged_at=now if purged else None,
        )
        .on_conflict_do_nothing(constraint="uq_deletions_subject")
    )
    append_log(owner_id, kind, subject_id, now)


def mark_entry_deleted(db: Session, entry: Entry, now: datetime) -> None:
    """Step 1: exclude the entry from everything now. The caller commits."""
    entry.deleted_at = now
    events.record_for_entry(db, entry.owner_id, entry.id, events.ENTRY_DELETED)
    # A worker holding a lease on a running job loses it here: its fenced
    # writes check the status and give up.
    db.execute(
        update(Job)
        .where(
            Job.owner_id == entry.owner_id,
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
    delete_entry_chunks(db, entry.owner_id, entry.id)
    record(db, entry.owner_id, "entry", entry.id, now)


# Step 2


@dataclass
class Purged:
    entries: int = 0
    files: list[tuple[UUID, UUID]] = field(default_factory=list)


def cites(column: Any, entry_id: UUID, key: str | None = None) -> ColumnElement[bool]:
    """JSONB containment: a list of evidence items with this entry_id."""
    target = column[key] if key else column
    ref = cast(literal(json.dumps([{"entry_id": str(entry_id)}])), JSONB)
    return target.op("@>")(ref)  # type: ignore[no-any-return]


def scrub(items: Iterable[dict[str, Any]], entry_id: UUID) -> list[dict[str, Any]]:
    """Evidence items with the deleted entry's text removed."""
    key = str(entry_id)
    return [
        {**item, "text": "", "snippet": ""} if item.get("entry_id") == key else item
        for item in items
    ]


def purge_entry(db: Session, owner_id: UUID, entry_id: UUID) -> list[UUID]:
    """Remove every copy of a deleted entry's content. Returns attachment ids
    whose files the caller removes after committing."""
    db.execute(
        update(Entry)
        .where(Entry.owner_id == owner_id, Entry.id == entry_id)
        .values(source_url=None)
    )
    db.execute(
        update(EntryRevision)
        .where(EntryRevision.owner_id == owner_id, EntryRevision.entry_id == entry_id)
        .values(text="", note="", client_ocr=None, client_extract=None)
    )
    files = list(
        db.scalars(
            delete(Attachment)
            .where(Attachment.owner_id == owner_id, Attachment.entry_id == entry_id)
            .returning(Attachment.id)
        )
    )
    delete_entry_chunks(db, owner_id, entry_id)
    for model in (Artifact, ProjectEntry):
        db.execute(
            delete(model).where(
                model.owner_id == owner_id,
                model.entry_id == entry_id,
            )
        )
    db.execute(
        delete(Relation).where(
            Relation.owner_id == owner_id,
            or_(Relation.entry_id == entry_id, Relation.target_entry_id == entry_id),
        )
    )

    # Answers: snippets go, answers that cited the entry are masked, and the
    # evidence kept in every trace that read it loses its text.
    cited = set(
        db.scalars(
            update(Citation)
            .where(Citation.owner_id == owner_id, Citation.entry_id == entry_id)
            .values(snippet="")
            .returning(Citation.message_id)
        )
    )
    traced = list(
        db.scalars(
            select(Message).where(
                Message.owner_id == owner_id,
                or_(
                    Message.id.in_(cited),
                    cites(Message.trace, entry_id, "evidence"),
                ),
            )
        )
    )
    for message in traced:
        if message.trace and "evidence" in message.trace:
            message.trace = {
                **message.trace,
                "evidence": scrub(message.trace["evidence"], entry_id),
            }
        if message.id in cited:
            message.content = MASKED_ANSWER

    # Summaries, reviews and decisions keep their text and stale markers.
    sourced: list[Any] = [SummaryRevision, Review, Decision]
    for table in sourced:
        for row in db.scalars(
            select(table).where(
                table.owner_id == owner_id, cites(table.sources, entry_id)
            )
        ):
            row.sources = scrub(row.sources, entry_id)

    jobs = list(
        db.scalars(
            select(Job).where(
                Job.owner_id == owner_id,
                or_(
                    Job.entry_id == entry_id,
                    Job.message_id.in_([m.id for m in traced]),
                    cites(Job.payload, entry_id, "evidence"),
                ),
            )
        )
    )
    for job in jobs:
        if job.payload and "evidence" in job.payload:
            job.payload = {
                **job.payload,
                "evidence": scrub(job.payload["evidence"], entry_id),
            }
    purge_checkpoints(db, [f"{owner_id}:{job.id}:" for job in jobs])
    return files


def purge_checkpoints(db: Session, prefixes: list[str]) -> None:
    """Delete LangGraph threads (``owner:job:generation``) of these jobs.

    Graph state holds only IDs (ADR 0006), but no run touching a deleted
    entry may be resumed. The tables exist once a worker has started.
    """
    if not prefixes:
        return
    for table in CHECKPOINT_TABLES:
        if db.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is None:
            continue
        db.execute(
            text(f"DELETE FROM {table} WHERE thread_id LIKE ANY(:patterns)"),
            {"patterns": [prefix + "%" for prefix in prefixes]},
        )


def purge_due(
    sessions: sessionmaker[Session], now: datetime, delay: timedelta = PURGE_DELAY
) -> Purged:
    """Purge deleted entries whose deletion is older than ``delay``."""
    result = Purged()
    while True:
        with sessions.begin() as db:
            due = list(
                db.scalars(
                    select(Deletion)
                    .where(
                        Deletion.kind == "entry",
                        Deletion.purged_at.is_(None),
                        Deletion.requested_at <= now - delay,
                    )
                    .order_by(Deletion.requested_at)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            files = []
            for deletion in due:
                for attachment_id in purge_entry(
                    db, deletion.owner_id, deletion.subject_id
                ):
                    files.append((deletion.owner_id, attachment_id))
                deletion.purged_at = now
            result.entries += len(due)
        for owner_id, attachment_id in files:
            storage.discard(owner_id, attachment_id)
        result.files += files
        if len(due) < BATCH:
            return result


def replay(sessions: sessionmaker[Session], path: Path, now: datetime) -> Purged:
    """After restoring a backup: delete again everything in the deletion log,
    then purge at once. Run it before starting the API, worker and scheduler."""
    from app.purge import purge_project

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for line in lines:
        if not line.strip():
            continue
        item = json.loads(line)
        owner_id, subject_id = UUID(item["owner_id"]), UUID(item["id"])
        at = datetime.fromisoformat(item["at"])
        with sessions.begin() as db:
            if item["kind"] == "project":
                if db.get(Project, subject_id) is not None:
                    purge_project(db, owner_id, subject_id)
                    _ledger(db, owner_id, "project", subject_id, at, purged=now)
                continue
            entry = db.scalar(
                select(Entry).where(Entry.owner_id == owner_id, Entry.id == subject_id)
            )
            if entry is None:
                continue
            _ledger(db, owner_id, "entry", subject_id, at, purged=None)
            if entry.deleted_at is None:
                # The backup predates the deletion: delete and purge again.
                entry.deleted_at = at
                delete_entry_chunks(db, owner_id, subject_id)
                db.execute(
                    update(Job)
                    .where(
                        Job.owner_id == owner_id,
                        Job.entry_id == subject_id,
                        Job.status.in_(ACTIVE),
                    )
                    .values(status="cancelled", finished_at=now, updated_at=now)
                )
                db.execute(
                    update(Deletion)
                    .where(Deletion.kind == "entry", Deletion.subject_id == subject_id)
                    .values(purged_at=None)
                )
    return purge_due(sessions, now, delay=timedelta(0))


def _ledger(
    db: Session,
    owner_id: UUID,
    kind: str,
    subject_id: UUID,
    at: datetime,
    purged: datetime | None,
) -> None:
    db.execute(
        insert(Deletion)
        .values(
            owner_id=owner_id,
            kind=kind,
            subject_id=subject_id,
            requested_at=at,
            purged_at=purged,
        )
        .on_conflict_do_nothing(constraint="uq_deletions_subject")
    )


def deleted_among(
    db: Session, owner_id: UUID, items: Iterable[dict[str, Any]]
) -> set[str]:
    ids = {str(item["entry_id"]) for item in items}
    if not ids:
        return set()
    return {
        str(entry_id)
        for entry_id in db.scalars(
            select(Entry.id).where(
                Entry.owner_id == owner_id,
                Entry.id.in_([UUID(i) for i in ids]),
                Entry.deleted_at.is_not(None),
            )
        )
    }


def without_deleted(
    db: Session, owner_id: UUID, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """For results published after an entry was deleted mid-run: its text
    must not be written back after (or while) it is purged."""
    gone = deleted_among(db, owner_id, items)
    return [
        {**item, "text": "", "snippet": ""} if str(item["entry_id"]) in gone else item
        for item in items
    ]
