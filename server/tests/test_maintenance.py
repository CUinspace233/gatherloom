"""Project events, debounced maintenance and summaries ([T4])."""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fake_models import FakeAPI, chat_reply
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_answering import project
from test_worker import Clock, Crash, Hooks

from app import events, jobs, maintenance
from app.models import Job, ProjectEvent, SummaryRevision


def summary_reply(text: str = "项目改用 @Observable 管理状态 [1]。") -> Any:
    return chat_reply(
        {
            "understanding": text,
            "key_points": [{"text": "状态管理用 @Observable [1]", "citations": [1]}],
            "uncertainties": ["还不清楚上传队列的重试上限"],
            "change_note": "首次总结",
        }
    )


def maintenance_jobs(sessions: sessionmaker[Session], project_id: UUID) -> list[Job]:
    with sessions() as db:
        return list(
            db.scalars(
                select(Job)
                .where(Job.project_id == project_id, Job.kind == jobs.MAINTENANCE)
                .order_by(Job.created_at)
            )
        )


def revisions(
    sessions: sessionmaker[Session], project_id: UUID
) -> list[SummaryRevision]:
    with sessions() as db:
        return list(
            db.scalars(
                select(SummaryRevision)
                .where(SummaryRevision.project_id == project_id)
                .order_by(SummaryRevision.version)
            )
        )


def event_count(sessions: sessionmaker[Session], project_id: UUID) -> int:
    with sessions() as db:
        return (
            db.scalar(
                select(func.count())
                .select_from(ProjectEvent)
                .where(ProjectEvent.project_id == project_id)
            )
            or 0
        )


def run_maintenance(
    sessions: sessionmaker[Session],
    saver: PostgresSaver,
    api: FakeAPI,
    clock: Clock,
    hooks: Hooks | None = None,
    worker: str = "maint",
) -> str:
    with sessions() as db:
        current = jobs.claim(
            db, worker, clock.advance(events.DEBOUNCE + timedelta(seconds=5))
        )
    assert current is not None and current.kind == jobs.MAINTENANCE
    pipeline = maintenance.Pipeline(
        sessions, saver, worker, models=api.models(), clock=clock, hook=hooks
    )
    return pipeline.run(current)


def summary_calls(api: FakeAPI) -> int:
    return sum(
        1
        for path, body in api.requests
        if path == "completions" and "当前认识" in body["messages"][0]["content"]
    )


def test_ready_entries_merge_into_one_maintenance_run(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    _owner, project_id, api, clock = ready_project
    assert event_count(committed, project_id) == 2
    queued = maintenance_jobs(committed, project_id)
    assert [job.status for job in queued] == ["queued"]

    api.chat.append(summary_reply())
    assert run_maintenance(committed, saver, api, clock) == "succeeded"
    [summary] = revisions(committed, project_id)
    assert summary.version == 1
    with committed() as db:
        assert summary.watermark == events.latest_seq(db, project_id)
    assert summary.content["understanding"].endswith("[1]。")
    assert [source["n"] for source in summary.sources] == [1]
    assert summary.coverage == {
        "entries": 2,
        "ready": 2,
        "represented": 2,
        "partial": False,
    }
    assert summary.model == "deepseek-flash"
    # Publishing a summary is AI output: no new event, no new job.
    assert event_count(committed, project_id) == 2
    assert [job.status for job in maintenance_jobs(committed, project_id)] == [
        "succeeded"
    ]


def test_old_watermark_never_overwrites_a_newer_summary(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    """[T4] A stalled worker that wakes up after a newer summary was published
    must not publish its older one."""
    owner, project_id, api, clock = ready_project
    api.chat.append(summary_reply("旧版本 [1]。"))
    with committed() as db:
        stalled = jobs.claim(
            db, "old", clock.advance(events.DEBOUNCE + timedelta(seconds=5))
        )
    assert stalled is not None and stalled.kind == jobs.MAINTENANCE
    hooks = Hooks()
    hooks.failures["publish"] = Crash()
    old = maintenance.Pipeline(
        committed, saver, "old", models=api.models(), clock=clock, hook=hooks
    )
    with pytest.raises(Crash):
        old.run(stalled)  # stalls just before publishing
    old_watermark = maintenance_jobs(committed, project_id)[0].payload["watermark"]  # type: ignore[index]

    # Its lease expires; a new change queues a newer job, which publishes.
    clock.advance(jobs.LEASE * 2)
    with committed.begin() as db:
        events.record(db, owner, project_id, events.QUESTION_ADDED)
        db.execute(
            update(Job)
            .where(Job.project_id == project_id, Job.status == "queued")
            .values(run_after=clock() - timedelta(days=1))
        )
    api.chat.append(summary_reply("新版本 [1]。"))
    with committed() as db:
        newer_claim = jobs.claim(db, "new", clock())
    assert newer_claim is not None and newer_claim.id != stalled.id
    newer = maintenance.Pipeline(
        committed, saver, "new", models=api.models(), clock=clock
    )
    assert newer.run(newer_claim) == "succeeded"
    [published] = revisions(committed, project_id)
    assert published.watermark > old_watermark

    # The stalled worker wakes up and continues: it must not publish.
    assert old.run(stalled) == "succeeded"
    assert [r.content["understanding"] for r in revisions(committed, project_id)] == [
        "新版本 [1]。"
    ]
    # It resumed at publish from its checkpoint: no second summary call.
    assert hooks.calls["summarize"] == 1
    assert summary_calls(api) == 2  # one per job


def test_crash_after_summarizing_does_not_call_the_model_again(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    _owner, project_id, api, clock = ready_project
    api.chat.append(summary_reply())
    hooks = Hooks()
    hooks.failures["publish:commit"] = Crash()
    with pytest.raises(Crash):
        run_maintenance(committed, saver, api, clock, hooks, worker="first")
    assert summary_calls(api) == 1
    clock.advance(jobs.LEASE * 2)
    assert (
        run_maintenance(committed, saver, api, clock, hooks, worker="second")
        == "succeeded"
    )
    assert summary_calls(api) == 1
    assert len(revisions(committed, project_id)) == 1


def test_nothing_new_is_a_noop_and_empty_projects_skip_the_model(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, project_id, api, clock = ready_project
    api.chat.append(summary_reply())
    run_maintenance(committed, saver, api, clock)
    calls = summary_calls(api)
    with committed.begin() as db:
        events.schedule_maintenance(db, owner, project_id)
    assert run_maintenance(committed, saver, api, clock) == "succeeded"
    assert summary_calls(api) == calls
    assert len(revisions(committed, project_id)) == 1

    empty = project(committed, owner, "空项目")
    with committed.begin() as db:
        events.record(db, owner, empty, events.QUESTION_ADDED)
    assert run_maintenance(committed, saver, api, clock) == "succeeded"
    assert summary_calls(api) == calls
    [summary] = revisions(committed, empty)
    assert summary.content["understanding"] == maintenance.EMPTY
    assert summary.coverage["entries"] == 0


def test_invalid_summary_citations_fail_after_one_repair(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    _owner, project_id, api, clock = ready_project
    api.chat += [summary_reply("引用了不存在的 [9]。"), summary_reply("还是 [9]。")]
    assert run_maintenance(committed, saver, api, clock) == "failed"
    assert revisions(committed, project_id) == []
    job = maintenance_jobs(committed, project_id)[0]
    assert job.error_code == "invalid_output"
