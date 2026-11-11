"""Relation suggestions: "related entries" and "relevant to an open question".

Generated during project maintenance for entries that changed in the run.
Candidates come from vectors only (nearest entries in the same project and
nearest open questions); one light-model call per entry keeps the truly
related ones, at most three, each with a one-sentence reason. An entry
revision is suggested once: dismissed suggestions do not come back.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import grounding, search
from app.auth import AUTH_ERROR, Db, Owner
from app.contracts import EntryType, Input
from app.entries import get_entry, views
from app.models import Artifact, Chunk, Entry, ProjectEntry, ProjectQuestion, Relation
from app.projects import NOT_FOUND, get_project
from app.providers import Embedder, Recorder, Router

TASK = "relation_reasons"
PROMPT_VERSION = 1
CANDIDATES = 5
KEEP = 3
QUESTION_MAX_DISTANCE = 0.6
TEXT_CHARS = 400

router = APIRouter(prefix="/v1", tags=["relations"], responses=AUTH_ERROR)


# Generation


class Pick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Annotated[str, StringConstraints(pattern=r"^[EQ]\d+$")]
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)
    ]


class Picks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relations: Annotated[list[Pick], Field(max_length=KEEP)]


SYSTEM = """你帮用户整理一个项目里的资料。给出一条新资料和若干候选：其他资料（E 开头）和待解问题（Q 开头）。
只挑出与新资料确实相关的候选，最多 3 个；没有就返回空列表。
每个写一句中文理由（不超过 40 字），说明具体关联点，不要泛泛而谈，不要编造资料里没有的内容。
只输出 JSON：{"relations": [{"target": "E1", "reason": "理由"}]}。"""


def representative(db: Session, entry_id: UUID) -> tuple[list[float] | None, str]:
    """An entry-level vector (its card, else first chunk) and a short text."""
    entry = db.get_one(Entry, entry_id)
    rows = list(
        db.execute(
            select(Chunk.unit, Chunk.embedding, Chunk.display_text)
            .where(
                Chunk.entry_id == entry_id,
                Chunk.revision == entry.current_revision,
                Chunk.embedding.is_not(None),
            )
            .order_by(Chunk.ordinal)
        )
    )
    card = next((row for row in rows if row[0] == "card"), None)
    first = next((row for row in rows if row[0] == "chunk"), None)
    chosen = card if card is not None else first
    vector = list(chosen[1]) if chosen is not None and chosen[1] is not None else None
    return vector, (first[2][:TEXT_CHARS] if first is not None else "")


def entry_candidates(
    db: Session, owner_id: UUID, project_id: UUID, entry_id: UUID, vector: list[float]
) -> list[tuple[UUID, float]]:
    distance = func.min(Chunk.embedding.cosine_distance(vector))
    return [
        (row[0], float(row[1]))
        for row in db.execute(
            select(Chunk.entry_id, distance)
            .join(
                Entry,
                and_(
                    Entry.id == Chunk.entry_id,
                    Entry.current_revision == Chunk.revision,
                    Entry.deleted_at.is_(None),
                ),
            )
            .join(
                ProjectEntry,
                and_(
                    ProjectEntry.entry_id == Chunk.entry_id,
                    ProjectEntry.project_id == project_id,
                ),
            )
            .where(
                Chunk.owner_id == owner_id,
                Chunk.entry_id != entry_id,
                Chunk.embedding.is_not(None),
            )
            .group_by(Chunk.entry_id)
            .having(distance <= search.SEARCH_MAX_DISTANCE)
            .order_by(distance)
            .limit(CANDIDATES)
        )
    ]


def cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = (sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5)
    return 1.0 - dot / norm if norm else 1.0


def entry_label(db: Session, entry_id: UUID) -> str:
    entry = db.get_one(Entry, entry_id)
    card = db.scalar(
        select(Artifact.content).where(
            Artifact.entry_id == entry_id,
            Artifact.revision == entry.current_revision,
            Artifact.kind == "card",
        )
    )
    if card:
        return f"{card['title']}：{card['summary']}"
    _vector, text = representative(db, entry_id)
    return text


def suggest(
    db: Session,
    owner_id: UUID,
    project_id: UUID,
    entry_id: UUID,
    question_vectors: list[tuple[UUID, str, list[float]]],
    router_: Router,
    record: Recorder,
) -> list[dict[str, Any]]:
    """Return suggestions for one entry (not yet stored)."""
    vector, text = representative(db, entry_id)
    if vector is None:
        return []
    entries = entry_candidates(db, owner_id, project_id, entry_id, vector)
    questions = sorted(
        (
            (question_id, question_text, cosine_distance(vector, q_vector))
            for question_id, question_text, q_vector in question_vectors
        ),
        key=lambda item: item[2],
    )
    questions = [q for q in questions if q[2] <= QUESTION_MAX_DISTANCE][:CANDIDATES]
    if not entries and not questions:
        return []
    labels: dict[str, tuple[str, UUID]] = {}
    lines = [f"新资料：{entry_label(db, entry_id) or text}", "候选："]
    for index, (candidate, _distance) in enumerate(entries, start=1):
        labels[f"E{index}"] = ("entry", candidate)
        lines.append(f"E{index}：{entry_label(db, candidate)}")
    for index, (question_id, question_text, _distance) in enumerate(questions, start=1):
        labels[f"Q{index}"] = ("question", question_id)
        lines.append(f"Q{index}（待解问题）：{question_text}")

    def check(content: str) -> Picks:
        picks = grounding.parse(content, Picks)
        if unknown := [p.target for p in picks.relations if p.target not in labels]:
            raise ValueError(f"未知的候选：{unknown}")
        return picks

    picks, _model = grounding.ask(
        router_,
        "light",
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n".join(lines)},
        ],
        TASK,
        record,
        check,
    )
    result = []
    for pick in {p.target: p for p in picks.relations}.values():
        kind, target = labels[pick.target]
        result.append(
            {
                "target_entry_id": str(target) if kind == "entry" else None,
                "target_question_id": str(target) if kind == "question" else None,
                "reason": pick.reason,
            }
        )
    return result


def embed_questions(
    db: Session, project_id: UUID, embedder: Embedder, record: Recorder
) -> list[tuple[UUID, str, list[float]]]:
    questions = list(
        db.execute(
            select(ProjectQuestion.id, ProjectQuestion.text).where(
                ProjectQuestion.project_id == project_id,
                ProjectQuestion.status == "open",
            )
        )
    )
    if not questions:
        return []
    vectors = embedder.embed(
        [text for _id, text in questions], "embed_question", record
    )
    return [
        (question_id, text, vector)
        for (question_id, text), vector in zip(questions, vectors, strict=True)
    ]


def already_suggested(db: Session, project_id: UUID, entry_id: UUID) -> bool:
    entry = db.get_one(Entry, entry_id)
    return bool(
        db.scalar(
            select(
                exists().where(
                    Relation.project_id == project_id,
                    Relation.entry_id == entry_id,
                    Relation.revision == entry.current_revision,
                )
            )
        )
    )


def store(
    db: Session,
    owner_id: UUID,
    project_id: UUID,
    entry_id: UUID,
    suggestions: list[dict[str, Any]],
) -> None:
    revision = db.get_one(Entry, entry_id).current_revision
    for item in suggestions:
        statement = insert(Relation).values(
            owner_id=owner_id,
            project_id=project_id,
            entry_id=entry_id,
            revision=revision,
            target_entry_id=item["target_entry_id"],
            target_question_id=item["target_question_id"],
            reason=item["reason"],
        )
        db.execute(statement.on_conflict_do_nothing())


# API


class RelationEntry(BaseModel):
    id: UUID
    title: str
    type: EntryType


class RelationQuestion(BaseModel):
    id: UUID
    text: str
    status: Literal["open", "resolved"]


class RelationView(BaseModel):
    id: UUID
    project_id: UUID
    entry: RelationEntry = Field(description="The entry the suggestion was made for.")
    target_entry: RelationEntry | None
    target_question: RelationQuestion | None
    reason: str = Field(description="AI-written reason; a suggestion, not a fact.")
    status: Literal["suggested", "accepted", "dismissed"]
    created_at: datetime


class RelationUpdate(Input):
    status: Literal["accepted", "dismissed"]


def relation_views(
    db: Session, owner_id: UUID, rows: list[Relation]
) -> list[RelationView]:
    entry_ids = {r.entry_id for r in rows} | {
        r.target_entry_id for r in rows if r.target_entry_id
    }
    entries = list(
        db.scalars(
            select(Entry).where(Entry.owner_id == owner_id, Entry.id.in_(entry_ids))
        )
    )
    summaries = {view.id: view for view in views(db, owner_id, entries)}
    questions = {
        q.id: q
        for q in db.scalars(
            select(ProjectQuestion).where(
                ProjectQuestion.id.in_({r.target_question_id for r in rows})
            )
        )
    }

    def brief(entry_id: UUID) -> RelationEntry:
        view = summaries[entry_id]
        return RelationEntry(
            id=entry_id,
            title=view.title or view.text[:40] or view.source_url or "",
            type=view.type,
        )

    return [
        RelationView(
            id=r.id,
            project_id=r.project_id,
            entry=brief(r.entry_id),
            target_entry=brief(r.target_entry_id) if r.target_entry_id else None,
            target_question=RelationQuestion(
                id=q.id,
                text=q.text,
                status=q.status,  # type: ignore[arg-type]
            )
            if r.target_question_id and (q := questions.get(r.target_question_id))
            else None,
            reason=r.reason,
            status=r.status,  # type: ignore[arg-type]
            created_at=r.created_at,
        )
        for r in rows
    ]


def visible() -> list[Any]:
    """Current-revision suggestions whose entries are still live."""
    source = Entry.__table__.alias("source")
    target = Entry.__table__.alias("target")
    return [
        Relation.status != "dismissed",
        exists().where(
            source.c.id == Relation.entry_id,
            source.c.deleted_at.is_(None),
            source.c.current_revision == Relation.revision,
        ),
        or_(
            Relation.target_entry_id.is_(None),
            exists().where(
                target.c.id == Relation.target_entry_id, target.c.deleted_at.is_(None)
            ),
        ),
    ]


@router.get("/entries/{entry_id}/relations", responses=NOT_FOUND)
def entry_relations(entry_id: UUID, owner: Owner, db: Db) -> list[RelationView]:
    """Suggestions involving this entry in any of its projects, either direction."""
    get_entry(db, owner.id, entry_id)
    rows = list(
        db.scalars(
            select(Relation)
            .where(
                Relation.owner_id == owner.id,
                or_(
                    Relation.entry_id == entry_id, Relation.target_entry_id == entry_id
                ),
                *visible(),
            )
            .order_by(Relation.created_at.desc())
        )
    )
    return relation_views(db, owner.id, rows)


@router.get("/projects/{project_id}/relations", responses=NOT_FOUND)
def project_relations(
    project_id: UUID, owner: Owner, db: Db, question_id: UUID | None = None
) -> list[RelationView]:
    """The project's suggestions, optionally only those for one open question."""
    get_project(db, owner.id, project_id)
    query = select(Relation).where(
        Relation.owner_id == owner.id, Relation.project_id == project_id, *visible()
    )
    if question_id is not None:
        query = query.where(Relation.target_question_id == question_id)
    rows = list(db.scalars(query.order_by(Relation.created_at.desc()).limit(200)))
    return relation_views(db, owner.id, rows)


@router.patch("/relations/{relation_id}", responses=NOT_FOUND)
def decide_relation(
    relation_id: UUID, body: RelationUpdate, owner: Owner, db: Db
) -> RelationView:
    """Accept or dismiss a suggestion. Dismissed ones stay hidden."""
    relation = db.scalar(
        select(Relation)
        .where(Relation.owner_id == owner.id, Relation.id == relation_id)
        .with_for_update()
    )
    if relation is None:
        raise HTTPException(404, "relation not found")
    relation.status = body.status
    relation.decided_at = datetime.now(UTC)
    db.flush()
    [view] = relation_views(db, owner.id, [relation])
    db.commit()
    return view
