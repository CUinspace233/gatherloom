from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Bailian qwen3.7-text-embedding-flash default output; index_version 1 (ADR 0001).
EMBEDDING_DIMENSION = 1024
INDEX_VERSION = 1


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), server_default="Asia/Shanghai")


class InviteCode(Base):
    __tablename__ = "invite_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class LoginSession(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    access_hash: Mapped[str] = mapped_column(String(64), unique=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthLimit(Base):
    __tablename__ = "auth_limits"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int]


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("owner_id", "id", name="uq_projects_owner_id_id"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_projects_status"),
        Index("ix_projects_owner_created_id", "owner_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(120))
    goal: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(String(8), server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProjectQuestion(Base):
    __tablename__ = "project_questions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "project_id"],
            ["projects.owner_id", "projects.id"],
            name="fk_questions_project_owner",
        ),
        CheckConstraint("status IN ('open', 'resolved')", name="ck_questions_status"),
        Index(
            "ix_questions_owner_project_created_id",
            "owner_id",
            "project_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    project_id: Mapped[UUID]
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(8), server_default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = (
        UniqueConstraint("owner_id", "id", name="uq_entries_owner_id_id"),
        UniqueConstraint(
            "owner_id", "client_submission_id", name="uq_entries_submission"
        ),
        Index("ix_entries_owner_created_id", "owner_id", "created_at", "id"),
        CheckConstraint("type IN ('thought', 'url', 'image')", name="ck_entries_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    client_submission_id: Mapped[UUID]
    type: Mapped[str] = mapped_column(String(16))
    source_url: Mapped[str | None] = mapped_column(Text)
    current_revision: Mapped[int] = mapped_column(server_default="1")
    possible_duplicate: Mapped[bool] = mapped_column(server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EntryRevision(Base):
    __tablename__ = "entry_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "entry_id"],
            ["entries.owner_id", "entries.id"],
            name="fk_entry_revisions_entry_owner",
        ),
        Index(
            "ix_entry_revisions_owner_fingerprint", "owner_id", "content_fingerprint"
        ),
    )

    owner_id: Mapped[UUID] = mapped_column(primary_key=True)
    entry_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, server_default="")
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    client_ocr: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    client_extract: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        UniqueConstraint("owner_id", "id", name="uq_attachments_owner_id_id"),
        UniqueConstraint(
            "owner_id", "client_submission_id", name="uq_attachments_submission"
        ),
        ForeignKeyConstraint(
            ["owner_id", "entry_id"],
            ["entries.owner_id", "entries.id"],
            name="fk_attachments_entry_owner",
        ),
        Index(
            "ix_attachments_unlinked_created",
            "created_at",
            postgresql_where=text("entry_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    client_submission_id: Mapped[UUID]
    entry_id: Mapped[UUID | None]
    content_type: Mapped[str] = mapped_column(String(32))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProjectEntry(Base):
    __tablename__ = "project_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "project_id"],
            ["projects.owner_id", "projects.id"],
            name="fk_project_entries_project_owner",
        ),
        ForeignKeyConstraint(
            ["owner_id", "entry_id"],
            ["entries.owner_id", "entries.id"],
            name="fk_project_entries_entry_owner",
        ),
        Index("ix_project_entries_owner_entry", "owner_id", "entry_id"),
    )

    owner_id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(primary_key=True)
    entry_id: Mapped[UUID] = mapped_column(primary_key=True)


class Job(Base):
    """Background work: ingesting an entry revision, or answering a question."""

    __tablename__ = "jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "message_id"],
            ["messages.owner_id", "messages.id"],
            name="fk_jobs_message_owner",
        ),
        UniqueConstraint("message_id", name="uq_jobs_message"),
        CheckConstraint(
            "(kind = 'entry_ingestion' AND entry_id IS NOT NULL"
            " AND revision IS NOT NULL AND message_id IS NULL)"
            " OR (kind = 'project_answer' AND message_id IS NOT NULL"
            " AND entry_id IS NULL AND revision IS NULL)",
            name="ck_jobs_subject",
        ),
        ForeignKeyConstraint(
            ["owner_id", "entry_id", "revision"],
            [
                "entry_revisions.owner_id",
                "entry_revisions.entry_id",
                "entry_revisions.revision",
            ],
            name="fk_jobs_entry_revision_owner",
        ),
        UniqueConstraint(
            "owner_id",
            "entry_id",
            "revision",
            "kind",
            name="uq_jobs_entry_revision_kind",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_jobs_status",
        ),
        Index(
            "ix_jobs_claimable",
            "run_after",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    entry_id: Mapped[UUID | None]
    revision: Mapped[int | None]
    message_id: Mapped[UUID | None]
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    attempts: Mapped[int] = mapped_column(server_default="0")
    generation: Mapped[int] = mapped_column(server_default="0")
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stage: Mapped[str | None] = mapped_column(String(32))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_step: Mapped[str | None] = mapped_column(String(32))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


def revision_key(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["owner_id", "entry_id", "revision"],
        [
            "entry_revisions.owner_id",
            "entry_revisions.entry_id",
            "entry_revisions.revision",
        ],
        name=name,
    )


class Chunk(Base):
    """A retrieval unit: a span of source text, or an entry's knowledge card."""

    __tablename__ = "chunks"
    __table_args__ = (
        revision_key("fk_chunks_entry_revision_owner"),
        UniqueConstraint(
            "owner_id", "entry_id", "revision", "unit", "ordinal", name="uq_chunks_unit"
        ),
        CheckConstraint("unit IN ('chunk', 'card')", name="ck_chunks_unit"),
        Index("ix_chunks_tokens", "tokens", postgresql_using="gin"),
        Index("ix_chunks_owner_entry", "owner_id", "entry_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    entry_id: Mapped[UUID]
    revision: Mapped[int]
    unit: Mapped[str] = mapped_column(String(8))
    ordinal: Mapped[int]
    source: Mapped[str] = mapped_column(String(16))
    start_offset: Mapped[int]
    end_offset: Mapped[int]
    display_text: Mapped[str] = mapped_column(Text)
    index_text: Mapped[str] = mapped_column(Text)
    tokens: Mapped[str] = mapped_column(TSVECTOR)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSION))
    context_header_version: Mapped[int]
    tokenizer_version: Mapped[int]
    index_version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Artifact(Base):
    """A derived product of one revision, such as its knowledge card."""

    __tablename__ = "artifacts"
    __table_args__ = (
        revision_key("fk_artifacts_entry_revision_owner"),
        UniqueConstraint(
            "owner_id", "entry_id", "revision", "kind", name="uq_artifacts_kind"
        ),
        CheckConstraint("kind IN ('card')", name="ck_artifacts_kind"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    entry_id: Mapped[UUID]
    revision: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prompt_version: Mapped[int]
    run_id: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Run(Base):
    """One model call, with usage and cost, for audit and budgeting."""

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_owner_created", "owner_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"))
    task: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[int | None]
    input_tokens: Mapped[int] = mapped_column(server_default="0")
    output_tokens: Mapped[int] = mapped_column(server_default="0")
    cost_estimate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    currency: Mapped[str | None] = mapped_column(String(3))
    price_date: Mapped[date | None] = mapped_column(Date)
    latency_ms: Mapped[int]
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("owner_id", "id", name="uq_conversations_owner_id_id"),
        ForeignKeyConstraint(
            ["owner_id", "project_id"],
            ["projects.owner_id", "projects.id"],
            name="fk_conversations_project_owner",
        ),
        Index(
            "ix_conversations_owner_project_created_id",
            "owner_id",
            "project_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    project_id: Mapped[UUID]
    title: Mapped[str] = mapped_column(String(120), server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Message(Base):
    """A question from the user, or the persisted answer to one (reply_to)."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("owner_id", "id", name="uq_messages_owner_id_id"),
        UniqueConstraint(
            "owner_id", "client_message_id", name="uq_messages_client_message"
        ),
        UniqueConstraint("reply_to", name="uq_messages_reply_to"),
        ForeignKeyConstraint(
            ["owner_id", "conversation_id"],
            ["conversations.owner_id", "conversations.id"],
            name="fk_messages_conversation_owner",
        ),
        ForeignKeyConstraint(
            ["owner_id", "reply_to"],
            ["messages.owner_id", "messages.id"],
            name="fk_messages_reply_owner",
        ),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        CheckConstraint(
            "(role = 'user') = (reply_to IS NULL)", name="ck_messages_reply"
        ),
        Index(
            "ix_messages_owner_conversation_created",
            "owner_id",
            "conversation_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[UUID]
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    client_message_id: Mapped[UUID | None]
    reply_to: Mapped[UUID | None]
    insufficient: Mapped[bool] = mapped_column(server_default="false")
    trace: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Citation(Base):
    """A numbered reference in an answer to a source chunk, as it was then."""

    __tablename__ = "citations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "message_id"],
            ["messages.owner_id", "messages.id"],
            name="fk_citations_message_owner",
        ),
        UniqueConstraint("message_id", "n", name="uq_citations_message_n"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    message_id: Mapped[UUID]
    n: Mapped[int]
    entry_id: Mapped[UUID]
    revision: Mapped[int]
    chunk_id: Mapped[UUID]
    start_offset: Mapped[int]
    end_offset: Mapped[int]
    snippet: Mapped[str] = mapped_column(Text)
