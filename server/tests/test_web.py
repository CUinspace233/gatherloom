"""Firecrawl contract validation, snapshots, backfill and lease fencing."""

import json
import threading
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from fake_models import FakeAPI
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker
from test_answering import answer_with, answers, project, question, reply
from test_worker import Clock, Crash, Hooks, account, claim, entry, job_of

from app import deletion, jobs, scheduler, web
from app.ingestion import Pipeline
from app.models import (
    Artifact,
    Chunk,
    Citation,
    Entry,
    EntryRevision,
    Job,
    ProjectEntry,
)
from app.settings import load_settings

BODY = "# 网页正文\n\nSwiftUI 的 @Observable 宏用于状态管理。\n\n支持中文与 emoji 🌿。"


def page(text: str = BODY, status: int = 200) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "markdown": text,
            "metadata": {"title": "网页标题", "statusCode": status},
        },
    }


class Site:
    def __init__(self, response: httpx.Response | Exception | None = None) -> None:
        self.response = (
            response if response is not None else httpx.Response(200, json=page())
        )
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def extractor(self) -> web.Firecrawl:
        return web.Firecrawl(
            "test-secret",
            client=httpx.Client(
                base_url="https://api.firecrawl.dev/v2/",
                timeout=75,
                transport=httpx.MockTransport(self.handle),
            ),
        )


def link(
    sessions: sessionmaker[Session],
    owner: UUID,
    client_extract: dict[str, Any] | None = None,
) -> UUID:
    saved = entry(sessions, owner, "")
    with sessions.begin() as db:
        db.execute(
            update(Entry)
            .where(Entry.id == saved)
            .values(type="url", source_url="https://example.com/post")
        )
        db.execute(
            update(EntryRevision)
            .where(EntryRevision.entry_id == saved)
            .values(client_extract=client_extract)
        )
    return saved


def run(
    sessions: sessionmaker[Session],
    saver: PostgresSaver,
    clock: Clock,
    site: Site | None,
    hooks: Hooks | None = None,
    models: FakeAPI | None = None,
) -> str:
    current = claim(sessions, "web", clock())
    return Pipeline(
        sessions,
        saver,
        "web",
        extractor=site.extractor() if site else None,
        models=models.models() if models else None,
        clock=clock,
        hook=hooks,
    ).run(current)


def test_contract_auth_payload_and_exact_body() -> None:
    site = Site()
    snapshot = site.extractor().scrape("https://example.com/post")
    request = site.requests[0]
    assert str(request.url) == "https://api.firecrawl.dev/v2/scrape"
    assert request.headers["Authorization"] == "Bearer test-secret"
    assert json.loads(request.content) == {
        "url": "https://example.com/post",
        "formats": ["markdown"],
        "onlyMainContent": True,
        "storeInCache": False,
        "timeout": 60000,
    }
    assert request.extensions["timeout"]["read"] == 75
    assert snapshot["text"] == BODY and snapshot["provider"] == "firecrawl"
    assert (
        snapshot["captured_at"] and snapshot["source_url"] == "https://example.com/post"
    )


@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (httpx.Response(401), "web_auth", False),
        (httpx.Response(402), "web_quota", False),
        (httpx.Response(403), "web_forbidden", False),
        (httpx.Response(429), "web_rate_limit", True),
        (httpx.Response(503, text="bad gateway"), "web_provider_error", True),
        (httpx.ReadTimeout("never log this secret"), "web_timeout", True),
        (httpx.ConnectError("secret"), "web_network", True),
        (httpx.Response(200, json=page(" \n")), "web_empty", False),
        (httpx.Response(200, json=page("x" * 100001)), "web_too_long", False),
        (httpx.Response(200, json=page(status=403)), "web_page_unavailable", False),
        (httpx.Response(200, json=page(status=503)), "web_page_unavailable", True),
        (httpx.Response(200, json={"success": False}), "web_invalid_response", False),
        (
            httpx.Response(200, json={"success": True, "data": {"markdown": "body"}}),
            "web_invalid_response",
            False,
        ),
        (httpx.Response(200, text="not JSON"), "web_invalid_response", False),
        (httpx.Response(200, json=page("null\x00")), "web_invalid_response", False),
        (
            httpx.Response(200, content=b"x" * (web.MAX_RESPONSE_BYTES + 1)),
            "web_response_too_large",
            False,
        ),
    ],
)
def test_failures_are_safe_and_classified(
    response: httpx.Response | Exception, code: str, retryable: bool
) -> None:
    with pytest.raises(jobs.JobError) as caught:
        Site(response).extractor().scrape("https://example.com/post")
    assert (caught.value.code, caught.value.retryable) == (code, retryable)
    assert str(caught.value) == code


