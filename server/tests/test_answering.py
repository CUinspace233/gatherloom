"""ProjectAnswerGraph: project scope [T3], citation checks, crash recovery."""

from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fake_models import FakeAPI, chat_reply
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from test_worker import Clock, Crash, Hooks, account, claim

from app import answering, ingestion, jobs
from app.models import (
    Citation,
    Conversation,
    Entry,
    EntryRevision,
    Job,
    Message,
    Project,
    ProjectEntry,
    Run,
)


def project(sessions: sessionmaker[Session], owner: UUID, name: str) -> UUID:
    with sessions.begin() as db:
        row = Project(owner_id=owner, name=name, goal=f"{name}的目标")
        db.add(row)
        db.flush()
        return row.id


def material(
    sessions: sessionmaker[Session], owner: UUID, project_id: UUID, text: str
) -> UUID:
    with sessions.begin() as db:
        entry = Entry(owner_id=owner, client_submission_id=uuid4(), type="thought")
        db.add(entry)
        db.flush()
        db.add(
            EntryRevision(
                owner_id=owner,
                entry_id=entry.id,
                revision=1,
                text=text,
                content_fingerprint=sha256(text.encode()).hexdigest(),
            )
        )
        db.add(ProjectEntry(owner_id=owner, project_id=project_id, entry_id=entry.id))
        db.flush()
        db.add(Job(owner_id=owner, entry_id=entry.id, revision=1, kind=jobs.INGESTION))
        return entry.id


def ingest_all(
    sessions: sessionmaker[Session], saver: PostgresSaver, api: FakeAPI, clock: Clock
) -> None:
    pipeline = ingestion.Pipeline(
        sessions, saver, "ingest", models=api.models(), clock=clock
    )
    while True:
        with sessions() as db:
            current = jobs.claim(db, "ingest", clock())
        if current is None:
            return
        assert current.kind == jobs.INGESTION
        assert pipeline.run(current) == "succeeded"


def question(
    sessions: sessionmaker[Session], owner: UUID, project_id: UUID, text: str
) -> UUID:
    with sessions.begin() as db:
        conversation = Conversation(owner_id=owner, project_id=project_id)
        db.add(conversation)
        db.flush()
        message = Message(
            owner_id=owner,
            conversation_id=conversation.id,
            role="user",
            content=text,
            client_message_id=uuid4(),
        )
        db.add(message)
        db.flush()
        db.add(Job(owner_id=owner, kind=jobs.ANSWER, message_id=message.id))
        return message.id


def answer_with(
    sessions: sessionmaker[Session],
    saver: PostgresSaver,
    api: FakeAPI,
    clock: Clock,
    hooks: Hooks | None = None,
    worker: str = "answer",
) -> str:
    current = claim(sessions, worker, clock.advance(timedelta(seconds=1)))
    assert current.kind == jobs.ANSWER
    pipeline = answering.Pipeline(
        sessions, saver, worker, models=api.models(), clock=clock, hook=hooks
    )
    return pipeline.run(current)


def reply(answer: str, citations: list[int], insufficient: bool = False) -> Any:
    return chat_reply(
        {"answer": answer, "citations": citations, "insufficient": insufficient}
    )


def answer_prompts(api: FakeAPI) -> list[str]:
    return [
        body["messages"][1]["content"]
        for path, body in api.requests
        if path == "completions" and body["model"] == "deepseek-flash"
    ]


def answers(sessions: sessionmaker[Session], question_id: UUID) -> list[Message]:
    with sessions() as db:
        return list(db.scalars(select(Message).where(Message.reply_to == question_id)))


