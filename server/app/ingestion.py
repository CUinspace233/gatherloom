"""EntryIngestionGraph: turn a saved entry revision into searchable chunks.

The graph state holds only identifiers; nodes read content from the domain
tables, so checkpoints never contain entry text (ADR 0006). Each node writes
through ``jobs.fence`` and is idempotent, because a node can run again after a
crash between its commit and the checkpoint write.
"""

from itertools import pairwise
from typing import Any, ClassVar, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import cards, chunking, jobs, tokenizer
from app.graphs import Hook, JobPipeline
from app.models import INDEX_VERSION, Artifact, Chunk, Entry, EntryRevision

__all__ = ["Hook", "Pipeline", "delete_entry_chunks"]
EMBED_BATCH = 25


class IngestionState(TypedDict):
    job_id: str
    owner_id: str
    entry_id: str
    revision: int


def initial_state(claim: jobs.Claim) -> IngestionState:
    assert claim.entry_id is not None and claim.revision is not None
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


class Pipeline(JobPipeline):
    """Ingestion jobs. Without models the graph builds the keyword index only."""

    PROMPT_VERSIONS: ClassVar[dict[str, int]] = {cards.TASK: cards.PROMPT_VERSION}

    def initial_state(self, claim: jobs.Claim) -> dict[str, Any]:
        return dict(initial_state(claim))

    def build(self) -> StateGraph[Any]:
        graph = StateGraph(IngestionState)
        graph.add_node("load", self.load)
        graph.add_node("index_keywords", self.index_keywords)
        graph.add_node("finish", self.finish)
        steps = ["load", "index_keywords"]
        if self.models is not None:
            graph.add_node("embed", self.embed)
            graph.add_node("card", self.card)
            graph.add_node("card_index", self.card_index)
            steps += ["embed", "card", "card_index"]
        steps.append("finish")
        graph.add_edge(START, steps[0])
        for current, following in pairwise(steps):
            graph.add_edge(current, following)
        graph.add_edge("finish", END)
        return graph

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


def delete_entry_chunks(db: Session, owner_id: UUID, entry_id: UUID) -> None:
    db.execute(
        delete(Chunk).where(Chunk.owner_id == owner_id, Chunk.entry_id == entry_id)
    )
