"""Decision drafts and confirmed decisions (product-plan §3.8, §4.5).

Drafts are proposed during project maintenance, only when the run saw new
material, at most ``DAILY_LIMIT`` per project per day (in the account's
time zone), deduplicated by normalized topic. A rejected topic is not
proposed again on the same evidence. Only the user's ``confirm`` makes a
draft a decision; confirming twice is a no-op ([T6]). Confirmed decisions
keep every revision. Nothing here acts on the outside world.
"""

import re
from datetime import UTC, datetime, time
from hashlib import sha256
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import deletion, events, grounding
from app.auth import AUTH_ERROR, Db, Owner
from app.contracts import CitationView, ErrorResponses, Input
from app.models import Decision, DecisionRevision, Project, User
from app.projects import NOT_FOUND, get_project
from app.providers import Recorder, Router
from app.sources import SourceRef, citation_views

TASK = "decision_drafts"
PROMPT_VERSION = 2
DAILY_LIMIT = 3
FORBIDDEN = ("已决定", "已确认", "已经决定", "最终决定", "已拍板")
STATUSES = ("draft", "confirmed", "rejected", "superseded")

router = APIRouter(prefix="/v1", tags=["decisions"], responses=AUTH_ERROR)

Short = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
Long = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)
]
Items = Annotated[
    list[
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
        ]
    ],
    Field(max_length=6),
]


# Generation


class DraftItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Short
    choice: Long
    rationale: Long
    alternatives: Items
    premises: Items
    citations: Annotated[list[int], Field(min_length=1, max_length=6)]


class DraftList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drafts: Annotated[list[DraftItem], Field(max_length=DAILY_LIMIT)]


SYSTEM = """你帮用户发现项目里“值得做出决定”的事项，并起草决定建议，交给用户确认。
规则：
1. 只有当证据里实际出现了两个以上可比较的方案，并且有倾向或理由时，才起草；证据里只有一个做法、或只是待办与提醒的，不起草。没有就返回空列表，不要硬凑。
2. 这是建议，不是用户的决定：不要写“已决定”“已确认”“最终决定”等字样。choice 只写倾向的方案本身（如“选 A 线”），不加“建议”等前缀；界面会把草稿标为建议。
3. 不要重复“已有决定与草稿”中的主题。
4. rationale 中的事实后用 [n] 标注证据编号，citations 必须与 rationale 中的 [n] 一致。
5. alternatives 只写证据里出现过的其他方案，不要自己编；premises 写这个建议成立的前提。
6. 不使用证据以外的知识。
只输出 JSON：{"drafts": [{"title": 主题, "choice": 倾向的方案, "rationale": 理由, "alternatives": [...], "premises": [...], "citations": [编号]}]}。"""


SUGGEST_PREFIX = re.compile(r"^(建议|推荐)(选择|采用|选)?[：:，,\s]*")


def plain_choice(choice: str) -> str:
    """The option itself: a confirmed decision must not read as a suggestion."""
    return SUGGEST_PREFIX.sub("", choice.strip(), count=1) or choice


def topic_key(title: str) -> str:
    return re.sub(r"\W", "", title.lower())[:120]


def fingerprint(sources: list[dict[str, Any]]) -> str:
    keys = sorted(f"{item['entry_id']}:{item['revision']}" for item in sources)
    return sha256("|".join(keys).encode()).hexdigest()


def validate_drafts(content: str, numbers: set[int]) -> DraftList:
    drafts = grounding.parse(content, DraftList)
    for draft in drafts.drafts:
        draft.rationale, draft.citations = grounding.reconcile(
            draft.rationale, draft.citations
        )
        grounding.check_markers([draft.rationale], draft.citations, numbers)
        draft.choice = plain_choice(draft.choice)
        text = draft.title + draft.choice + draft.rationale
        if found := [word for word in FORBIDDEN if word in text]:
            raise ValueError(f"草稿措辞不能写成已经决定：{found}")
    # One option is a to-do, not a decision.
    drafts.drafts = [draft for draft in drafts.drafts if draft.alternatives]
    return drafts


def local_day_start(db: Session, owner_id: UUID, now: datetime) -> datetime:
    zone = ZoneInfo(
        db.scalar(select(User.timezone).where(User.id == owner_id)) or "UTC"
    )
    local = now.astimezone(zone)
    return datetime.combine(local.date(), time(), tzinfo=zone)


def remaining_today(
    db: Session, owner_id: UUID, project_id: UUID, now: datetime
) -> int:
    created = db.scalar(
        select(func.count())
        .select_from(Decision)
        .where(
            Decision.project_id == project_id,
            Decision.created_at >= local_day_start(db, owner_id, now),
        )
    )
    return max(0, DAILY_LIMIT - (created or 0))


