from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    __tablename__ = "jobs"
    __table_args__ = (
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
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID]
    entry_id: Mapped[UUID]
    revision: Mapped[int]
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
