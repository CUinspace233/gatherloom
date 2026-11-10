"""Load the evaluation materials into the database and process them.

    uv run --locked --env-file .env python -m evaluation.load

Creates accounts eval_a and eval_b (random passwords; use
``python -m app.admin reset-password eval_a`` to sign in) and their
projects, then saves each material as an entry. Reruns are idempotent. Image
materials carry OCR text only: their attachment rows have no stored file.
Afterwards it runs the ingestion pipeline in this process until every
evaluation entry is processed.
"""

import secrets
import sys
import time
from hashlib import sha256
from typing import Any

from fastapi import Response
from sqlalchemy import func, select

from app import jobs
from app.auth import password_hasher
from app.contracts import EntryCreate
from app.db import SessionLocal
from app.entries import create_entry
from app.ingestion import Pipeline
from app.models import Attachment, Job, Project, User
from app.providers import build_models
from app.settings import load_settings
from app.worker import checkpointer, setup_checkpoints
from evaluation.common import load, materials, submission_id


def ensure_accounts() -> dict[str, User]:
    users = {}
    with SessionLocal() as db:
        for account, projects in load("materials")["accounts"].items():
            user = db.scalar(select(User).where(User.username == account))
            if user is None:
                user = User(
                    username=account,
                    password_hash=password_hasher.hash(secrets.token_urlsafe(24)),
                )
                db.add(user)
                db.flush()
            for name in projects.values():
                if not db.scalar(
                    select(Project.id).where(
                        Project.owner_id == user.id, Project.name == name
                    )
                ):
                    db.add(Project(owner_id=user.id, name=name))
            db.commit()
            users[account] = user
    return users


def save(item: dict[str, Any], user: User) -> None:
    with SessionLocal() as db:
        submission = submission_id(item["account"], item["id"])
        project = db.scalar(
            select(Project.id).where(
                Project.owner_id == user.id,
                Project.name
                == load("materials")["accounts"][item["account"]][item["project"]],
            )
        )
        fields: dict[str, Any] = {
            "client_submission_id": submission,
            "type": item["type"],
            "text": item.get("text", ""),
            "note": item.get("note", ""),
            "project_id": project,
        }
        if item["type"] == "url":
            fields["url"] = item["url"]
            fields["client_extract"] = {"title": item["title"], "text": item["body"]}
        elif item["type"] == "image":
            attachment = db.scalar(
                select(Attachment.id).where(
                    Attachment.owner_id == user.id,
                    Attachment.client_submission_id == submission,
                )
            )
            if attachment is None:
                ocr = item["ocr"].strip()
                row = Attachment(
                    owner_id=user.id,
                    client_submission_id=submission,
                    content_type="image/png",
                    size=0,
                    sha256=sha256(ocr.encode()).hexdigest(),
                )
                db.add(row)
                db.flush()
                attachment = row.id
            fields["attachment_id"] = attachment
            fields["client_ocr"] = {"text": item["ocr"].strip(), "engine": "evaluation"}
        create_entry(EntryCreate.model_validate(fields), user, db, Response())


def process(owners: list[Any]) -> dict[str, int]:
    settings = load_settings()
    if problems := settings.problems():
        raise SystemExit("model configuration: " + "; ".join(problems))
    setup_checkpoints()
    with checkpointer() as saver:
        pipeline = Pipeline(
            SessionLocal, saver, "evaluation-loader", models=build_models(settings)
        )
        while True:
            with SessionLocal() as db:
                pending = db.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.owner_id.in_(owners), Job.status.in_(jobs.ACTIVE))
                )
                claim = jobs.claim(db, "evaluation-loader", jobs.utcnow())
            if claim is None:
                if not pending:
                    break
                time.sleep(2)
                continue
            status = pipeline.run(claim)
            print(f"  job {claim.id} → {status}", file=sys.stderr)
    with SessionLocal() as db:
        return dict(
            db.execute(
                select(Job.status, func.count())
                .where(Job.owner_id.in_(owners))
                .group_by(Job.status)
            ).all()
        )


def main() -> None:
    users = ensure_accounts()
    items = materials()
    for item in items.values():
        save(item, users[item["account"]])
    print(f"Saved {len(items)} materials. Processing…", file=sys.stderr)
    summary = process([user.id for user in users.values()])
    print(f"Jobs: {summary}")


if __name__ == "__main__":
    main()