def confirmed_lines(db: Session, project_id: UUID) -> list[str]:
    return [
        f"- {d.title}：{d.choice}（确认于 {d.confirmed_at:%Y-%m-%d}）"
        for d in db.scalars(
            select(Decision)
            .where(Decision.project_id == project_id, Decision.status == "confirmed")
            .order_by(Decision.confirmed_at)
        )
        if d.confirmed_at
    ]


def propose(
    db: Session,
    owner_id: UUID,
    project_id: UUID,
    evidence: list[dict[str, Any]],
    router_: Router,
    record: Recorder,
    now: datetime,
) -> list[dict[str, Any]]:
    """Draft decisions to store (already filtered for limits and duplicates)."""
    remaining = remaining_today(db, owner_id, project_id, now)
    numbers = {item["n"] for item in evidence}
    if remaining == 0 or not numbers:
        return []
    project = db.get_one(Project, project_id)
    existing = list(
        db.scalars(select(Decision).where(Decision.project_id == project_id))
    )
    lines = [f"项目：{project.name}", f"用户目标：{project.goal or '（未填写）'}"]
    lines.append("已有决定与草稿（不要重复）：")
    lines += [f"- {d.title}" for d in existing if d.status != "rejected"] or [
        "（暂无）"
    ]
    lines.append(f"本次最多起草 {remaining} 条。")
    lines.append("\n证据：")
    lines += grounding.evidence_block(evidence, 600)
    drafts, _model = grounding.ask(
        router_,
        "heavy",
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n".join(lines)},
        ],
        TASK,
        record,
        lambda content: validate_drafts(content, numbers),
    )
    taken = {d.topic_key for d in existing if d.status != "rejected"}
    rejected = {
        (d.topic_key, d.source_fingerprint) for d in existing if d.status == "rejected"
    }
    by_number = {item["n"]: item for item in evidence}
    result = []
    for draft in drafts.drafts:
        key = topic_key(draft.title)
        sources = [by_number[n] for n in sorted(set(draft.citations))]
        print_ = fingerprint(sources)
        if not key or key in taken or (key, print_) in rejected:
            continue
        taken.add(key)
        result.append(
            {
                **draft.model_dump(exclude={"citations"}),
                "sources": sources,
                "topic_key": key,
                "source_fingerprint": print_,
            }
        )
        if len(result) == remaining:
            break
    return result


def store(
    db: Session, owner_id: UUID, project_id: UUID, drafts: list[dict[str, Any]]
) -> None:
    for draft in drafts:
        draft = {
            **draft,
            "sources": deletion.without_deleted(db, owner_id, draft["sources"]),
        }
        db.add(
            Decision(owner_id=owner_id, project_id=project_id, status="draft", **draft)
        )


# API


class DecisionView(BaseModel):
    id: UUID
    project_id: UUID
    status: Literal["draft", "confirmed", "rejected", "superseded"]
    title: str
    choice: str
    rationale: str = Field(description="Marks citations as [n].")
    alternatives: list[str]
    premises: list[str]
    sources: list[CitationView]
    basis_changed: bool = Field(
        description="A cited entry was edited, deleted or removed since."
    )
    version: int
    superseded_by: UUID | None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None


class DecisionRevisionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: int
    title: str
    choice: str
    rationale: str
    alternatives: list[str]
    premises: list[str]
    created_at: datetime


class DecisionAction(Input):
    """``version`` must match the decision's current version.

    confirm: draft → confirmed, with optional edits. Repeating it is a no-op.
    reject: draft → rejected. revise: edit a confirmed decision (new revision).
    supersede: confirmed → superseded by another confirmed decision.
    """

    action: Literal["confirm", "reject", "revise", "supersede"]
    version: Annotated[int, Field(ge=1)]
    title: Short | None = None
    choice: Long | None = None
    rationale: Annotated[str, StringConstraints(max_length=1500)] | None = None
    alternatives: Items | None = None
    premises: Items | None = None
    superseded_by: UUID | None = None

    @model_validator(mode="after")
    def check_fields(self) -> "DecisionAction":
        if (self.action == "supersede") != (self.superseded_by is not None):
            raise ValueError(
                "superseded_by is required for supersede and only allowed there"
            )
        return self


def decision_view(db: Session, owner_id: UUID, decision: Decision) -> DecisionView:
    sources = citation_views(
        db,
        owner_id,
        decision.project_id,
        [SourceRef.from_evidence(item) for item in decision.sources],
    )
    return DecisionView(
        id=decision.id,
        project_id=decision.project_id,
        status=decision.status,  # type: ignore[arg-type]
        title=decision.title,
        choice=decision.choice,
        rationale=decision.rationale,
        alternatives=decision.alternatives,
        premises=decision.premises,
        sources=sources,
        basis_changed=any(view.stale for view in sources),
        version=decision.version,
        superseded_by=decision.superseded_by,
        created_at=decision.created_at,
        updated_at=decision.updated_at,
        confirmed_at=decision.confirmed_at,
    )


