"""Project conversations: questions answered from the project's material.

Posting a question returns 202 with a run id; the worker answers it
(app.answering) and the client polls GET /v1/runs/{id}.
"""

from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth import AUTH_ERROR, Db, Owner
from app.contracts import (
    AnswerAccepted,
    CitationView,
    ConversationCreate,
    ConversationView,
    MessageCreate,
    MessageView,
    Page,
    RunView,
)
from app.entries import CONFLICT, views
from app.jobs import ACTIVE, ANSWER
from app.models import Citation, Conversation, Entry, Job, Message, ProjectEntry
from app.projects import BAD_CURSOR, NOT_FOUND, Cursor, Limit, get_project, paginate

router = APIRouter(prefix="/v1", tags=["conversations"], responses=AUTH_ERROR)
TITLE_CHARS = 40


def get_conversation(
    db: Session, owner_id: UUID, conversation_id: UUID
) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.owner_id == owner_id, Conversation.id == conversation_id
        )
    )
    if conversation is None:
        raise HTTPException(404, "conversation not found")
    return conversation


def get_run(db: Session, owner_id: UUID, run_id: UUID, *, lock: bool = False) -> Job:
    query = select(Job).where(
        Job.owner_id == owner_id, Job.id == run_id, Job.kind == ANSWER
    )
    job = db.scalar(query.with_for_update() if lock else query)
    if job is None:
        raise HTTPException(404, "run not found")
    return job


def run_view(job: Job, answer_id: UUID | None) -> RunView:
    return RunView(
        id=job.id,
        status=job.status,  # type: ignore[arg-type]
        stage=job.stage,
        error_code=job.error_code,
        attempts=job.attempts,
        can_retry=job.status == "failed",
        answer_message_id=answer_id,
        updated_at=job.updated_at,
    )


def answer_ids(db: Session, owner_id: UUID, questions: list[UUID]) -> dict[UUID, UUID]:
    return {
        reply_to: answer_id
        for answer_id, reply_to in db.execute(
            select(Message.id, Message.reply_to).where(
                Message.owner_id == owner_id, Message.reply_to.in_(questions)
            )
        )
        if reply_to is not None
    }


def message_views(
    db: Session, owner_id: UUID, conversation: Conversation, messages: list[Message]
) -> list[MessageView]:
    ids = [message.id for message in messages]
    runs = {
        job.message_id: job
        for job in db.scalars(
            select(Job).where(Job.owner_id == owner_id, Job.message_id.in_(ids))
        )
    }
    answers = answer_ids(db, owner_id, ids)
    citations: dict[UUID, list[Citation]] = defaultdict(list)
    for citation in db.scalars(
        select(Citation)
        .where(Citation.owner_id == owner_id, Citation.message_id.in_(ids))
        .order_by(Citation.n)
    ):
        citations[citation.message_id].append(citation)
    cited = {c.entry_id for group in citations.values() for c in group}
    entries = {
        entry.id: entry
        for entry in db.scalars(
            select(Entry).where(Entry.owner_id == owner_id, Entry.id.in_(cited))
        )
    }
    in_project = set(
        db.scalars(
            select(ProjectEntry.entry_id).where(
                ProjectEntry.owner_id == owner_id,
                ProjectEntry.project_id == conversation.project_id,
                ProjectEntry.entry_id.in_(cited),
            )
        )
    )
    summaries = {view.id: view for view in views(db, owner_id, list(entries.values()))}

    def citation_view(citation: Citation) -> CitationView:
        entry = entries[citation.entry_id]
        summary = summaries[entry.id]
        title = summary.title or summary.text[:TITLE_CHARS] or summary.source_url or ""
        return CitationView(
            n=citation.n,
            entry_id=citation.entry_id,
            revision=citation.revision,
            chunk_id=citation.chunk_id,
            title=title,
            type=summary.type,
            created_at=entry.created_at,
            source=citation.source,
            snippet=citation.snippet,
            start_offset=citation.start_offset,
            end_offset=citation.end_offset,
            stale=entry.deleted_at is not None
            or entry.current_revision != citation.revision
            or entry.id not in in_project,
        )

    return [
        MessageView(
            id=message.id,
            role=message.role,  # type: ignore[arg-type]
            content=message.content,
            insufficient=message.insufficient,
            reply_to=message.reply_to,
            run=run_view(job, answers.get(message.id))
            if (job := runs.get(message.id)) is not None
            else None,
            citations=[citation_view(c) for c in citations[message.id]],
            created_at=message.created_at,
        )
        for message in messages
    ]


