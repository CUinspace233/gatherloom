"""EntryIngestionGraph: turn a saved entry revision into searchable chunks.

The graph state holds only identifiers; nodes read content from the domain
tables, so checkpoints never contain entry text (ADR 0006). Each node writes
through ``jobs.fence`` and is idempotent, because a node can run again after a
crash between its commit and the checkpoint write.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app import chunking, jobs, tokenizer
from app.models import INDEX_VERSION, Chunk, Entry, EntryRevision, Job

log = logging.getLogger("gatherloom.ingestion")
Hook = Callable[[str], None]


class IngestionState(TypedDict):
    job_id: str
    owner_id: str
    entry_id: str
    revision: int


def initial_state(claim: jobs.Claim) -> IngestionState:
    return {
        "job_id": str(claim.id),
        "owner_id": str(claim.owner_id),
        "entry_id": str(claim.entry_id),
        "revision": claim.revision,
    }


def material(db: Session, claim: jobs.Claim) -> chunking.Material:
    entry = db.get_one(Entry, claim.entry_id)
    revision = db.get_one(
        EntryRevision, (claim.owner_id, claim.entry_id, claim.revision)
    )
    return chunking.Material(
        type=entry.type,
        text=revision.text,
        note=revision.note,
        source_url=entry.source_url,
        client_extract=revision.client_extract,
        client_ocr=revision.client_ocr,
    )


def chunk_rows(claim: jobs.Claim, unit: str) -> Any:
    return (
        Chunk.owner_id == claim.owner_id,
        Chunk.entry_id == claim.entry_id,
        Chunk.revision == claim.revision,
        Chunk.unit == unit,
    )


class Pipeline:
    """Runs one claimed job through the graph. One instance per worker thread."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        checkpointer: BaseCheckpointSaver[Any],
        worker_id: str,
        *,
        clock: Callable[[], datetime] = jobs.utcnow,
        hook: Hook | None = None,
    ) -> None:
        self.sessions = sessions
        self.checkpointer = checkpointer
        self.worker_id = worker_id
        self.clock = clock
        self.hook = hook or (lambda _point: None)
        self.claim: jobs.Claim | None = None
        graph = StateGraph(IngestionState)
        graph.add_node("load", self.load)
        graph.add_node("index_keywords", self.index_keywords)
        graph.add_node("finish", self.finish)
        graph.add_edge(START, "load")
        graph.add_edge("load", "index_keywords")
        graph.add_edge("index_keywords", "finish")
        graph.add_edge("finish", END)
        self.graph = graph.compile(checkpointer=checkpointer)

    # Running

    def run(self, claim: jobs.Claim) -> str:
        """Execute or resume the job. Returns its resulting status."""
        self.claim = claim
        config: RunnableConfig = {"configurable": {"thread_id": claim.thread_id}}
        started = self.clock()
        try:
            pending = self.graph.get_state(config).next
            self.graph.invoke(
                None if pending else initial_state(claim), config, durability="sync"
            )
            status = "succeeded"
        except jobs.Cancelled:
            with self.sessions() as db:
                jobs.record_cancelled(db, claim, self.worker_id, self.clock())
            status = "cancelled"
        except jobs.LeaseLost:
            # The new owner (or the deletion) decides what happens next.
            status = "lost"
        except Exception as error:  # noqa: BLE001 - recorded as a job failure
            failure = (
                error
                if isinstance(error, jobs.JobError)
                else jobs.Retryable("internal_error", type(error).__name__)
            )
            try:
                with self.sessions() as db:
                    status = jobs.record_failure(
                        db, claim, self.worker_id, failure, self.clock()
                    )
            except jobs.LeaseLost:
                status = "lost"
            log.warning(
                "job attempt failed",
                extra={
                    "job_id": str(claim.id),
                    "error_code": failure.code,
                    "error_type": type(error).__name__,
                },
            )
        if status in ("succeeded", "cancelled", "failed"):
            self.checkpointer.delete_thread(claim.thread_id)
        log.info(
            "job attempt finished",
            extra={
                "job_id": str(claim.id),
                "status": status,
                "latency_ms": int((self.clock() - started).total_seconds() * 1000),
            },
        )
        return status

    @contextmanager
    def step(self, stage: str) -> Iterator[tuple[Session, Job]]:
        """A fenced transaction that records the current stage."""
        assert self.claim is not None
        with self.sessions() as db:
            job = jobs.fence(db, self.claim, self.worker_id)
            job.stage = stage
            job.updated_at = self.clock()
            yield db, job
            self.hook(f"{stage}:commit")
            db.commit()

    # Nodes

    def load(self, state: IngestionState) -> dict[str, Any]:
        self.hook("load")
        with self.step("load"):
            pass
        return {}

    def index_keywords(self, state: IngestionState) -> dict[str, Any]:
        """Chunk the revision and write keyword-indexed rows in one transaction."""
        self.hook("index_keywords")
        assert self.claim is not None
        claim = self.claim
        with self.step("chunk") as (db, job):
            done = db.scalar(
                select(
                    exists().where(
                        *chunk_rows(claim, "chunk"),
                        Chunk.context_header_version == chunking.CONTEXT_HEADER_VERSION,
                        Chunk.tokenizer_version == tokenizer.TOKENIZER_VERSION,
                        Chunk.index_version == INDEX_VERSION,
                    )
                )
            )
            if not done:
                db.execute(delete(Chunk).where(*chunk_rows(claim, "chunk")))
                drafts = chunking.chunk_material(material(db, claim))
                if drafts:
                    db.execute(
                        insert(Chunk).values(
                            [
                                {
                                    "owner_id": claim.owner_id,
                                    "entry_id": claim.entry_id,
                                    "revision": claim.revision,
                                    "unit": "chunk",
                                    "ordinal": draft.ordinal,
                                    "source": draft.source,
                                    "start_offset": draft.start,
                                    "end_offset": draft.end,
                                    "display_text": draft.display_text,
                                    "index_text": draft.index_text,
                                    "tokens": func.to_tsvector(
                                        "simple", tokenizer.index_text(draft.index_text)
                                    ),
                                    "context_header_version": chunking.CONTEXT_HEADER_VERSION,
                                    "tokenizer_version": tokenizer.TOKENIZER_VERSION,
                                    "index_version": INDEX_VERSION,
                                }
                                for draft in drafts
                            ]
                        )
                    )
            if job.indexed_at is None:
                job.indexed_at = self.clock()
        return {}

    def finish(self, state: IngestionState) -> dict[str, Any]:
        self.hook("finish")
        with self.step("done") as (_db, job):
            jobs.settle(job, "succeeded", self.clock())
        return {}


def delete_entry_chunks(db: Session, owner_id: UUID, entry_id: UUID) -> None:
    db.execute(
        delete(Chunk).where(Chunk.owner_id == owner_id, Chunk.entry_id == entry_id)
    )
