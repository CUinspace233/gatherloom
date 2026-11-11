"""Weekly and manual reviews ([T7])."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fake_models import FakeAPI, chat_reply
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import Connection, insert, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_answering import project
from test_conversations_api import setup_account
from test_worker import Clock

from app import jobs, reviews
from app.models import Job, Project, ProjectEvent, Review

SHANGHAI = ZoneInfo("Asia/Shanghai")
REVIEW = "阶段回顾"


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI)


def test_weekly_period_starts_after_monday_nine_local_time() -> None:
    before = reviews.weekly_period("Asia/Shanghai", local(2026, 11, 16, 8, 59))
    after = reviews.weekly_period("Asia/Shanghai", local(2026, 11, 16, 9, 1))
    assert before == (local(2026, 11, 2), local(2026, 11, 9))
    assert after == (local(2026, 11, 9), local(2026, 11, 16))
    # Midweek still points at the last complete week.
    assert reviews.weekly_period("Asia/Shanghai", local(2026, 11, 19, 15)) == after


def review_reply() -> Any:
    return chat_reply(
        {
            "new_information": [{"text": "改用 @Observable [1]", "citations": [1]}],
            "understanding_changes": [],
            "decisions": [],
            "open_questions": [{"text": "上传重试上限未定", "citations": []}],
            "next_steps": [{"text": "建议补充重试策略的资料", "citations": []}],
        }
    )


def run_review(
    sessions: sessionmaker[Session],
    saver: PostgresSaver,
    api: FakeAPI,
    clock: Clock,
) -> str:
    with sessions() as db:
        current = jobs.claim(db, "review", clock.advance(timedelta(seconds=1)))
    assert current is not None and current.kind == jobs.REVIEW
    pipeline = reviews.Pipeline(
        sessions, saver, "review", models=api.models(), clock=clock
    )
    return pipeline.run(current)


def published(sessions: sessionmaker[Session], project_id: UUID) -> list[Review]:
    with sessions() as db:
        return list(db.scalars(select(Review).where(Review.project_id == project_id)))


def review_calls(api: FakeAPI) -> int:
    return sum(
        1
        for path, body in api.requests
        if path == "completions" and REVIEW in body["messages"][0]["content"]
    )


def test_scan_queues_once_and_a_period_publishes_once(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, project_id, api, clock = ready_project
    quiet = project(committed, owner, "没有变化的项目")
    now = datetime.now(UTC)
    start, end = reviews.weekly_period("Asia/Shanghai", now)
    with committed.begin() as db:
        # Move this project's events into last week; the maintenance job too.
        db.execute(
            update(ProjectEvent)
            .where(ProjectEvent.project_id == project_id)
            .values(created_at=start + timedelta(days=1))
        )
        db.execute(
            update(Job).where(Job.project_id == project_id).values(status="cancelled")
        )
        db.execute(
            update(Project)
            .where(Project.id.in_([project_id, quiet]))
            .values(created_at=start - timedelta(days=30))
        )
    with committed.begin() as db:
        assert reviews.scan(db, now) == 1
    with committed.begin() as db:
        assert reviews.scan(db, now) == 0  # [T7] already queued

    api.script(REVIEW, review_reply())
    assert run_review(committed, saver, api, clock) == "succeeded"
    [review] = published(committed, project_id)
    assert (review.kind, review.period_start, review.period_end) == ("auto", start, end)
    assert review.content["new_information"][0]["citations"] == [1]
    assert [s["n"] for s in review.sources] == [1]
    assert review.input_snapshot["events"] == 2
    assert published(committed, quiet) == []  # no change, no review

    # A duplicate job for the same period finishes without a second review.
    with committed.begin() as db:
        reviews.queue_review(db, owner, project_id, "auto", start, end)
    api.script(REVIEW, review_reply())
    assert run_review(committed, saver, api, clock) == "succeeded"
    assert len(published(committed, project_id)) == 1
    with committed.begin() as db:
        assert reviews.scan(db, now) == 0


def test_quiet_manual_review_skips_the_model(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, project_id, api, clock = ready_project
    later = datetime.now(UTC) + timedelta(days=1)
    with committed.begin() as db:
        reviews.queue_review(
            db, owner, project_id, "manual", later, later + timedelta(hours=1)
        )
    assert run_review(committed, saver, api, clock) == "succeeded"
    assert review_calls(api) == 0
    [review] = published(committed, project_id)
    assert review.content["new_information"][0]["text"] == reviews.QUIET


def test_review_api_manual_periods_read_marks_and_isolation(
    client: TestClient, connection: Connection
) -> None:
    a = setup_account(client, connection, "rev_a")
    b = setup_account(client, connection, "rev_b")
    path = f"/v1/projects/{a['project']}/reviews"
    accepted = client.post(path, headers=a["headers"])
    assert accepted.status_code == 202
    run = client.get(
        f"/v1/runs/{accepted.json()['run_id']}", headers=a["headers"]
    ).json()
    assert run["status"] == "queued" and run["review_id"] is None
    first = connection.scalar(
        select(Job.payload).where(Job.id == UUID(accepted.json()["run_id"]))
    )
    assert first is not None and first["kind"] == "manual"

    review_id = uuid4()
    end = datetime(2026, 11, 9, tzinfo=UTC)
    connection.execute(
        insert(Review).values(
            id=review_id,
            owner_id=a["id"],
            project_id=UUID(a["project"]),
            kind="manual",
            period_start=end - timedelta(days=7),
            period_end=end,
            content={section: [] for section in reviews.SECTIONS},
            sources=[],
            input_snapshot={},
        )
    )
    second = client.post(path, headers=a["headers"]).json()
    payload = connection.scalar(
        select(Job.payload).where(Job.id == UUID(second["run_id"]))
    )
    assert payload is not None
    assert datetime.fromisoformat(payload["period_start"]) == end

    listed = client.get(path, headers=a["headers"]).json()
    assert [(r["id"], r["unread"]) for r in listed] == [(str(review_id), True)]
    detail = client.get(f"/v1/reviews/{review_id}", headers=a["headers"]).json()
    assert detail["sections"]["next_steps"] == []
    assert (
        client.patch(f"/v1/reviews/{review_id}/read", headers=a["headers"]).status_code
        == 204
    )
    assert client.get(path, headers=a["headers"]).json()[0]["unread"] is False

    assert (
        client.get(f"/v1/reviews/{review_id}", headers=b["headers"]).status_code == 404
    )
    assert (
        client.patch(f"/v1/reviews/{review_id}/read", headers=b["headers"]).status_code
        == 404
    )
    assert client.post(path, headers=b["headers"]).status_code == 404
    assert (
        client.get(f"/v1/runs/{second['run_id']}", headers=b["headers"]).status_code
        == 404
    )
