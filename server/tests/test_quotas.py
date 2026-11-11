"""Daily quotas and model budget per account."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fake_models import FakeAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, insert, select
from sqlalchemy.orm import Session, sessionmaker
from test_conversations_api import setup_account
from test_worker import Clock

from app import jobs, quotas, worker
from app.models import Job, Run


@pytest.fixture
def tight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATHERLOOM_DAILY_ENTRIES", "2")
    monkeypatch.setenv("GATHERLOOM_DAILY_QUESTIONS", "1")
    monkeypatch.setenv("GATHERLOOM_DAILY_REVIEWS", "1")
    monkeypatch.setenv("GATHERLOOM_DAILY_BUDGET_CNY", "1")


def thought(client: TestClient, auth: dict[str, str], submission: str) -> int:
    return client.post(
        "/v1/entries",
        json={"client_submission_id": submission, "text": "一条想法"},
        headers=auth,
    ).status_code


def spend(connection: Connection, owner: UUID, usd: str) -> None:
    connection.execute(
        insert(Run).values(
            id=uuid4(),
            owner_id=owner,
            task="answer",
            provider="deepseek",
            model="deepseek-flash",
            cost_estimate=Decimal(usd),
            currency="USD",
            latency_ms=1,
        )
    )


def test_daily_limits_answer_429_and_replays_still_succeed(
    client: TestClient, connection: Connection, tight: None
) -> None:
    a = setup_account(client, connection, "quota_a")
    b = setup_account(client, connection, "quota_b")
    first, second = str(uuid4()), str(uuid4())
    assert thought(client, a["headers"], first) == 201
    assert thought(client, a["headers"], second) == 201
    refused = client.post(
        "/v1/entries",
        json={"client_submission_id": str(uuid4()), "text": "第三条"},
        headers=a["headers"],
    )
    assert refused.status_code == 429
    assert refused.json()["detail"] == "daily entries limit reached"
    assert 0 < int(refused.headers["Retry-After"]) <= 86400
    # A retried upload of an entry already saved is not a new entry.
    assert thought(client, a["headers"], first) == 200
    # Another account has its own count.
    assert thought(client, b["headers"], str(uuid4())) == 201

    conversation = client.post(
        f"/v1/projects/{a['project']}/conversations", json={}, headers=a["headers"]
    ).json()
    path = f"/v1/conversations/{conversation['id']}/messages"
    asked = {"client_message_id": str(uuid4()), "text": "问题一"}
    assert client.post(path, json=asked, headers=a["headers"]).status_code == 202
    assert client.post(path, json=asked, headers=a["headers"]).status_code == 202
    again = client.post(
        path,
        json={"client_message_id": str(uuid4()), "text": "问题二"},
        headers=a["headers"],
    )
    assert again.status_code == 429
    assert again.json()["detail"] == "daily questions limit reached"

    reviews = f"/v1/projects/{a['project']}/reviews"
    assert client.post(reviews, headers=a["headers"]).status_code == 202
    assert client.post(reviews, headers=a["headers"]).status_code == 429


def test_spent_budget_blocks_questions_and_reviews(
    client: TestClient, connection: Connection, tight: None
) -> None:
    a = setup_account(client, connection, "budget_a")
    spend(connection, a["id"], "0.2")  # ¥1.44 at the default rate
    conversation = client.post(
        f"/v1/projects/{a['project']}/conversations", json={}, headers=a["headers"]
    ).json()
    refused = client.post(
        f"/v1/conversations/{conversation['id']}/messages",
        json={"client_message_id": str(uuid4()), "text": "问题"},
        headers=a["headers"],
    )
    assert refused.status_code == 429
    assert refused.json()["detail"] == "daily model budget reached"
    review = client.post(f"/v1/projects/{a['project']}/reviews", headers=a["headers"])
    assert review.status_code == 429
    # Saving material is never blocked by the budget.
    assert thought(client, a["headers"], str(uuid4())) == 201


def test_worker_postpones_background_jobs_over_budget(
    committed: sessionmaker[Session],
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
    tight: None,
) -> None:
    owner, _project, _api, _clock = ready_project
    with committed.begin() as db:
        db.add(
            Run(
                owner_id=owner,
                task="summary",
                provider="qwen",
                model="qwen3.8-flash",
                cost_estimate=Decimal(2),
                currency="CNY",
                latency_ms=1,
            )
        )
    now = jobs.utcnow() + timedelta(minutes=5)
    with committed() as db:
        claim = jobs.claim(db, "budget", now)
    assert claim is not None and claim.kind == jobs.MAINTENANCE
    assert worker.postpone(committed, claim, "budget")
    with committed() as db:
        job = db.get_one(Job, claim.id)
        _start, end = quotas.day_bounds(db, owner, datetime.now(UTC))
        assert (job.status, job.attempts, job.error_code) == (
            "queued",
            0,
            "daily_budget",
        )
        assert job.run_after == end
        assert db.scalar(select(Job.id).where(jobs.claimable(now))) is None
