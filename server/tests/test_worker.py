"""[T4] Worker leases, crash recovery and retries for entry ingestion."""

import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app import chunking, jobs
from app.ingestion import Pipeline
from app.models import Chunk, Entry, EntryRevision, Job, User

LONG_TEXT = "\n\n".join(
    "".join(f"第{p}段第{s}句记录了离线收集的可靠性要求。" for s in range(10))
    for p in range(6)
)


class Crash(BaseException):
    """Stands in for the process dying: not handled as a job failure."""


class Clock:
    def __init__(self) -> None:
        self.now = jobs.utcnow()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> datetime:
        self.now += delta
        return self.now


class Hooks:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.failures: dict[str, BaseException] = {}

    def __call__(self, point: str) -> None:
        self.calls[point] += 1
        if (error := self.failures.pop(point, None)) is not None:
            raise error


def account(sessions: sessionmaker[Session]) -> UUID:
    with sessions.begin() as db:
        user = User(username=f"worker_{uuid4().hex[:12]}", password_hash="unused")
        db.add(user)
        db.flush()
        return user.id


def entry(sessions: sessionmaker[Session], owner: UUID, body: str = LONG_TEXT) -> UUID:
    with sessions.begin() as db:
        saved = Entry(owner_id=owner, client_submission_id=uuid4(), type="thought")
        db.add(saved)
        db.flush()
        db.add(
            EntryRevision(
                owner_id=owner,
                entry_id=saved.id,
                revision=1,
                text=body,
                note="可靠性笔记",
                content_fingerprint=sha256(body.encode()).hexdigest(),
            )
        )
        db.flush()
        db.add(Job(owner_id=owner, entry_id=saved.id, revision=1, kind=jobs.INGESTION))
        return saved.id


def job_of(sessions: sessionmaker[Session], entry_id: UUID) -> Job:
    with sessions() as db:
        return db.scalars(select(Job).where(Job.entry_id == entry_id)).one()


def chunk_count(sessions: sessionmaker[Session], entry_id: UUID) -> int:
    with sessions() as db:
        return (
            db.scalar(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.entry_id == entry_id)
            )
            or 0
        )


def claim(sessions: sessionmaker[Session], worker: str, now: datetime) -> jobs.Claim:
    with sessions() as db:
        result = jobs.claim(db, worker, now)
    assert result is not None
    return result


def checkpoint_rows(sessions: sessionmaker[Session], claim: jobs.Claim) -> int:
    with sessions() as db:
        return (
            db.scalar(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread"),
                {"thread": claim.thread_id},
            )
            or 0
        )


@contextmanager
def pipeline(
    sessions: sessionmaker[Session],
    saver: PostgresSaver,
    worker: str,
    clock: Clock,
    hooks: Hooks,
) -> Iterator[Pipeline]:
    yield Pipeline(sessions, saver, worker, clock=clock, hook=hooks)


def expected_chunks() -> int:
    return len(
        chunking.chunk_material(
            chunking.Material(type="thought", text=LONG_TEXT, note="可靠性笔记")
        )
    )


def test_concurrent_claims_are_exclusive_and_one_per_account(
    committed: sessionmaker[Session],
) -> None:
    owners = [account(committed) for _ in range(4)]
    for owner in owners:
        entry(committed, owner)
        entry(committed, owner)
    now = jobs.utcnow()
    barrier = threading.Barrier(8)
    claims: list[jobs.Claim | None] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        with committed() as db:
            result = jobs.claim(db, f"w{index}", now)
        with lock:
            claims.append(result)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    taken = [c for c in claims if c is not None]
    assert len({c.id for c in taken}) == len(taken)
    assert sorted(c.owner_id for c in taken) == sorted(owners)
    assert all(c.attempts == 1 for c in taken)