def get_decision(
    db: Session, owner_id: UUID, decision_id: UUID, *, lock: bool = False
) -> Decision:
    query = select(Decision).where(
        Decision.owner_id == owner_id, Decision.id == decision_id
    )
    decision = db.scalar(query.with_for_update() if lock else query)
    if decision is None:
        raise HTTPException(404, "decision not found")
    return decision


def add_revision(db: Session, decision: Decision) -> None:
    """Record the confirmed text; revisions are numbered 1, 2, … per decision."""
    count = db.scalar(
        select(func.count())
        .select_from(DecisionRevision)
        .where(DecisionRevision.decision_id == decision.id)
    )
    db.add(
        DecisionRevision(
            owner_id=decision.owner_id,
            decision_id=decision.id,
            version=(count or 0) + 1,
            title=decision.title,
            choice=decision.choice,
            rationale=decision.rationale,
            alternatives=decision.alternatives,
            premises=decision.premises,
        )
    )


@router.get("/projects/{project_id}/decisions", responses=NOT_FOUND)
def list_decisions(
    project_id: UUID,
    owner: Owner,
    db: Db,
    status: Literal["draft", "confirmed", "rejected", "superseded"] | None = None,
) -> list[DecisionView]:
    """Drafts and decisions of a project, newest first; filter by status."""
    get_project(db, owner.id, project_id)
    query = select(Decision).where(
        Decision.owner_id == owner.id, Decision.project_id == project_id
    )
    if status is not None:
        query = query.where(Decision.status == status)
    rows = db.scalars(query.order_by(Decision.updated_at.desc()).limit(200))
    return [decision_view(db, owner.id, row) for row in rows]


@router.get("/decisions/{decision_id}", responses=NOT_FOUND)
def decision_detail(decision_id: UUID, owner: Owner, db: Db) -> DecisionView:
    return decision_view(db, owner.id, get_decision(db, owner.id, decision_id))


@router.get("/decisions/{decision_id}/revisions", responses=NOT_FOUND)
def decision_revisions(
    decision_id: UUID, owner: Owner, db: Db
) -> list[DecisionRevisionView]:
    """Every confirmed text of the decision, newest first."""
    decision = get_decision(db, owner.id, decision_id)
    return [
        DecisionRevisionView.model_validate(row)
        for row in db.scalars(
            select(DecisionRevision)
            .where(DecisionRevision.decision_id == decision.id)
            .order_by(DecisionRevision.version.desc())
        )
    ]


CONFLICT: ErrorResponses = {
    409: {"description": "Version changed, or the action does not apply."}
}


@router.patch("/decisions/{decision_id}", responses={**NOT_FOUND, **CONFLICT})
def act_on_decision(
    decision_id: UUID, body: DecisionAction, owner: Owner, db: Db
) -> DecisionView:
    """Confirm, reject, revise or supersede. Only this makes AI drafts decisions."""
    decision = get_decision(db, owner.id, decision_id, lock=True)
    if body.action == "confirm" and decision.status == "confirmed":
        return decision_view(db, owner.id, decision)  # repeated confirm: no-op
    if body.version != decision.version:
        raise HTTPException(409, "decision changed since version")
    now = datetime.now(UTC)
    edits = {
        key: value
        for key in ("title", "choice", "rationale", "alternatives", "premises")
        if (value := getattr(body, key)) is not None
    }
    if body.action == "confirm" and decision.status == "draft":
        for key, value in edits.items():
            setattr(decision, key, value)
        decision.choice = plain_choice(decision.choice)
        decision.status = "confirmed"
        decision.confirmed_at = now
        add_revision(db, decision)
        events.record(
            db, owner.id, decision.project_id, events.DECISION_CONFIRMED, decision.id
        )
    elif body.action == "reject" and decision.status == "draft":
        decision.status = "rejected"
    elif body.action == "revise" and decision.status == "confirmed":
        if not edits:
            raise HTTPException(422, "nothing to revise")
        for key, value in edits.items():
            setattr(decision, key, value)
        add_revision(db, decision)
        events.record(
            db, owner.id, decision.project_id, events.DECISION_REVISED, decision.id
        )
    elif body.action == "supersede" and decision.status == "confirmed":
        assert body.superseded_by is not None
        newer = get_decision(db, owner.id, body.superseded_by)
        if (
            newer.id == decision.id
            or newer.project_id != decision.project_id
            or newer.status != "confirmed"
        ):
            raise HTTPException(409, "superseded_by must be another confirmed decision")
        decision.status = "superseded"
        decision.superseded_by = newer.id
        events.record(
            db, owner.id, decision.project_id, events.DECISION_SUPERSEDED, decision.id
        )
    else:
        raise HTTPException(409, f"cannot {body.action} a {decision.status} decision")
    decision.version += 1
    decision.updated_at = now
    db.flush()
    result = decision_view(db, owner.id, decision)
    db.commit()
    return result