@router.post(
    "/projects/{project_id}/conversations", status_code=201, responses=NOT_FOUND
)
def create_conversation(
    project_id: UUID, body: ConversationCreate, owner: Owner, db: Db
) -> ConversationView:
    """Start a conversation in a project. The first question names it if untitled."""
    get_project(db, owner.id, project_id)
    conversation = Conversation(
        owner_id=owner.id, project_id=project_id, title=body.title
    )
    db.add(conversation)
    db.flush()
    db.refresh(conversation)
    result = ConversationView.model_validate(conversation)
    db.commit()
    return result


@router.get(
    "/projects/{project_id}/conversations", responses={**NOT_FOUND, **BAD_CURSOR}
)
def list_conversations(
    project_id: UUID, owner: Owner, db: Db, limit: Limit = 25, cursor: Cursor = None
) -> Page[ConversationView]:
    """The project's conversations, newest first."""
    get_project(db, owner.id, project_id)
    query = select(Conversation).where(
        Conversation.owner_id == owner.id, Conversation.project_id == project_id
    )
    rows, next_cursor = paginate(db, query, Conversation, limit, cursor)
    return Page(
        items=[ConversationView.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/conversations/{conversation_id}/messages", responses=NOT_FOUND)
def list_messages(conversation_id: UUID, owner: Owner, db: Db) -> list[MessageView]:
    """Every question and answer in order, with citations and answer runs."""
    conversation = get_conversation(db, owner.id, conversation_id)
    messages = list(
        db.scalars(
            select(Message)
            .where(
                Message.owner_id == owner.id,
                Message.conversation_id == conversation.id,
            )
            .order_by(Message.created_at, Message.role.desc())
        )
    )
    return message_views(db, owner.id, conversation, messages)


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=202,
    responses={**NOT_FOUND, **CONFLICT},
)
def ask(
    conversation_id: UUID, body: MessageCreate, owner: Owner, db: Db
) -> AnswerAccepted:
    """Ask a question. The answer is produced in the background.

    Reusing client_message_id returns the original question and run.
    """
    conversation = get_conversation(db, owner.id, conversation_id)
    message_id = db.scalar(
        insert(Message)
        .values(
            owner_id=owner.id,
            conversation_id=conversation.id,
            role="user",
            content=body.text,
            client_message_id=body.client_message_id,
        )
        .on_conflict_do_nothing(constraint="uq_messages_client_message")
        .returning(Message.id)
    )
    if message_id is None:
        existing = db.scalars(
            select(Message).where(
                Message.owner_id == owner.id,
                Message.client_message_id == body.client_message_id,
            )
        ).one()
        if existing.conversation_id != conversation.id:
            raise HTTPException(409, "message id used in another conversation")
    else:
        db.add(Job(owner_id=owner.id, kind=ANSWER, message_id=message_id))
        if not conversation.title:
            conversation.title = body.text[:TITLE_CHARS]
        conversation.updated_at = datetime.now(UTC)
        db.flush()
        existing = db.get_one(Message, message_id)
    [view] = message_views(db, owner.id, conversation, [existing])
    db.commit()
    assert view.run is not None
    return AnswerAccepted(message=view, run_id=view.run.id)


@router.get("/runs/{run_id}", responses=NOT_FOUND)
def run_status(run_id: UUID, owner: Owner, db: Db) -> RunView:
    """State of a question's answer run."""
    job = get_run(db, owner.id, run_id)
    assert job.message_id is not None
    return run_view(job, answer_ids(db, owner.id, [job.message_id]).get(job.message_id))


@router.post("/runs/{run_id}/retry", responses={**NOT_FOUND, **CONFLICT})
def retry_run(run_id: UUID, owner: Owner, db: Db) -> RunView:
    """Run a failed answer again. Queued or running runs are returned as is."""
    job = get_run(db, owner.id, run_id)
    db.scalar(select(Message.id).where(Message.id == job.message_id).with_for_update())
    job = get_run(db, owner.id, run_id, lock=True)
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
        raise HTTPException(409, "run already finished")
    result = run_view(job, None)
    db.commit()
    return result
