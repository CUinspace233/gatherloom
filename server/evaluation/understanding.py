"""Project understanding evaluation: summary, relations, drafts and review.

    uv run --locked --env-file .env python -m evaluation.understanding

Runs project maintenance and a manual review for eval_a's project A1 with the
real models, then writes docs/evaluation/results/<date>-understanding.md with
the full output for human review. Automatic checks: every citation stays in
A1; nothing calls a draft or a personal note a confirmed decision (there are
no confirmed decisions in the set); a summary that mentions the storage
choice relies on the later decision (a1-05).
"""

import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update

from app import events, jobs, maintenance, reviews
from app.db import SessionLocal
from app.models import (
    Decision,
    Job,
    Project,
    ProjectEntry,
    ProjectEvent,
    ProjectQuestion,
    Relation,
    Review,
    Run,
    SummaryRevision,
)
from app.providers import build_models
from app.settings import load_settings
from app.worker import checkpointer, setup_checkpoints
from evaluation.common import DOCS, entry_ids, owner_id, project_id

QUESTIONS = ["检索结果要不要加 reranker？", "上传失败时怎么避免重复提交？"]
FORBIDDEN = ("已决定", "已确认", "最终决定")


def prepare() -> tuple[UUID, UUID]:
    with SessionLocal.begin() as db:
        owner = owner_id(db, "eval_a")
        project = project_id(db, "eval_a", "A1")
        existing = set(
            db.scalars(
                select(ProjectQuestion.text).where(
                    ProjectQuestion.project_id == project
                )
            )
        )
        # Evaluation data only: suggestions and drafts are regenerated each run.
        db.execute(delete(Relation).where(Relation.project_id == project))
        db.execute(delete(Decision).where(Decision.project_id == project))
        # Material loaded before S5 has no events; record them as real use would.
        if not db.scalar(
            select(func.count())
            .select_from(ProjectEvent)
            .where(
                ProjectEvent.project_id == project,
                ProjectEvent.kind == events.ENTRY_READY,
            )
        ):
            for entry_id in db.scalars(
                select(ProjectEntry.entry_id).where(ProjectEntry.project_id == project)
            ):
                events.record(db, owner, project, events.ENTRY_READY, entry_id)
        for text in QUESTIONS:
            if text not in existing:
                question = ProjectQuestion(
                    owner_id=owner, project_id=project, text=text
                )
                db.add(question)
                db.flush()
                events.record(db, owner, project, events.QUESTION_ADDED, question.id)
        events.schedule_maintenance(db, owner, project, delay=timedelta(0))
        # Do not wait for the debounce window in an evaluation run.
        db.execute(
            update(Job)
            .where(
                Job.project_id == project,
                Job.kind == jobs.MAINTENANCE,
                Job.status == "queued",
            )
            .values(run_after=func.now())
        )
    return owner, project


def run(kinds: tuple[str, ...], project: UUID) -> None:
    settings = load_settings()
    if problems := settings.problems():
        raise SystemExit("model configuration: " + "; ".join(problems))
    setup_checkpoints()
    models = build_models(settings)
    with checkpointer() as saver:
        pipelines = {
            jobs.MAINTENANCE: maintenance.Pipeline(
                SessionLocal, saver, "eval-understanding", models=models
            ),
            jobs.REVIEW: reviews.Pipeline(
                SessionLocal, saver, "eval-understanding", models=models
            ),
        }
        while True:
            with SessionLocal() as db:
                pending = db.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(
                        Job.project_id == project,
                        Job.kind.in_(kinds),
                        Job.status.in_(jobs.ACTIVE),
                        Job.run_after <= func.now(),
                    )
                )
                if not pending:
                    return
                claim = jobs.claim(db, "eval-understanding", jobs.utcnow())
            if claim is None or claim.kind not in kinds:
                if claim is not None:
                    raise SystemExit(
                        f"claimed a {claim.kind} job; stop other workers first"
                    )
                continue
            print(
                f"  {claim.kind} → {pipelines[claim.kind].run(claim)}", file=sys.stderr
            )


def cost(db: Any, project: UUID, since: datetime) -> str:
    rows = db.execute(
        select(Run.task, func.count(), func.sum(Run.cost_estimate), Run.currency)
        .join(Job, Job.id == Run.job_id)
        .where(Job.project_id == project, Run.created_at >= since)
        .group_by(Run.task, Run.currency)
    ).all()
    return "；".join(
        f"{task} {count} 次 {(total or Decimal(0)):.4f} {currency or ''}".strip()
        for task, count, total, currency in rows
    )


