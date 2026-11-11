"""Deletion lifecycle [T5]: deleted content leaves search at once and every
copy of it is purged shortly after; replay after a restore deletes it again."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fake_models import FakeAPI
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from test_answering import answer_with, answers, question, reply
from test_maintenance import run_maintenance, summary_reply
from test_worker import Clock, claim

from app import answering, deletion, jobs
from app.models import (
    Artifact,
    Chunk,
    Citation,
    Deletion,
    Entry,
    EntryRevision,
    Job,
    Message,
    Project,
    ProjectEntry,
    SummaryRevision,
)


def delete_now(sessions: sessionmaker[Session], owner: UUID, entry_id: UUID) -> None:
    with sessions.begin() as db:
        entry = db.scalars(
            select(Entry).where(Entry.owner_id == owner, Entry.id == entry_id)
        ).one()
        deletion.mark_entry_deleted(db, entry, jobs.utcnow())


def count(sessions: sessionmaker[Session], model: Any, entry_id: UUID) -> int:
    with sessions() as db:
        return (
            db.scalar(
                select(func.count())
                .select_from(model)
                .where(model.entry_id == entry_id)
            )
            or 0
        )


def checkpoint_threads(sessions: sessionmaker[Session], prefix: str) -> int:
    with sessions() as db:
        return (
            db.scalar(
                text("SELECT count(*) FROM checkpoints WHERE thread_id LIKE :p"),
                {"p": prefix + "%"},
            )
            or 0
        )


def test_deleted_entries_leave_search_now_and_are_purged_later(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, project_id, api, clock = ready_project
    api.chat.append(summary_reply())
    assert run_maintenance(committed, saver, api, clock) == "succeeded"
    asked = question(committed, owner, project_id, "SwiftUI 状态管理用什么？")
    api.chat.append(reply("改用 @Observable 宏 [1]。", [1]))
    assert answer_with(committed, saver, api, clock) == "succeeded"
    with committed() as db:
        trace = db.get_one(Message, asked).trace or {}
        cited = UUID(trace["evidence"][0]["entry_id"])
        kept = next(
            UUID(item["entry_id"])
            for item in trace["evidence"]
            if UUID(item["entry_id"]) != cited
        )
        answer_job = db.scalars(select(Job).where(Job.message_id == asked)).one()
    # A leftover checkpoint of a run that read the entry.
    thread = f"{owner}:{answer_job.id}:"
    saver.put(
        {"configurable": {"thread_id": thread + "9", "checkpoint_ns": ""}},
        empty_checkpoint(),
        {},
        {},
    )

    delete_now(committed, owner, cited)
    # Right away: out of search, and recorded for purging and replay.
    assert count(committed, Chunk, cited) == 0
    with committed() as db:
        ledger = db.scalars(select(Deletion).where(Deletion.subject_id == cited)).one()
        assert ledger.purged_at is None
    logged = [json.loads(line) for line in deletion.log_path().read_text().splitlines()]
    assert {"kind": "entry", "id": str(cited)}.items() <= logged[-1].items()

    # Not before running workers have lost their leases.
    assert deletion.purge_due(committed, jobs.utcnow()).entries == 0
    later = jobs.utcnow() + deletion.PURGE_DELAY + timedelta(seconds=1)
    assert deletion.purge_due(committed, later).entries == 1
    assert deletion.purge_due(committed, later).entries == 0

    with committed() as db:
        assert db.get_one(Deletion, ledger.id).purged_at is not None
        revision = db.scalars(
            select(EntryRevision).where(EntryRevision.entry_id == cited)
        ).one()
        assert (revision.text, revision.note, revision.client_ocr) == ("", "", None)
        [answer] = answers(committed, asked)
        assert answer.content == deletion.MASKED_ANSWER
        [citation] = db.scalars(
            select(Citation).where(Citation.message_id == answer.id)
        )
        assert citation.snippet == ""
        evidence = {
            item["entry_id"]: item["text"]
            for item in (db.get_one(Message, asked).trace or {})["evidence"]
        }
        assert evidence[str(cited)] == "" and evidence[str(kept)] != ""
        summary = db.scalars(
            select(SummaryRevision).where(SummaryRevision.project_id == project_id)
        ).one()
        assert summary.content["understanding"]  # the text and dates stay
        for item in summary.sources:
            assert item["text"] == "" or item["entry_id"] != str(cited)
        for job in db.scalars(
            select(Job).where(Job.project_id == project_id, Job.payload.is_not(None))
        ):
            for item in (job.payload or {}).get("evidence", []):
                assert item["text"] == "" or item["entry_id"] != str(cited)
    for model in (Artifact, ProjectEntry, Chunk):
        assert count(committed, model, cited) == 0
    assert count(committed, Artifact, kept) == 1
    assert checkpoint_threads(committed, thread) == 0


def test_answers_published_after_a_deletion_are_masked(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, project_id, api, clock = ready_project
    asked = question(committed, owner, project_id, "SwiftUI 状态管理用什么？")
    api.chat.append(reply("改用 @Observable 宏 [1]。", [1]))

    def delete_before_publish(stage: str) -> None:
        if stage == "publish":
            with committed() as db:
                trace = db.get_one(Message, asked).trace or {}
            delete_now(committed, owner, UUID(trace["evidence"][0]["entry_id"]))

    current = claim(committed, "answer", clock.advance(timedelta(seconds=1)))
    pipeline = answering.Pipeline(
        committed,
        saver,
        "answer",
        models=api.models(),
        clock=clock,
        hook=delete_before_publish,
    )
    assert pipeline.run(current) == "succeeded"
    [answer] = answers(committed, asked)
    assert answer.content == deletion.MASKED_ANSWER
    with committed() as db:
        [citation] = db.scalars(
            select(Citation).where(Citation.message_id == answer.id)
        )
        assert citation.snippet == ""


def test_replay_after_restore_deletes_again(
    committed: sessionmaker[Session],
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATHERLOOM_DELETION_LOG", str(tmp_path / "deletions.jsonl"))
    owner, project_id, _api, _clock = ready_project
    with committed() as db:
        entry_id = db.scalars(
            select(ProjectEntry.entry_id).where(ProjectEntry.project_id == project_id)
        ).first()
    assert entry_id is not None
    delete_now(committed, owner, entry_id)
    log = deletion.log_path()
    deletion.append_log(owner, "project", project_id, jobs.utcnow())

    # An older backup is restored: the entry is back, with no ledger row.
    with committed.begin() as db:
        db.get_one(Entry, entry_id).deleted_at = None
        db.execute(delete(Deletion).where(Deletion.subject_id == entry_id))

    purged = deletion.replay(committed, log, jobs.utcnow())
    assert purged.entries == 1
    with committed() as db:
        entry = db.get_one(Entry, entry_id)
        assert entry.deleted_at is not None
        revision = db.scalars(
            select(EntryRevision).where(EntryRevision.entry_id == entry_id)
        ).one()
        assert revision.text == ""
        assert db.get(Project, project_id) is None
    # Replaying twice changes nothing.
    assert deletion.replay(committed, log, jobs.utcnow()).entries == 0
