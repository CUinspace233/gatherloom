"""Embedding, knowledge cards and run records in the ingestion graph."""

from datetime import timedelta
from uuid import UUID

import pytest
from fake_models import CARD, FakeAPI, chat_reply
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_worker import Clock, Crash, Hooks, account, claim, entry, job_of

from app import jobs
from app.ingestion import Pipeline
from app.models import Artifact, Chunk, Job, Run


def run_once(
    sessions: sessionmaker[Session],
    saver: PostgresSaver,
    api: FakeAPI,
    clock: Clock,
    hooks: Hooks | None = None,
    worker: str = "w",
) -> str:
    current = claim(sessions, worker, clock())
    pipeline = Pipeline(
        sessions, saver, worker, models=api.models(), clock=clock, hook=hooks
    )
    return pipeline.run(current)


def rows(sessions: sessionmaker[Session], entry_id: UUID) -> dict[str, int]:
    with sessions() as db:
        chunks = db.execute(
            select(Chunk.unit, func.count(), func.count(Chunk.embedding))
            .where(Chunk.entry_id == entry_id)
            .group_by(Chunk.unit)
        ).all()
        result = {f"{unit}": total for unit, total, _embedded in chunks}
        result |= {f"{unit}_embedded": embedded for unit, _total, embedded in chunks}
        result["cards"] = (
            db.scalar(
                select(func.count())
                .select_from(Artifact)
                .where(Artifact.entry_id == entry_id)
            )
            or 0
        )
        return result


def runs(sessions: sessionmaker[Session], owner: UUID) -> list[Run]:
    with sessions() as db:
        return list(
            db.scalars(
                select(Run).where(Run.owner_id == owner).order_by(Run.created_at)
            )
        )


def test_full_ingestion_embeds_generates_card_and_records_runs(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    entry_id = entry(committed, owner)
    api = FakeAPI()
    assert run_once(committed, saver, api, Clock()) == "succeeded"

    counts = rows(committed, entry_id)
    assert counts["chunk"] == counts["chunk_embedded"] > 1
    assert (counts["card"], counts["card_embedded"], counts["cards"]) == (1, 1, 1)
    with committed() as db:
        card = db.scalars(select(Artifact).where(Artifact.entry_id == entry_id)).one()
        assert card.content["title"] == CARD["title"]
        assert card.content["model"] == "qwen3.7-flash-2026-07-15"
        assert card.prompt_version == 1
        card_chunk = db.scalars(
            select(Chunk).where(Chunk.entry_id == entry_id, Chunk.unit == "card")
        ).one()
        assert str(CARD["title"]) in card_chunk.index_text
        assert "离线收集要满足什么" not in card_chunk.index_text  # R3 off for now
        job = db.scalars(select(Job).where(Job.entry_id == entry_id)).one()
        assert job.indexed_at is not None and job.status == "succeeded"

    recorded = runs(committed, owner)
    assert [run.task for run in recorded] == ["embed", "knowledge_card", "embed_card"]
    assert card.run_id == recorded[1].id
    assert all(run.job_id == job.id and run.error_code is None for run in recorded)
    assert recorded[1].cost_estimate is not None and recorded[1].currency == "CNY"
    assert recorded[1].prompt_version == 1 and recorded[0].prompt_version is None


def test_card_failure_is_partial_and_retry_reuses_the_index(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    entry_id = entry(committed, owner)
    api = FakeAPI()
    api.chat += [chat_reply("坏的输出"), chat_reply("还是坏的")]
    assert run_once(committed, saver, api, Clock()) == "failed"

    job = job_of(committed, entry_id)
    assert jobs.processing_status(job.status, job.indexed_at) == "partial"
    assert (job.error_code, jobs.failed_step(job.error_step)) == (
        "invalid_output",
        "card",
    )
    counts = rows(committed, entry_id)
    assert counts["chunk"] == counts["chunk_embedded"] and counts["cards"] == 0
    assert [run.task for run in runs(committed, owner)].count("knowledge_card") == 2

    embeddings_before = api.count("embeddings")
    with committed.begin() as db:
        db.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(status="queued", attempts=0, generation=Job.generation + 1)
        )
    assert run_once(committed, saver, api, Clock()) == "succeeded"
    # Only the card and its embedding are new; source chunks were kept.
    assert api.count("embeddings") == embeddings_before + 1
    assert rows(committed, entry_id)["cards"] == 1


def test_embedding_outage_retries_then_fails_unsearchable(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    entry_id = entry(committed, owner)
    api = FakeAPI()
    api.embeddings += [503, 503, 503]
    clock = Clock()
    statuses = []
    for _ in range(3):
        statuses.append(run_once(committed, saver, api, clock))
        clock.advance(timedelta(hours=1))
    assert statuses == ["queued", "queued", "failed"]
    job = job_of(committed, entry_id)
    assert job.indexed_at is None
    assert jobs.processing_status(job.status, job.indexed_at) == "failed"
    assert jobs.failed_step(job.error_step) == "index"
    assert [run.error_code for run in runs(committed, owner)] == ["provider_error"] * 3


def test_crash_before_card_resumes_without_repeating_model_calls(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    entry_id = entry(committed, owner)
    api, clock, hooks = FakeAPI(), Clock(), Hooks()
    hooks.failures["card_index"] = Crash()
    with pytest.raises(Crash):
        run_once(committed, saver, api, clock, hooks, worker="first")
    calls_before = (api.count("embeddings"), api.count("completions"))
    assert calls_before[1] == 1

    clock.advance(jobs.LEASE * 2)
    assert run_once(committed, saver, api, clock, hooks, worker="second") == "succeeded"
    # Only the card embedding is new: no re-embedding, no second card call.
    assert api.count("completions") == 1
    assert api.count("embeddings") == calls_before[0] + 1
    assert hooks.calls["embed"] == 1 and hooks.calls["card"] == 1
    assert rows(committed, entry_id)["cards"] == 1