def main() -> None:
    started = datetime.now(UTC)
    owner, project = prepare()
    print("Running maintenance…", file=sys.stderr)
    run((jobs.MAINTENANCE,), project)
    with SessionLocal.begin() as db:
        # Evaluation data only: review the whole project every run.
        db.execute(delete(Review).where(Review.project_id == project))
        start = db.scalar(select(Project.created_at).where(Project.id == project))
        assert start is not None
        reviews.queue_review(db, owner, project, "manual", start, datetime.now(UTC))
    print("Running a manual review…", file=sys.stderr)
    run((jobs.REVIEW,), project)

    with SessionLocal() as db:
        names = entry_ids(db)
        in_a1 = {name for name in names.values() if name.startswith("a1-")}
        summary = db.scalar(
            select(SummaryRevision)
            .where(SummaryRevision.project_id == project)
            .order_by(SummaryRevision.version.desc())
            .limit(1)
        )
        relations = list(
            db.scalars(
                select(Relation)
                .where(Relation.project_id == project, Relation.created_at >= started)
                .order_by(Relation.created_at)
            )
        )
        drafts = list(
            db.scalars(
                select(Decision)
                .where(Decision.project_id == project, Decision.created_at >= started)
                .order_by(Decision.created_at)
            )
        )
        review = db.scalar(
            select(Review)
            .where(Review.project_id == project)
            .order_by(Review.created_at.desc())
            .limit(1)
        )
        questions = {q.id: q.text for q in db.scalars(select(ProjectQuestion))}
        spent = cost(db, project, started)

    def cited(items: list[dict[str, Any]]) -> list[str]:
        return [names.get(UUID(i["entry_id"]), "?") for i in items]

    checks: list[tuple[str, bool, str]] = []
    texts: list[str] = []
    if summary:
        summary_sources = cited(summary.sources)
        texts.append(summary.content["understanding"])
        texts += [p["text"] for p in summary.content["key_points"]]
        checks.append(
            (
                "总结引用都在 A1",
                set(summary_sources) <= in_a1,
                ", ".join(summary_sources),
            )
        )
        mentions_storage = "SQLite" in "".join(texts) or "存储" in "".join(texts)
        checks.append(
            (
                "提到存储方案时以更新后的决定（a1-05）为准",
                not mentions_storage or "a1-05" in summary_sources,
                "提到存储" if mentions_storage else "未提到存储",
            )
        )
    draft_sources = [name for d in drafts for name in cited(d.sources)]
    checks.append(
        (
            "决定草稿引用都在 A1",
            set(draft_sources) <= in_a1,
            ", ".join(draft_sources) or "—",
        )
    )
    texts += [d.title + d.choice + d.rationale for d in drafts]
    if review:
        texts += [
            item["text"]
            for section in reviews.SECTIONS
            for item in review.content[section]
        ]
        review_sources = cited(review.sources)
        checks.append(
            (
                "回顾引用都在 A1",
                set(review_sources) <= in_a1,
                ", ".join(review_sources) or "—",
            )
        )
    found = [word for word in FORBIDDEN if any(word in text for text in texts)]
    checks.append(
        ("不把草稿或个人记录称为已确认的决定", not found, ", ".join(found) or "—")
    )
    relation_targets = [
        names.get(r.target_entry_id, "?") for r in relations if r.target_entry_id
    ]
    checks.append(
        (
            "关联建议的资料都在 A1",
            set(relation_targets) <= in_a1,
            ", ".join(relation_targets) or "—",
        )
    )

    lines = [
        f"# 项目认识评测 {date.today().isoformat()}",
        "",
        "> 材料由开发助手编写，结果偏乐观。自动检查只覆盖可机械判断的部分，内容质量需要人工阅读下文。",
        "",
        f"- 本次花费：{spent or '—'}",
        "",
        "## 自动检查",
        "",
        "| 检查 | 结果 | 说明 |",
        "|---|---|---|",
        *[
            f"| {name} | {'通过' if ok else '失败'} | {note} |"
            for name, ok, note in checks
        ],
        "",
        "## 总结",
        "",
    ]
    if summary:
        lines += [
            f"第 {summary.version} 版 · 覆盖 {summary.coverage} · 变化：{summary.change_note}",
            "",
            *[f"> {line}" for line in summary.content["understanding"].splitlines()],
            "",
            "要点：",
            *[f"- {p['text']}" for p in summary.content["key_points"]],
            "",
            "不确定项：",
            *[f"- {u}" for u in summary.content["uncertainties"]],
            "",
            f"引用：{', '.join(f'[{i["n"]}] {names.get(UUID(i["entry_id"]), "?")}' for i in summary.sources)}",
        ]
    lines += ["", "## 关联建议", ""]
    for relation in relations:
        target = (
            names.get(relation.target_entry_id, "?")
            if relation.target_entry_id
            else f"待解问题「{questions.get(relation.target_question_id, '?')}」"  # type: ignore[arg-type]
        )
        lines.append(
            f"- {names.get(relation.entry_id, '?')} → {target}：{relation.reason}"
        )
    lines += ["", "## 决定草稿", ""]
    for draft in drafts:
        lines += [
            f"### {draft.title}",
            "",
            f"- 建议：{draft.choice}",
            f"- 理由：{draft.rationale}",
            f"- 备选：{'；'.join(draft.alternatives) or '—'}",
            f"- 前提：{'；'.join(draft.premises) or '—'}",
            f"- 依据：{', '.join(cited(draft.sources))}",
            "",
        ]
    lines += ["## 回顾", ""]
    if review:
        titles = {
            "new_information": "本期新增信息",
            "understanding_changes": "认识变化",
            "decisions": "正式决定",
            "open_questions": "未解决问题",
            "next_steps": "下一阶段建议",
        }
        for section in reviews.SECTIONS:
            lines.append(f"**{titles[section]}**")
            lines += [f"- {item['text']}" for item in review.content[section]] or [
                "- （无）"
            ]
            lines.append("")
    path = DOCS / "results" / f"{date.today().isoformat()}-understanding.md"
    path.write_text("\n".join(lines) + "\n")
    print(f"Report: {path}")
    for name, ok, note in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")


if __name__ == "__main__":
    main()
