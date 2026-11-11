"""Hybrid retrieval over an account's chunks (ADR 0005, 0009).

Two paths rank chunk rows of the current revision of live entries:

- keyword: jieba tokens OR-ed into a ``simple`` tsquery, ranked by ts_rank_cd;
- semantic: the query embedding against chunk embeddings, exact cosine order.

Both include source chunks and knowledge-card units. Reciprocal rank fusion
combines them, card units weighted by ``card_weight``; results are grouped by
entry and each entry is represented by its best source chunk, because answers
may only cite source text, never cards.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, exists, func, select
from sqlalchemy.orm import Session

from app import tokenizer
from app.jobs import JobError
from app.models import Chunk, Entry, ProjectEntry
from app.providers import Embedder, Recorder

CANDIDATES = 50
# Search drops semantic matches farther than this cosine distance, so unrelated
# queries do not list every entry; keyword matches are kept regardless. Measured
# with qwen3.7-text-embedding-flash: related queries ≈ 0.40, unrelated ≥ 0.60
# (docs/evaluation/results). Answers do not use it; the model judges sufficiency.
SEARCH_MAX_DISTANCE = 0.55
RRF_K = 60
SNIPPET_CHARS = 160
SNIPPET_LEAD = 40
# Words so common in Chinese questions that OR-matching them floods the
# keyword path. Only the query side drops them; the index keeps everything.
STOPWORDS = frozenset(
    [
        "的",
        "了",
        "是",
        "在",
        "和",
        "与",
        "及",
        "或",
        "我",
        "你",
        "他",
        "她",
        "它",
        "我们",
        "你们",
        "有",
        "没有",
        "什么",
        "怎么",
        "怎样",
        "如何",
        "为什么",
        "吗",
        "呢",
        "吧",
        "啊",
        "哪",
        "哪些",
        "哪个",
        "这",
        "那",
        "这个",
        "那个",
        "一个",
        "一些",
        "就",
        "都",
        "也",
        "还",
        "要",
        "会",
        "能",
        "可以",
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "for",
        "and",
        "or",
        "is",
        "are",
        "what",
        "how",
        "why",
        "which",
    ]
)


@dataclass(frozen=True)
class Filters:
    project_id: UUID | None = None
    types: Sequence[str] = ()
    since: datetime | None = None
    until: datetime | None = None


NO_FILTERS = Filters()


@dataclass
class Hit:
    entry_id: UUID
    score: float
    chunk_id: UUID
    source: str
    display_text: str
    start_offset: int
    matched_by: set[str] = field(default_factory=set)


@dataclass
class SearchResult:
    hits: list[Hit]
    degraded: bool
    terms: list[str]


def query_terms(query: str) -> list[str]:
    """Distinct search tokens of the query, without stopwords or punctuation."""
    terms: list[str] = []
    for token in tokenizer.tokenize(query):
        term = re.sub(r"\W", "", token)
        if term and term not in STOPWORDS and term not in terms:
            terms.append(term)
    return terms


def scope(owner_id: UUID, filters: Filters) -> list[ColumnElement[bool]]:
    """Conditions on Chunk joined to its live entry at the current revision."""
    conditions: list[ColumnElement[bool]] = [
        Chunk.owner_id == owner_id,
        Entry.deleted_at.is_(None),
    ]
    if filters.project_id is not None:
        conditions.append(
            exists().where(
                ProjectEntry.owner_id == owner_id,
                ProjectEntry.project_id == filters.project_id,
                ProjectEntry.entry_id == Chunk.entry_id,
            )
        )
    if filters.types:
        conditions.append(Entry.type.in_(filters.types))
    if filters.since is not None:
        conditions.append(Entry.created_at >= filters.since)
    if filters.until is not None:
        conditions.append(Entry.created_at < filters.until)
    return conditions


def chunk_rows() -> Any:
    return select(Chunk.id, Chunk.entry_id, Chunk.unit).join(
        Entry,
        and_(
            Entry.owner_id == Chunk.owner_id,
            Entry.id == Chunk.entry_id,
            Entry.current_revision == Chunk.revision,
        ),
    )


def keyword_ranking(
    db: Session, conditions: list[ColumnElement[bool]], terms: list[str]
) -> list[tuple[UUID, UUID, str]]:
    if not terms:
        return []
    query = func.to_tsquery("simple", " | ".join(terms))
    return [
        tuple(row)
        for row in db.execute(
            chunk_rows()
            .where(*conditions, Chunk.tokens.bool_op("@@")(query))
            .order_by(func.ts_rank_cd(Chunk.tokens, query).desc(), Chunk.id)
            .limit(CANDIDATES)
        )
    ]


def semantic_ranking(
    db: Session,
    conditions: list[ColumnElement[bool]],
    vector: list[float],
    max_distance: float | None,
) -> list[tuple[UUID, UUID, str]]:
    distance = Chunk.embedding.cosine_distance(vector)
    if max_distance is not None:
        conditions = [*conditions, distance <= max_distance]
    return [
        tuple(row)
        for row in db.execute(
            chunk_rows()
            .where(*conditions, Chunk.embedding.is_not(None))
            .order_by(distance, Chunk.id)
            .limit(CANDIDATES)
        )
    ]


def retrieve(
    db: Session,
    owner_id: UUID,
    query: str,
    *,
    filters: Filters = NO_FILTERS,
    embedder: Embedder | None,
    record: Recorder,
    card_weight: float = 1.0,
    max_distance: float | None = None,
    paths: Sequence[str] = ("keyword", "semantic"),
) -> SearchResult:
    """Rank the account's entries for a query.

    max_distance drops semantic matches farther than this cosine distance;
    without it the semantic path always returns its top candidates.
    """
    conditions = scope(owner_id, filters)
    terms = query_terms(query)
    rankings: dict[str, list[tuple[UUID, UUID, str]]] = {}
    if "keyword" in paths:
        rankings["keyword"] = keyword_ranking(db, conditions, terms)
    degraded = False
    if "semantic" in paths:
        vector = None
        if embedder is not None:
            try:
                [vector] = embedder.embed([query], "search_embed", record)
            except JobError:
                vector = None
        if vector is None:
            degraded = True
        else:
            rankings["semantic"] = semantic_ranking(
                db, conditions, vector, max_distance
            )

    scores: dict[UUID, float] = defaultdict(float)
    units: dict[UUID, tuple[UUID, str]] = {}
    first_rank: dict[UUID, int] = {}
    matched: dict[UUID, set[str]] = defaultdict(set)
    for path, rows in rankings.items():
        for rank, (chunk_id, entry_id, unit) in enumerate(rows, start=1):
            units[chunk_id] = (entry_id, unit)
            if unit == "chunk":
                first_rank[chunk_id] = min(first_rank.get(chunk_id, rank), rank)
            weight = card_weight if unit == "card" else 1.0
            if weight <= 0:
                continue
            scores[chunk_id] += weight / (RRF_K + rank)
            matched[entry_id].add("card" if unit == "card" else path)

    entry_scores: dict[UUID, float] = defaultdict(float)
    best_chunk: dict[UUID, tuple[float, UUID]] = {}
    for chunk_id, score in scores.items():
        entry_id, unit = units[chunk_id]
        entry_scores[entry_id] = max(entry_scores[entry_id], score)
        if unit == "chunk" and score > best_chunk.get(entry_id, (-1.0, chunk_id))[0]:
            best_chunk[entry_id] = (score, chunk_id)
    # Card-only hits point to the entry's best-ranked source chunk, if any.
    for chunk_id, rank in sorted(first_rank.items(), key=lambda item: item[1]):
        entry_id = units[chunk_id][0]
        if entry_id in entry_scores and entry_id not in best_chunk:
            best_chunk[entry_id] = (0.0, chunk_id)

    ordered = sorted(entry_scores, key=lambda entry: (-entry_scores[entry], str(entry)))
    evidence = evidence_chunks(db, owner_id, ordered, best_chunk)
    hits = [
        Hit(
            entry_id=entry_id,
            score=entry_scores[entry_id],
            chunk_id=chunk.id,
            source=chunk.source,
            display_text=chunk.display_text,
            start_offset=chunk.start_offset,
            matched_by=matched[entry_id],
        )
        for entry_id in ordered
        if (chunk := evidence.get(entry_id)) is not None
    ]
    return SearchResult(hits=hits, degraded=degraded, terms=terms)


def evidence_chunks(
    db: Session,
    owner_id: UUID,
    entries: list[UUID],
    best_chunk: dict[UUID, tuple[float, UUID]],
) -> dict[UUID, Chunk]:
    """The source chunk that represents each entry."""
    if not entries:
        return {}
    chosen = {chunk_id for _score, chunk_id in best_chunk.values()}
    found = {
        chunk.entry_id: chunk
        for chunk in db.scalars(select(Chunk).where(Chunk.id.in_(chosen)))
    }
    missing = [entry_id for entry_id in entries if entry_id not in found]
    if missing:
        for chunk in db.scalars(
            chunk_rows()
            .with_only_columns(Chunk)
            .where(
                Chunk.owner_id == owner_id,
                Chunk.entry_id.in_(missing),
                Chunk.unit == "chunk",
            )
            .order_by(Chunk.entry_id, Chunk.ordinal)
        ):
            found.setdefault(chunk.entry_id, chunk)
    return found


def snippet(text: str, terms: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """About SNIPPET_CHARS of text around the first match, with match ranges."""
    lowered = text.lower()
    marks = [term for term in terms if len(term) > 1] or terms
    positions = [lowered.find(term) for term in marks if lowered.find(term) >= 0]
    start = max(0, min(positions) - SNIPPET_LEAD) if positions else 0
    excerpt = text[start : start + SNIPPET_CHARS]
    window = excerpt.lower()
    taken: list[tuple[int, int]] = []
    for term in sorted(marks, key=len, reverse=True):
        position = window.find(term)
        while position >= 0:
            span = (position, position + len(term))
            if all(span[1] <= a or span[0] >= b for a, b in taken):
                taken.append(span)
            position = window.find(term, position + len(term))
    return excerpt, sorted(taken)
