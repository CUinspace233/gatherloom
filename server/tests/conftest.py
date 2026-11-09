from collections.abc import Iterator
from os import environ

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import app


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
