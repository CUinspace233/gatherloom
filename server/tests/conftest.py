import tempfile
from collections.abc import Iterator
from os import environ, getpid
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import Connection, Engine, create_engine, delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from app import storage
from app.db import get_db
from app.main import app
from app.models import (
    Artifact,
    Attachment,
    Chunk,
    Citation,
    Conversation,
    Decision,
    DecisionRevision,
    Deletion,
    Entry,
    EntryRevision,
    Job,
    LoginSession,
    Message,
    Project,
    ProjectEntry,
    ProjectEvent,
    ProjectQuestion,
    Relation,
    Review,
    Run,
    SummaryRevision,
    User,
)
from app.worker import checkpointer, setup_checkpoints

COMMITTED_PREFIX = "worker_"
environ["GATHERLOOM_DELETION_LOG"] = str(
    Path(tempfile.gettempdir()) / f"gatherloom-test-deletions-{getpid()}.jsonl"
)


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    url = environ.get("GATHERLOOM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set GATHERLOOM_TEST_DATABASE_URL to a separate pgvector database")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def connection(test_engine: Engine) -> Iterator[Connection]:
    with test_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def client(connection: Connection) -> Iterator[TestClient]:
    def get_test_db() -> Iterator[Session]:
        with Session(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as db:
            yield db

    app.dependency_overrides[get_db] = get_test_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db)


@pytest.fixture(autouse=True)
def storage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(storage, "STORAGE_DIR", tmp_path / "storage")
    return tmp_path / "storage"


def purge_committed(sessions: sessionmaker[Session]) -> None:
    """Delete accounts named worker_* and everything they own."""
    with sessions.begin() as db:
        owners = list(
            db.scalars(
                select(User.id).where(User.username.startswith(COMMITTED_PREFIX))
            )
        )
        if not owners:
            return
        for model in (
            Run,
            Relation,
            DecisionRevision,
            Decision,
            Artifact,
            Chunk,
            Job,
            Citation,
            Message,
            Conversation,
            ProjectEntry,
            Attachment,
            EntryRevision,
            Entry,
            ProjectEvent,
            SummaryRevision,
            Review,
            ProjectQuestion,
            Project,
            Deletion,
        ):
            db.execute(delete(model).where(model.owner_id.in_(owners)))
        db.execute(delete(LoginSession).where(LoginSession.owner_id.in_(owners)))
        db.execute(delete(User).where(User.id.in_(owners)))
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            for owner in owners:
                db.execute(
                    text(f"DELETE FROM {table} WHERE thread_id LIKE :prefix"),
                    {"prefix": f"{owner}:%"},
                )


@pytest.fixture
def committed(test_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Sessions that really commit, for workers that use several connections.

    Data must belong to accounts named worker_*; it is removed afterwards.
    """
    setup_checkpoints(test_engine.url.render_as_string(hide_password=False))
    sessions = sessionmaker(test_engine, expire_on_commit=False)
    purge_committed(sessions)
    try:
        yield sessions
    finally:
        purge_committed(sessions)


@pytest.fixture
def saver(
    committed: sessionmaker[Session], test_engine: Engine
) -> Iterator[PostgresSaver]:
    with checkpointer(test_engine.url.render_as_string(hide_password=False)) as saver:
        yield saver


@pytest.fixture
def ready_project(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> tuple[UUID, UUID, Any, Any]:
    """An account with one project, two ingested entries and a queued
    maintenance job. Returns (owner, project, FakeAPI, Clock)."""
    from fake_models import FakeAPI
    from test_answering import ingest_all, material, project
    from test_worker import Clock, account

    owner = account(committed)
    project_id = project(committed, owner, "知识库 App")
    material(committed, owner, project_id, "SwiftUI 状态管理改用 @Observable 宏。")
    material(committed, owner, project_id, "上传队列用幂等键防止重复提交。")
    api, clock = FakeAPI(), Clock()
    ingest_all(committed, saver, api, clock)
    return owner, project_id, api, clock
