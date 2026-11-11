"""Per-account daily quotas and model budget (product plan §8, S6).

Days follow the account's timezone. Limits come from GATHERLOOM_* variables;
0 turns a limit off. Exceeding one answers 429 with Retry-After set to the
account's next midnight: iOS keeps the local copy and retries later.

The model budget counts estimated cost recorded in ``runs`` (USD converted at
a fixed rate). Over budget, new questions and manual reviews are refused, and
the worker postpones background jobs to the next day instead of failing them.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Entry, Job, Message, Run, User

ENTRIES = "entries"
QUESTIONS = "questions"
REVIEWS = "reviews"


@dataclass(frozen=True)
class Limits:
    entries: int = 200
    questions: int = 100
    reviews: int = 10
    budget_cny: Decimal = Decimal(5)
    usd_cny: Decimal = Decimal("7.2")


def load_limits(env: Mapping[str, str] = os.environ) -> Limits:
    default = Limits()
    return Limits(
        entries=int(env.get("GATHERLOOM_DAILY_ENTRIES") or default.entries),
        questions=int(env.get("GATHERLOOM_DAILY_QUESTIONS") or default.questions),
        reviews=int(env.get("GATHERLOOM_DAILY_REVIEWS") or default.reviews),
        budget_cny=Decimal(
            env.get("GATHERLOOM_DAILY_BUDGET_CNY") or default.budget_cny
        ),
        usd_cny=Decimal(env.get("GATHERLOOM_USD_CNY") or default.usd_cny),
    )


def day_bounds(db: Session, owner_id: UUID, now: datetime) -> tuple[datetime, datetime]:
    zone = ZoneInfo(
        db.scalar(select(User.timezone).where(User.id == owner_id)) or "UTC"
    )
    start = datetime.combine(now.astimezone(zone).date(), time(), tzinfo=zone)
    return start, start + timedelta(days=1)


def used(db: Session, owner_id: UUID, kind: str, since: datetime) -> int:
    if kind == ENTRIES:
        query = select(func.count()).where(
            Entry.owner_id == owner_id, Entry.created_at >= since
        )
    elif kind == QUESTIONS:
        query = select(func.count()).where(
            Message.owner_id == owner_id,
            Message.role == "user",
            Message.created_at >= since,
        )
    else:
        query = select(func.count()).where(
            Job.owner_id == owner_id,
            Job.kind == "project_review",
            Job.payload["kind"].astext == "manual",
            Job.created_at >= since,
        )
    return db.scalar(query) or 0


def spent_cny(db: Session, owner_id: UUID, since: datetime, limits: Limits) -> Decimal:
    rate = case((Run.currency == "USD", limits.usd_cny), else_=Decimal(1))
    total = db.scalar(
        select(func.sum(func.coalesce(Run.cost_estimate, 0) * rate)).where(
            Run.owner_id == owner_id, Run.created_at >= since
        )
    )
    return Decimal(total or 0)


def over_budget(
    db: Session, owner_id: UUID, now: datetime, limits: Limits | None = None
) -> datetime | None:
    """When the account is over today's model budget, the next day's start."""
    limits = limits or load_limits()
    if not limits.budget_cny:
        return None
    start, end = day_bounds(db, owner_id, now)
    return end if spent_cny(db, owner_id, start, limits) >= limits.budget_cny else None


def too_many(end: datetime, now: datetime, detail: str) -> HTTPException:
    retry = max(1, int((end - now).total_seconds()))
    return HTTPException(429, detail, headers={"Retry-After": str(retry)})


def check(
    db: Session,
    owner_id: UUID,
    kind: str,
    now: datetime,
    limits: Limits | None = None,
) -> None:
    """Raise 429 when today's count for ``kind`` is used up or, for model
    work started on request, when the budget is spent."""
    limits = limits or load_limits()
    limit = getattr(limits, kind)
    start, end = day_bounds(db, owner_id, now)
    if limit and used(db, owner_id, kind, start) >= limit:
        raise too_many(end, now, f"daily {kind} limit reached")
    if kind != ENTRIES and over_budget(db, owner_id, now, limits):
        raise too_many(end, now, "daily model budget reached")