def test_crash_between_steps_resumes_from_checkpoint_without_redoing(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    entry_id = entry(committed, owner)
    clock, hooks = Clock(), Hooks()
    hooks.failures["finish"] = Crash()
    first = claim(committed, "first", clock())
    with (
        pipeline(committed, saver, "first", clock, hooks) as worker,
        pytest.raises(Crash),
    ):
        worker.run(first)
    assert job_of(committed, entry_id).status == "running"
    assert chunk_count(committed, entry_id) == expected_chunks()
    assert checkpoint_rows(committed, first) > 0
    # The checkpoint holds identifiers only, never the entry's text.
    with committed() as db:
        stored = db.execute(
            text(
                "SELECT checkpoint::text FROM checkpoints WHERE thread_id = :t "
                "UNION ALL SELECT encode(blob, 'escape') FROM checkpoint_blobs "
                "WHERE thread_id = :t AND blob IS NOT NULL"
            ),
            {"t": first.thread_id},
        ).scalars()
        assert not any("可靠性" in (value or "") for value in stored)

    with committed() as db:
        assert jobs.claim(db, "second", clock()) is None  # lease still valid
    second = claim(committed, "second", clock.advance(jobs.LEASE * 2))
    assert (second.id, second.attempts) == (first.id, 2)
    with pipeline(committed, saver, "second", clock, hooks) as worker:
        assert worker.run(second) == "succeeded"

    assert hooks.calls["index_keywords"] == 1
    assert hooks.calls["load"] == 1
    job = job_of(committed, entry_id)
    assert (job.status, job.stage, job.lease_owner) == ("succeeded", "done", None)
    assert job.indexed_at is not None
    assert chunk_count(committed, entry_id) == expected_chunks()
    assert checkpoint_rows(committed, second) == 0


def test_crash_inside_index_transaction_leaves_no_partial_chunks(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    entry_id = entry(committed, owner)
    clock, hooks = Clock(), Hooks()
    hooks.failures["chunk:commit"] = Crash()
    first = claim(committed, "first", clock())
    with (
        pipeline(committed, saver, "first", clock, hooks) as worker,
        pytest.raises(Crash),
    ):
        worker.run(first)
    assert chunk_count(committed, entry_id) == 0
    assert job_of(committed, entry_id).indexed_at is None

    second = claim(committed, "second", clock.advance(jobs.LEASE * 2))
    with pipeline(committed, saver, "second", clock, hooks) as worker:
        assert worker.run(second) == "succeeded"
    assert hooks.calls["load"] == 1
    assert hooks.calls["index_keywords"] == 2
    assert chunk_count(committed, entry_id) == expected_chunks()


def test_worker_that_lost_its_lease_cannot_write(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    entry_id = entry(committed, owner)
    clock, hooks = Clock(), Hooks()
    stale = claim(committed, "stale", clock())
    current = claim(committed, "current", clock.advance(jobs.LEASE * 2))
    with pipeline(committed, saver, "stale", clock, hooks) as worker:
        assert worker.run(stale) == "lost"
    assert chunk_count(committed, entry_id) == 0
    job = job_of(committed, entry_id)
    assert (job.status, job.lease_owner) == ("running", "current")
    with committed() as db:
        assert not jobs.heartbeat(db, stale, "stale", clock())
        assert jobs.heartbeat(db, current, "current", clock())
    with pipeline(committed, saver, "current", clock, hooks) as worker:
        assert worker.run(current) == "succeeded"
    assert chunk_count(committed, entry_id) == expected_chunks()


def test_retryable_errors_back_off_and_stop_after_three_attempts(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    entry_id = entry(committed, owner)
    clock, hooks = Clock(), Hooks()
    waits = []
    for attempt in range(1, 4):
        hooks.failures["index_keywords"] = jobs.Retryable("network")
        current = claim(committed, f"w{attempt}", clock())
        assert current.attempts == attempt
        with pipeline(committed, saver, f"w{attempt}", clock, hooks) as worker:
            status = worker.run(current)
        job = job_of(committed, entry_id)
        if attempt < 3:
            assert status == "queued"
            waits.append(job.run_after - clock())
            with committed() as db:
                assert jobs.claim(db, "early", clock()) is None
            clock.now = job.run_after
        else:
            assert status == "failed"
    assert waits == [timedelta(seconds=30), timedelta(seconds=120)]
    job = job_of(committed, entry_id)
    assert (job.status, job.error_code, job.error_step) == ("failed", "network", "load")
    assert jobs.failed_step(job.error_step) == "index"
    assert checkpoint_rows(committed, current) == 0


def test_permanent_error_fails_at_once_and_manual_retry_completes(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    entry_id = entry(committed, owner)
    clock, hooks = Clock(), Hooks()
    hooks.failures["finish"] = jobs.Permanent("invalid_output")
    first = claim(committed, "w", clock())
    with pipeline(committed, saver, "w", clock, hooks) as worker:
        assert worker.run(first) == "failed"
    job = job_of(committed, entry_id)
    assert job.indexed_at is not None
    assert jobs.processing_status(job.status, job.indexed_at) == "partial"

    # What POST /v1/entries/{id}/retry does.
    with committed.begin() as db:
        db.execute(
            update(Job)
            .where(Job.id == first.id)
            .values(status="queued", attempts=0, generation=Job.generation + 1)
        )
    retry = claim(committed, "w", clock())
    assert retry.generation == 1 and retry.thread_id != first.thread_id
    with pipeline(committed, saver, "w", clock, hooks) as worker:
        assert worker.run(retry) == "succeeded"
    # Chunks written by the first attempt are reused, not rebuilt.
    assert chunk_count(committed, entry_id) == expected_chunks()


def test_deleted_or_replaced_entries_stop_processing(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    replaced = entry(committed, owner, "第一版")
    clock, hooks = Clock(), Hooks()
    current = claim(committed, "w", clock())
    with committed.begin() as db:
        db.execute(update(Entry).where(Entry.id == replaced).values(current_revision=2))
    with pipeline(committed, saver, "w", clock, hooks) as worker:
        assert worker.run(current) == "cancelled"
    assert job_of(committed, replaced).status == "cancelled"
    assert chunk_count(committed, replaced) == 0

    deleted = entry(committed, owner, "将被删除")
    current = claim(committed, "w", clock.advance(timedelta(seconds=5)))
    with committed.begin() as db:
        db.execute(
            update(Job)
            .where(Job.id == current.id)
            .values(status="cancelled", lease_owner=None)
        )
    with pipeline(committed, saver, "w", clock, hooks) as worker:
        assert worker.run(current) == "lost"
    assert chunk_count(committed, deleted) == 0


def test_last_attempt_lost_to_a_crash_is_not_retried_again(
    committed: sessionmaker[Session],
) -> None:
    owner = account(committed)
    entry_id = entry(committed, owner)
    with committed.begin() as db:
        db.execute(
            update(Job)
            .where(Job.entry_id == entry_id)
            .values(
                status="running",
                attempts=jobs.MAX_ATTEMPTS,
                lease_owner="dead",
                lease_expires_at=jobs.utcnow() - timedelta(seconds=1),
                stage="chunk",
            )
        )
    with committed() as db:
        assert jobs.claim(db, "w", jobs.utcnow()) is None
    job = job_of(committed, entry_id)
    assert (job.status, job.error_code, job.error_step) == (
        "failed",
        "worker_lost",
        "chunk",
    )
