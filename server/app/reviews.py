"""Stage reviews (product-plan §4.6): weekly automatic and on-demand.

The scheduler calls ``scan`` every few minutes. Once Monday 09:00 has passed
in an account's time zone, every active project with user events in the
previous natural week (Monday 00:00 to Monday 00:00) gets one review job.
Projects without events get none. Automatic reviews are unique per project
and period, so retries publish once ([T7]). Manual reviews cover the time
since the last review ended, or since the project was created.

ProjectReviewGraph: load → gather → write → publish → finish
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, ClassVar, Literal, TypedDict
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Response
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import decisions, events, grounding, jobs
from app.auth import AUTH_ERROR, Db, Owner
from app.contracts import CitationView, SummaryPoint
from app.graphs import JobPipeline
from app.models import (
    Chunk,
    Decision,
    Entry,
    Job,
    Project,
    ProjectEntry,
    ProjectEvent,
    ProjectQuestion,
    Review,
    SummaryRevision,
    User,
)
from app.projects import NOT_FOUND, get_project
from app.sources import SourceRef, citation_views

TASK = "project_review"
PROMPT_VERSION = 1
REVIEW_HOUR = 9
ENTRIES = 10
EVIDENCE_CHARS = 500
SECTIONS = (
    "new_information",
    "understanding_changes",
    "decisions",
    "open_questions",
    "next_steps",
)
QUIET = "这段时间项目没有新的资料、问题或决定变化。"

router = APIRouter(prefix="/v1", tags=["reviews"], responses=AUTH_ERROR)


# Periods and scheduling


def weekly_period(timezone: str, now: datetime) -> tuple[datetime, datetime]:
    """The latest natural week whose following Monday 09:00 has passed."""
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    monday: date = local.date() - timedelta(days=local.weekday())
    if local < datetime.combine(monday, time(REVIEW_HOUR), tzinfo=zone):
        monday -= timedelta(days=7)
    end = datetime.combine(monday, time(), tzinfo=zone)
    start = datetime.combine(monday - timedelta(days=7), time(), tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def has_events(db: Session, project_id: UUID, start: datetime, end: datetime) -> bool:
    return bool(
        db.scalar(
            select(func.count())
            .select_from(ProjectEvent)
            .where(
                ProjectEvent.project_id == project_id,
                ProjectEvent.created_at >= start,
                ProjectEvent.created_at < end,
            )
        )
    )


def review_job_exists(db: Session, project_id: UUID, start: datetime) -> bool:
    return bool(
        db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.project_id == project_id,
                Job.kind == jobs.REVIEW,
                Job.payload["kind"].astext == "auto",
                Job.payload["period_start"].astext == start.isoformat(),
            )
        )
    )


def queue_review(
    db: Session,
    owner_id: UUID,
    project_id: UUID,
    kind: str,
    start: datetime,
    end: datetime,
) -> Job:
    job = Job(
        owner_id=owner_id,
        project_id=project_id,
        kind=jobs.REVIEW,
        payload={
            "kind": kind,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        },
    )
    db.add(job)
    db.flush()
    return job


def scan(db: Session, now: datetime) -> int:
    """Queue due weekly reviews. Returns how many were queued."""
    queued = 0
    for user_id, timezone in db.execute(select(User.id, User.timezone)):
        start, end = weekly_period(timezone, now)
        for project in db.scalars(
            select(Project).where(
                Project.owner_id == user_id,
                Project.status == "active",
                Project.created_at < end,
            )
        ):
            if not has_events(db, project.id, start, end):
                continue
            exists = db.scalar(
                select(Review.id).where(
                    Review.project_id == project.id,
                    Review.kind == "auto",
                    Review.period_start == start,
                    Review.period_end == end,
                )
            )
            if exists or review_job_exists(db, project.id, start):
                continue
            queue_review(db, user_id, project.id, "auto", start, end)
            queued += 1
    return queued


# The review graph


class ReviewState(TypedDict):
    job_id: str
    owner_id: str
    project_id: str


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)
    ]
    citations: Annotated[list[int], Field(max_length=6)]


Section = Annotated[list[Item], Field(max_length=6)]


class ReviewDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator(
        "new_information",
        "understanding_changes",
        "decisions",
        "open_questions",
        "next_steps",
        mode="before",
    )
    @classmethod
    def keep_first(cls, value: Any) -> Any:
        return value[:6] if isinstance(value, list) else value

    new_information: Section
    understanding_changes: Section
    decisions: Section
    open_questions: Section
    next_steps: Section


SYSTEM = """你为一个个人项目写阶段回顾，只依据给出的材料。
结构：new_information（本期新增信息）、understanding_changes（认识变化，对比期初与期末总结）、
decisions（本期确认或修订的正式决定）、open_questions（仍未解决的问题）、next_steps（下一阶段建议）。
规则：
1. 事实后用 [n] 标注证据编号，每条的 citations 必须与该条正文中的 [n] 一致；没有对应证据的条目 citations 为空。
2. 只有“本期正式决定”列表里的内容可以写成决定；不要把建议或草稿写成已决定。
3. next_steps 是建议，用“建议……”开头。
4. 没有内容的部分返回空列表，不要凑数；每部分最多 6 条；不使用材料以外的知识。
只输出 JSON：{"new_information": [{"text": ..., "citations": [...]}], "understanding_changes": [...], "decisions": [...], "open_questions": [...], "next_steps": [...]}。"""


def validate_review(content: str, numbers: set[int]) -> ReviewDraft:
    draft = grounding.parse(content, ReviewDraft)
    for section in SECTIONS:
        for item in getattr(draft, section):
            item.text, item.citations = grounding.reconcile(item.text, item.citations)
            grounding.check_markers([item.text], item.citations, numbers)
    for item in draft.next_steps:
        if not item.text.startswith("建议"):
            item.text = f"建议：{item.text}"  # keep it worded as a suggestion
    return draft


def summary_at(db: Session, project_id: UUID, moment: datetime) -> str | None:
    understanding = db.scalar(
        select(SummaryRevision.content["understanding"].astext)
        .where(
            SummaryRevision.project_id == project_id,
            SummaryRevision.created_at <= moment,
        )
        .order_by(SummaryRevision.version.desc())
        .limit(1)
    )
    return grounding.strip_markers(understanding) if understanding else None


class Pipeline(JobPipeline):
    """Review jobs (kind ``project_review``). Requires models."""

    PROMPT_VERSIONS: ClassVar[dict[str, int]] = {TASK: PROMPT_VERSION}

    def initial_state(self, claim: jobs.Claim) -> dict[str, Any]:
        assert claim.project_id is not None
        return {
            "job_id": str(claim.id),
            "owner_id": str(claim.owner_id),
            "project_id": str(claim.project_id),
        }

    def build(self) -> StateGraph[Any]:
        graph = StateGraph(ReviewState)
        for name in ("gather", "write", "publish", "finish"):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "gather")
        graph.add_edge("gather", "write")
        graph.add_edge("write", "publish")
        graph.add_edge("publish", "finish")
        graph.add_edge("finish", END)
        return graph

    @property
    def project_id(self) -> UUID:
        assert self.claim is not None and self.claim.project_id is not None
        return self.claim.project_id

    def payload(self) -> dict[str, Any]:
        assert self.claim is not None
        with self.sessions() as db:
            return dict(db.get_one(Job, self.claim.id).payload or {})

    def update(self, stage: str, values: dict[str, Any]) -> None:
        with self.step(stage) as (_db, job):
            job.payload = {**(job.payload or {}), **values}

    def gather(self, state: ReviewState) -> dict[str, Any]:
        self.hook("gather")
        payload = self.payload()
        if "evidence" in payload:
            return {}
        start = datetime.fromisoformat(payload["period_start"])
        end = datetime.fromisoformat(payload["period_end"])
        with self.sessions() as db:
            self.update("gather", self.collect(db, start, end))
        return {}

    def collect(self, db: Session, start: datetime, end: datetime) -> dict[str, Any]:
        project_id = self.project_id
        changes = list(
            db.execute(
                select(ProjectEvent.seq, ProjectEvent.kind, ProjectEvent.subject_id)
                .where(
                    ProjectEvent.project_id == project_id,
                    ProjectEvent.created_at >= start,
                    ProjectEvent.created_at < end,
                )
                .order_by(ProjectEvent.seq)
            )
        )
        added = list(
            dict.fromkeys(
                subject
                for _seq, kind, subject in changes
                if kind in (events.ENTRY_READY, events.ENTRY_ADDED) and subject
            )
        )
        live = set(
            db.scalars(
                select(Entry.id)
                .join(ProjectEntry, ProjectEntry.entry_id == Entry.id)
                .where(
                    ProjectEntry.project_id == project_id,
                    Entry.id.in_(added),
                    Entry.deleted_at.is_(None),
                )
            )
        )
        evidence: list[dict[str, Any]] = []
        for entry_id in [e for e in added if e in live][:ENTRIES]:
            entry = db.get_one(Entry, entry_id)
            chunk = db.scalar(
                select(Chunk)
                .where(
                    Chunk.entry_id == entry_id,
                    Chunk.revision == entry.current_revision,
                    Chunk.unit == "chunk",
                )
                .order_by(Chunk.ordinal)
                .limit(1)
            )
            if chunk is None:
                continue
            evidence.append(
                {
                    "n": len(evidence) + 1,
                    "entry_id": str(entry_id),
                    "revision": chunk.revision,
                    "chunk_id": str(chunk.id),
                    "source": chunk.source,
                    "start_offset": chunk.start_offset,
                    "end_offset": chunk.end_offset,
                    "type": entry.type,
                    "title": "",
                    "created_at": entry.created_at.isoformat(),
                    "text": chunk.display_text[:EVIDENCE_CHARS],
                }
            )
        decided = [
            f"- {d.title}：{d.choice}"
            for d in db.scalars(
                select(Decision).where(
                    Decision.project_id == project_id,
                    Decision.status.in_(("confirmed", "superseded")),
                    Decision.updated_at >= start,
                    Decision.updated_at < end,
                )
            )
        ]
        questions = [
            f"- {q.text}"
            for q in db.scalars(
                select(ProjectQuestion).where(
                    ProjectQuestion.project_id == project_id,
                    ProjectQuestion.status == "open",
                )
            )
        ]
        return {
            "evidence": evidence,
            "decided": decided,
            "questions": questions,
            "summary_start": summary_at(db, project_id, start),
            "summary_end": summary_at(db, project_id, end),
            "snapshot": {
                "events": len(changes),
                "first_seq": changes[0][0] if changes else None,
                "last_seq": changes[-1][0] if changes else None,
                "entries": len(evidence),
            },
        }

    def write(self, state: ReviewState) -> dict[str, Any]:
        self.hook("write")
        payload = self.payload()
        if "draft" in payload:
            return {}
        assert self.models is not None
        if not payload["snapshot"]["events"]:
            draft: dict[str, list[dict[str, Any]]] = {
                section: [] for section in SECTIONS
            }
            draft["new_information"] = [{"text": QUIET, "citations": []}]
            self.update("write", {"draft": draft, "model": None})
            return {}
        with self.sessions() as db:
            project = db.get_one(Project, self.project_id)
            confirmed = decisions.confirmed_lines(db, self.project_id)
        lines = [
            f"项目：{project.name}",
            f"用户目标：{project.goal or '（未填写）'}",
            f"回顾期间：{payload['period_start'][:10]} 至 {payload['period_end'][:10]}",
            "期初总结：" + (payload["summary_start"] or "（无）"),
            "期末总结：" + (payload["summary_end"] or "（无）"),
            "本期正式决定：",
            *(payload["decided"] or ["（无）"]),
            "当前全部正式决定：",
            *(confirmed or ["（无）"]),
            "未解决的待解问题：",
            *(payload["questions"] or ["（无）"]),
            "\n本期新增资料（证据）：",
            *grounding.evidence_block(payload["evidence"], EVIDENCE_CHARS),
        ]
        numbers = {item["n"] for item in payload["evidence"]}
        result, model = grounding.ask(
            self.models.router,
            "heavy",
            [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": "\n".join(lines)},
            ],
            TASK,
            self.record,
            lambda content: validate_review(content, numbers),
        )
        self.update("write", {"draft": result.model_dump(), "model": model})
        return {}

    def publish(self, state: ReviewState) -> dict[str, Any]:
        self.hook("publish")
        payload = self.payload()
        if "review_id" in payload:
            return {}
        assert self.claim is not None
        draft = payload["draft"]
        cited = {
            n
            for section in SECTIONS
            for item in draft[section]
            for n in item["citations"]
        }
        with self.step("publish") as (db, job):
            review_id = db.scalar(
                insert(Review)
                .values(
                    owner_id=self.claim.owner_id,
                    project_id=self.project_id,
                    kind=payload["kind"],
                    period_start=datetime.fromisoformat(payload["period_start"]),
                    period_end=datetime.fromisoformat(payload["period_end"]),
                    content=draft,
                    sources=[i for i in payload["evidence"] if i["n"] in cited],
                    input_snapshot=payload["snapshot"],
                    model=payload.get("model"),
                )
                .on_conflict_do_nothing(
                    index_elements=["project_id", "period_start", "period_end"],
                    index_where=Review.kind == "auto",
                )
                .returning(Review.id)
            )
            job.payload = {
                **(job.payload or {}),
                "review_id": str(review_id) if review_id else None,
            }
        return {}


# API


class ReviewSections(BaseModel):
    new_information: list[SummaryPoint]
    understanding_changes: list[SummaryPoint]
    decisions: list[SummaryPoint]
    open_questions: list[SummaryPoint]
    next_steps: list[SummaryPoint] = Field(description="Suggestions, not decisions.")


class ReviewView(BaseModel):
    id: UUID
    project_id: UUID
    kind: Literal["auto", "manual"]
    period_start: datetime
    period_end: datetime
    sections: ReviewSections
    sources: list[CitationView]
    unread: bool
    model: str | None
    created_at: datetime


class ReviewListItem(BaseModel):
    id: UUID
    kind: Literal["auto", "manual"]
    period_start: datetime
    period_end: datetime
    unread: bool
    created_at: datetime


class ReviewRun(BaseModel):
    run_id: UUID


def review_view(db: Session, owner_id: UUID, review: Review) -> ReviewView:
    return ReviewView(
        id=review.id,
        project_id=review.project_id,
        kind=review.kind,  # type: ignore[arg-type]
        period_start=review.period_start,
        period_end=review.period_end,
        sections=ReviewSections(**review.content),
        sources=citation_views(
            db,
            owner_id,
            review.project_id,
            [SourceRef.from_evidence(item) for item in review.sources],
        ),
        unread=review.read_at is None,
        model=review.model,
        created_at=review.created_at,
    )


def get_review(db: Session, owner_id: UUID, review_id: UUID) -> Review:
    review = db.scalar(
        select(Review).where(Review.owner_id == owner_id, Review.id == review_id)
    )
    if review is None:
        raise HTTPException(404, "review not found")
    return review


@router.get("/projects/{project_id}/reviews", responses=NOT_FOUND)
def list_reviews(project_id: UUID, owner: Owner, db: Db) -> list[ReviewListItem]:
    """The project's reviews, newest first, with unread marks."""
    get_project(db, owner.id, project_id)
    return [
        ReviewListItem(
            id=r.id,
            kind=r.kind,  # type: ignore[arg-type]
            period_start=r.period_start,
            period_end=r.period_end,
            unread=r.read_at is None,
            created_at=r.created_at,
        )
        for r in db.scalars(
            select(Review)
            .where(Review.owner_id == owner.id, Review.project_id == project_id)
            .order_by(Review.created_at.desc())
            .limit(100)
        )
    ]


@router.post("/projects/{project_id}/reviews", status_code=202, responses=NOT_FOUND)
def request_review(project_id: UUID, owner: Owner, db: Db) -> ReviewRun:
    """Review the time since the last review (or since the project began).

    Poll GET /v1/runs/{run_id}; its review_id points to the result.
    """
    project = get_project(db, owner.id, project_id)
    last_end = db.scalar(
        select(func.max(Review.period_end)).where(Review.project_id == project_id)
    )
    job = queue_review(
        db,
        owner.id,
        project_id,
        "manual",
        last_end or project.created_at,
        datetime.now(UTC),
    )
    db.commit()
    return ReviewRun(run_id=job.id)


@router.get("/reviews/{review_id}", responses=NOT_FOUND)
def review_detail(review_id: UUID, owner: Owner, db: Db) -> ReviewView:
    return review_view(db, owner.id, get_review(db, owner.id, review_id))


@router.patch("/reviews/{review_id}/read", status_code=204, responses=NOT_FOUND)
def mark_read(review_id: UUID, owner: Owner, db: Db) -> Response:
    review = get_review(db, owner.id, review_id)
    if review.read_at is None:
        review.read_at = datetime.now(UTC)
    db.commit()
    return Response(status_code=204)
