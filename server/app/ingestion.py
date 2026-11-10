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
from itertools import pairwise
from typing import Any, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app import cards, chunking, jobs, tokenizer
from app.models import INDEX_VERSION, Artifact, Chunk, Entry, EntryRevision, Job, Run
from app.providers import Call, Models

log = logging.getLogger("gatherloom.ingestion")
EMBED_BATCH = 25
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


def artifact_rows(claim: jobs.Claim) -> Any:
    return (
        Artifact.owner_id == claim.owner_id,
        Artifact.entry_id == claim.entry_id,
        Artifact.revision == claim.revision,
        Artifact.kind == "card",
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
        models: Models | None = None,
        clock: Callable[[], datetime] = jobs.utcnow,
        hook: Hook | None = None,
    ) -> None:
        """Without models the graph builds the keyword index only."""
        self.sessions = sessions
        self.models = models
        self.checkpointer = checkpointer
        self.worker_id = worker_id
        self.clock = clock
        self.hook = hook or (lambda _point: None)
        self.claim: jobs.Claim | None = None
        self.last_run: UUID | None = None
        graph = StateGraph(IngestionState)
        graph.add_node("load", self.load)
        graph.add_node("index_keywords", self.index_keywords)
        graph.add_node("finish", self.finish)
        steps = ["load", "index_keywords"]
        if models is not None:
            graph.add_node("embed", self.embed)
            graph.add_node("card", self.card)
            graph.add_node("card_index", self.card_index)
            steps += ["embed", "card", "card_index"]
        steps.append("finish")
        graph.add_edge(START, steps[0])
        for current, following in pairwise(steps):
            graph.add_edge(current, following)
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
            if self.models is None and job.indexed_at is None:
                job.indexed_at = self.clock()
        return {}

    def embed(self, state: IngestionState) -> dict[str, Any]:
        """Embed source chunks batch by batch; finished batches survive a crash.

        Completing this step makes the entry searchable ("已可检索").
        """
        self.hook("embed")
        assert self.claim is not None and self.models is not None
        claim, embedder = self.claim, self.models.embedder
        while True:
            with self.step("embed") as (db, _job):
                pending = db.execute(
                    select(Chunk.id, Chunk.index_text)
                    .where(*chunk_rows(claim, "chunk"), Chunk.embedding.is_(None))
                    .order_by(Chunk.ordinal)
                    .limit(EMBED_BATCH)
                ).all()
            if not pending:
                break
            vectors = embedder.embed(
                [text for _id, text in pending], "embed", self.record
            )
            with self.step("embed") as (db, _job):
                for (chunk_id, _text), vector in zip(pending, vectors, strict=True):
                    db.execute(
                        update(Chunk)
                        .where(Chunk.id == chunk_id, Chunk.embedding.is_(None))
                        .values(embedding=vector)
                    )
        with self.step("embed") as (_db, job):
            if job.indexed_at is None:
                job.indexed_at = self.clock()
        return {}

    def card(self, state: IngestionState) -> dict[str, Any]:
        """Generate the knowledge card once per revision."""
        self.hook("card")
        assert self.claim is not None and self.models is not None
        claim = self.claim
        with self.step("card") as (db, _job):
            if db.scalar(select(exists().where(*artifact_rows(claim)))):
                return {}
            source = material(db, claim)
            chunks = [
                (ordinal, text)
                for ordinal, text in db.execute(
                    select(Chunk.ordinal, Chunk.display_text)
                    .where(*chunk_rows(claim, "chunk"))
                    .order_by(Chunk.ordinal)
                )
            ]
        # The model call happens outside any transaction or row lock.
        card, route = cards.generate(self.models.router, source, chunks, self.record)
        run_id = self.last_run
        with self.step("card") as (db, _job):
            db.execute(
                insert(Artifact)
                .values(
                    owner_id=claim.owner_id,
                    entry_id=claim.entry_id,
                    revision=claim.revision,
                    kind="card",
                    content={**card.model_dump(), "model": route.model},
                    prompt_version=cards.PROMPT_VERSION,
                    run_id=run_id,
                )
                .on_conflict_do_nothing(constraint="uq_artifacts_kind")
            )
        return {}

    def card_index(self, state: IngestionState) -> dict[str, Any]:
        """Index the card as one entry-level retrieval unit (ADR 0009 R2)."""
        self.hook("card_index")
        assert self.claim is not None and self.models is not None
        claim = self.claim
        with self.step("card_index") as (db, _job):
            if db.scalar(select(exists().where(*chunk_rows(claim, "card")))):
                return {}
            content = db.scalar(select(Artifact.content).where(*artifact_rows(claim)))
        if content is None:
            raise jobs.Permanent("card_missing")
        text = cards.Card.model_validate(
            {key: value for key, value in content.items() if key != "model"}
        ).index_text()
        [vector] = self.models.embedder.embed([text], "embed_card", self.record)
        with self.step("card_index") as (db, _job):
            db.execute(
                insert(Chunk)
                .values(
                    owner_id=claim.owner_id,
                    entry_id=claim.entry_id,
                    revision=claim.revision,
                    unit="card",
                    ordinal=0,
                    source="card",
                    start_offset=0,
                    end_offset=0,
                    display_text=text,
                    index_text=text,
                    tokens=func.to_tsvector("simple", tokenizer.index_text(text)),
                    embedding=vector,
                    context_header_version=chunking.CONTEXT_HEADER_VERSION,
                    tokenizer_version=tokenizer.TOKENIZER_VERSION,
                    index_version=INDEX_VERSION,
                )
                .on_conflict_do_nothing(constraint="uq_chunks_unit")
            )
        return {}

    def record(self, call: Call) -> None:
        """Store one model call's usage and cost, whatever the outcome."""
        assert self.claim is not None
        cost, price = call.cost()
        with self.sessions.begin() as db:
            run = Run(
                owner_id=self.claim.owner_id,
                job_id=self.claim.id,
                task=call.task,
                provider=call.provider,
                model=call.model,
                prompt_version=cards.PROMPT_VERSION
                if call.task == cards.TASK
                else None,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cost_estimate=cost,
                currency=price.currency if price else None,
                price_date=price.checked if price else None,
                latency_ms=call.latency_ms,
                error_code=call.error_code,
            )
            db.add(run)
            db.flush()
            self.last_run = run.id
        log.info(
            "model call",
            extra={
                "job_id": str(self.claim.id),
                "stage": call.task,
                "latency_ms": call.latency_ms,
                "error_code": call.error_code,
                "model": call.model,
            },
        )

    def finish(self, state: IngestionState) -> dict[str, Any]:
        self.hook("finish")
        with self.step("done") as (_db, job):
            jobs.settle(job, "succeeded", self.clock())
        return {}


def delete_entry_chunks(db: Session, owner_id: UUID, entry_id: UUID) -> None:
    db.execute(
        delete(Chunk).where(Chunk.owner_id == owner_id, Chunk.entry_id == entry_id)
    )