def test_config_is_optional_and_disabled_backfill_does_not_touch_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_settings({}).firecrawl_api_key == ""
    assert (
        load_settings({"GATHERLOOM_FIRECRAWL_API_KEY": " key "}).firecrawl_api_key
        == "key"
    )
    monkeypatch.delenv("GATHERLOOM_FIRECRAWL_API_KEY", raising=False)
    assert scheduler.backfill_tick() == 0


def test_snapshot_is_indexed_and_card_uses_page_body(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    saved = link(committed, owner)
    site, models = Site(), FakeAPI()
    assert run(committed, saver, Clock(), site, models=models) == "succeeded"
    with committed() as db:
        revision = db.get_one(EntryRevision, (owner, saved, 1))
        assert revision.client_extract is None
        assert (
            revision.server_extract is not None
            and revision.server_extract["text"] == BODY
        )
        chunks = db.scalars(
            select(Chunk).where(Chunk.entry_id == saved, Chunk.unit == "chunk")
        ).all()
        assert chunks and all(c.source == "extract" for c in chunks)
        for chunk in chunks:
            assert BODY[chunk.start_offset : chunk.end_offset] == chunk.display_text
        assert db.scalar(select(Artifact).where(Artifact.entry_id == saved)) is not None
    assert any(
        "@Observable" in message["content"]
        for _, body in models.requests
        for message in body.get("messages", [])
        if message["role"] == "user"
    )


@pytest.mark.parametrize("client_body", [BODY, " \n\t"])
def test_safari_body_wins_but_whitespace_fetches(
    committed: sessionmaker[Session], saver: PostgresSaver, client_body: str
) -> None:
    saved = link(
        committed, account(committed), {"title": "Safari", "text": client_body}
    )
    site = Site()
    assert run(committed, saver, Clock(), site) == "succeeded"
    assert len(site.requests) == (0 if client_body == BODY else 1)
    with committed() as db:
        source = db.scalars(
            select(EntryRevision).where(EntryRevision.entry_id == saved)
        ).one()
        assert source.client_extract == {"title": "Safari", "text": client_body}


def test_backfill_replaces_metadata_only_after_success_and_does_not_loop(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    saved = link(committed, owner)
    clock, models = Clock(), FakeAPI()
    assert run(committed, saver, clock, None, models=models) == "succeeded"
    with committed() as db:
        old = db.scalars(select(Chunk).where(Chunk.entry_id == saved)).all()
        old_ids = {c.id for c in old}
        old_card = (
            db.scalars(select(Artifact).where(Artifact.entry_id == saved)).one().id
        )
    with committed.begin() as db:
        assert web.enqueue_missing(db, clock()) == 1
    site = Site(httpx.Response(401))
    assert run(committed, saver, clock, site, models=models) == "failed"
    job = job_of(committed, saved)
    assert job.indexed_at is not None and jobs.failed_step(job.error_step) == "extract"
    with committed.begin() as db:
        assert web.enqueue_missing(db, clock()) == 0
        assert {
            c.id for c in db.scalars(select(Chunk).where(Chunk.entry_id == saved))
        } == old_ids
        assert db.get(Artifact, old_card) is not None
        db.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(status="queued", generation=Job.generation + 1, attempts=0)
        )
    site.response = httpx.Response(200, json=page())
    assert run(committed, saver, clock, site, models=models) == "succeeded"
    with committed.begin() as db:
        assert web.enqueue_missing(db, clock()) == 0
        assert not old_ids.intersection(
            c.id for c in db.scalars(select(Chunk).where(Chunk.entry_id == saved))
        )
        assert db.get(Artifact, old_card) is None
    assert len(site.requests) == 2


def test_crash_after_snapshot_commit_reuses_snapshot(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    saved = link(committed, account(committed))
    site, clock, hooks = Site(), Clock(), Hooks()
    hooks.failures["index_keywords"] = Crash()
    with pytest.raises(Crash):
        run(committed, saver, clock, site, hooks)
    clock.advance(jobs.LEASE * 2)
    assert run(committed, saver, clock, site, hooks) == "succeeded"
    assert len(site.requests) == 1
    assert job_of(committed, saved).attempts == 2


@pytest.mark.parametrize("status", [429, 503])
def test_temporary_failures_stop_after_three_attempts(
    committed: sessionmaker[Session], saver: PostgresSaver, status: int
) -> None:
    saved = link(committed, account(committed))
    site, clock = Site(httpx.Response(status)), Clock()
    outcomes = []
    for _ in range(3):
        outcomes.append(run(committed, saver, clock, site))
        clock.advance(timedelta(hours=1))
    assert outcomes == ["queued", "queued", "failed"]
    assert job_of(committed, saved).attempts == 3
    with committed.begin() as db:
        assert web.enqueue_missing(db, clock()) == 0


@pytest.mark.parametrize("action", ["delete", "edit"])
def test_result_cannot_publish_after_delete_or_edit(
    committed: sessionmaker[Session], saver: PostgresSaver, action: str
) -> None:
    owner = account(committed)
    saved = link(committed, owner)
    site = Site()

    def change(point: str) -> None:
        if point != "extract:received":
            return
        with committed.begin() as db:
            source = db.get_one(Entry, saved)
            if action == "delete":
                deletion.mark_entry_deleted(db, source, jobs.utcnow())
            else:
                db.add(
                    EntryRevision(
                        owner_id=owner,
                        entry_id=saved,
                        revision=2,
                        text="",
                        note="修改",
                        content_fingerprint="f" * 64,
                    )
                )
                source.current_revision = 2

    current = claim(committed, "web", jobs.utcnow())
    outcome = Pipeline(
        committed, saver, "web", extractor=site.extractor(), hook=change
    ).run(current)
    assert outcome in ("lost", "cancelled")
    with committed() as db:
        assert db.get_one(EntryRevision, (owner, saved, 1)).server_extract is None
        assert not db.scalars(select(Chunk).where(Chunk.entry_id == saved)).all()


def test_backfill_two_scanners_are_bounded_and_exclusive(
    committed: sessionmaker[Session],
) -> None:
    owner = account(committed)
    for _ in range(55):
        link(committed, owner)
    active = link(committed, owner)
    captured = link(committed, owner, {"text": BODY})
    deleted = link(committed, owner)
    with committed.begin() as db:
        db.execute(
            update(Job)
            .values(status="succeeded")
            .where(Job.owner_id == owner, Job.entry_id != active)
        )
        db.execute(
            update(Entry).where(Entry.id == deleted).values(deleted_at=jobs.utcnow())
        )
    barrier = threading.Barrier(2)
    counts: list[int] = []

    def scan() -> None:
        barrier.wait()
        with committed.begin() as db:
            counts.append(web.enqueue_missing(db, jobs.utcnow()))

    threads = [threading.Thread(target=scan) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(counts) == [5, 50]
    with committed.begin() as db:
        assert web.enqueue_missing(db, jobs.utcnow()) == 0
        for saved in (active, captured, deleted):
            assert (
                db.scalars(select(Job).where(Job.entry_id == saved)).one().payload
                is None
            )
        # An old active job that finished without the new node is picked up later.
        db.execute(update(Job).where(Job.entry_id == active).values(status="succeeded"))
    with committed.begin() as db:
        assert web.enqueue_missing(db, jobs.utcnow()) == 1


def test_web_body_answers_with_exact_snapshot_citation(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    project_id = project(committed, owner, "网页资料")
    saved = link(committed, owner)
    with committed.begin() as db:
        db.add(ProjectEntry(owner_id=owner, project_id=project_id, entry_id=saved))
    models, clock = FakeAPI(), Clock()
    assert run(committed, saver, clock, Site(), models=models) == "succeeded"
    asked = question(committed, owner, project_id, "SwiftUI 状态管理用什么？")
    models.chat.append(reply("使用 @Observable 宏 [1]。", [1]))
    assert answer_with(committed, saver, models, clock) == "succeeded"
    [answer] = answers(committed, asked)
    assert answer.content == "使用 @Observable 宏 [1]。" and not answer.insufficient
    with committed() as db:
        [citation] = db.scalars(
            select(Citation).where(Citation.message_id == answer.id)
        )
        assert citation.entry_id == saved and citation.source == "extract"
        assert citation.snippet == BODY[citation.start_offset : citation.end_offset]