@pytest.fixture
def setup(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> tuple[UUID, UUID, UUID, FakeAPI, Clock]:
    owner = account(committed)
    app_project = project(committed, owner, "知识库 App")
    coffee = project(committed, owner, "咖啡烘焙")
    material(committed, owner, app_project, "SwiftUI 状态管理改用 @Observable 宏。")
    material(committed, owner, app_project, "上传队列用幂等键防止重复提交。")
    material(committed, owner, coffee, "咖啡豆一爆之后发展九十秒再下豆。")
    api, clock = FakeAPI(), Clock()
    ingest_all(committed, saver, api, clock)
    return owner, app_project, coffee, api, clock


def test_answers_use_only_the_project_and_record_citations(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    setup: tuple[UUID, UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, app_project, _coffee, api, clock = setup
    asked = question(committed, owner, app_project, "SwiftUI 状态管理用什么？")
    api.chat.append(reply("改用 @Observable 宏 [1]。", [1]))
    assert answer_with(committed, saver, api, clock) == "succeeded"

    [prompt] = answer_prompts(api)
    assert "@Observable" in prompt and "幂等键" in prompt
    assert "咖啡" not in prompt and "一爆" not in prompt  # [T3]
    assert "导航上下文（不可引用）" in prompt
    [answer] = answers(committed, asked)
    assert answer.content == "改用 @Observable 宏 [1]。"
    assert answer.insufficient is False
    with committed() as db:
        trace = db.get_one(Message, asked).trace or {}
        evidence = {item["n"]: item for item in trace["evidence"]}
        [citation] = db.scalars(
            select(Citation).where(Citation.message_id == answer.id)
        )
        assert citation.n == 1
        assert str(citation.entry_id) == evidence[1]["entry_id"]
        assert citation.snippet == evidence[1]["text"]
        in_project = set(
            db.scalars(
                select(ProjectEntry.entry_id).where(
                    ProjectEntry.project_id == app_project
                )
            )
        )
        assert {UUID(item["entry_id"]) for item in trace["evidence"]} <= in_project
        run = db.scalars(
            select(Run).where(Run.owner_id == owner, Run.task == answering.TASK)
        ).one()
        assert (run.provider, run.model, run.prompt_version) == (
            "deepseek",
            "deepseek-flash",
            1,
        )
    # The card is navigation only: it has no number in the evidence list.
    assert all(item["text"] != "" for item in trace["evidence"])
    assert "[" not in "".join(card["title"] for card in trace["navigation"])


def test_invalid_citations_are_repaired_once_then_fail(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    setup: tuple[UUID, UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, app_project, _coffee, api, clock = setup
    repaired = question(committed, owner, app_project, "上传会重复吗？")
    api.chat += [
        reply("不会 [9]。", [9]),
        reply("用幂等键避免重复 [2]。", [2]),
    ]
    assert answer_with(committed, saver, api, clock) == "succeeded"
    repair = [
        body["messages"][-1]["content"]
        for path, body in api.requests
        if path == "completions" and len(body["messages"]) > 2
    ]
    assert "不存在的证据编号：[9]" in repair[0]
    assert len(answers(committed, repaired)) == 1

    failing = question(committed, owner, app_project, "还有什么？")
    api.chat += [
        reply("见 [1]。", []),  # marker without citation
        chat_reply("not json"),
    ]
    assert answer_with(committed, saver, api, clock) == "failed"
    assert answers(committed, failing) == []
    with committed() as db:
        job = db.scalars(select(Job).where(Job.message_id == failing)).one()
        assert (job.status, job.error_code) == ("failed", "invalid_output")


def test_no_evidence_answers_insufficient_without_a_model_call(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    setup: tuple[UUID, UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, _app_project, _coffee, api, clock = setup
    empty = project(committed, owner, "空项目")
    asked = question(committed, owner, empty, "这里有什么？")
    assert answer_with(committed, saver, api, clock) == "succeeded"
    assert answer_prompts(api) == []
    [answer] = answers(committed, asked)
    assert answer.insufficient is True
    assert answer.content == answering.INSUFFICIENT
    with committed() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Citation)
                .where(Citation.message_id == answer.id)
            )
            == 0
        )


def test_crash_before_publishing_does_not_call_the_model_again(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    setup: tuple[UUID, UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, app_project, _coffee, api, clock = setup
    asked = question(committed, owner, app_project, "SwiftUI 怎么管理状态？")
    api.chat.append(reply("用 @Observable [1]。", [1]))
    hooks = Hooks()
    hooks.failures["publish:commit"] = Crash()
    with pytest.raises(Crash):
        answer_with(committed, saver, api, clock, hooks, worker="first")
    assert answers(committed, asked) == []
    clock.advance(jobs.LEASE * 2)
    assert (
        answer_with(committed, saver, api, clock, hooks, worker="second") == "succeeded"
    )
    assert len(answer_prompts(api)) == 1
    assert hooks.calls["answer"] == 1
    assert len(answers(committed, asked)) == 1


def test_answers_are_claimed_before_ingestion_and_run_per_kind(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    setup: tuple[UUID, UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, app_project, _coffee, _api, clock = setup
    material(committed, owner, app_project, "新资料，等待整理")
    asked = question(committed, owner, app_project, "有新资料吗？")
    first = claim(committed, "w1", clock() + timedelta(seconds=1))
    assert (first.kind, first.message_id) == (jobs.ANSWER, asked)
    # The account's answer job is running; its ingestion job is still claimable.
    second = claim(committed, "w2", clock() + timedelta(seconds=1))
    assert second.kind == jobs.INGESTION
