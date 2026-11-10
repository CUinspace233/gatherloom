"""Shared loading of the evaluation set and its accounts."""

from functools import cache
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Entry, Project, User

DOCS = Path(__file__).resolve().parents[2] / "docs" / "evaluation"


@cache
def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((DOCS / f"{name}.yaml").read_text())
    return data


def submission_id(account: str, material_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"gatherloom-eval:{account}:{material_id}")


def materials() -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in load("materials")["materials"]}


def owner_id(db: Session, account: str) -> UUID:
    user_id = db.scalar(select(User.id).where(User.username == account))
    if user_id is None:
        raise SystemExit(f"account {account} not found; run python -m evaluation.load")
    return user_id


def project_id(db: Session, account: str, key: str) -> UUID:
    name = load("materials")["accounts"][account][key]
    found = db.scalar(
        select(Project.id).where(
            Project.owner_id == owner_id(db, account), Project.name == name
        )
    )
    if found is None:
        raise SystemExit(f"project {name} not found; run python -m evaluation.load")
    return found


def entry_ids(db: Session) -> dict[UUID, str]:
    """Entry id → material id, for every loaded material."""
    by_submission = {
        submission_id(item["account"], item["id"]): item["id"]
        for item in materials().values()
    }
    return {
        entry_id: by_submission[submission]
        for entry_id, submission in db.execute(
            select(Entry.id, Entry.client_submission_id).where(
                Entry.client_submission_id.in_(by_submission),
                Entry.deleted_at.is_(None),
            )
        )
    }
