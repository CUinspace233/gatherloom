"""Answer evaluation: insufficient-information and scope checks.

    uv run --locked --env-file .env python -m evaluation.answers

Asks the no-answer questions (no_answer.yaml) and the scoped questions
(scope.yaml, section answer) through the real answer pipeline and models,
then writes docs/evaluation/results/<date>-answers.md with every answer for
human review. Automatic checks cover only what can be checked mechanically:
insufficient flags, citations staying in the project, and leak terms.
Whether a citation actually supports the sentence needs a human.
"""

import sys
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app import answering, jobs
from app.db import SessionLocal
from app.models import Citation, Conversation, Job, Message, ProjectEntry
from app.providers import build_models
from app.settings import load_settings
from app.worker import checkpointer, setup_checkpoints
from evaluation.common import DOCS, entry_ids, load, owner_id, project_id


def ask(item: dict[str, Any]) -> UUID:
    with SessionLocal.begin() as db:
        conversation = Conversation(
            owner_id=owner_id(db, item["account"]),
            project_id=project_id(db, item["account"], item["scope"]),
            title=f"评测 {item['id']} {date.today().isoformat()}",
        )
        db.add(conversation)
        db.flush()
        message = Message(
            owner_id=conversation.owner_id,
            conversation_id=conversation.id,
            role="user",
            content=item["query"],
            client_message_id=uuid4(),
        )
        db.add(message)
        db.flush()
        db.add(
            Job(owner_id=conversation.owner_id, kind=jobs.ANSWER, message_id=message.id)
        )
        return message.id


def run_all(questions: list[UUID]) -> None:
    settings = load_settings()
    if problems := settings.problems():
        raise SystemExit("model configuration: " + "; ".join(problems))
    setup_checkpoints()
    with checkpointer() as saver:
        pipeline = answering.Pipeline(
            SessionLocal, saver, "evaluation-answers", models=build_models(settings)
        )
        while True:
            with SessionLocal() as db:
                pending = db.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.message_id.in_(questions), Job.status.in_(jobs.ACTIVE))
                )
                if not pending:
                    return
                claim = jobs.claim(db, "evaluation-answers", jobs.utcnow())
            if claim is None or claim.kind != jobs.ANSWER:
                if claim is not None:
                    raise SystemExit("an ingestion job was claimed; stop other workers")
                continue
            print(f"  {claim.message_id} → {pipeline.run(claim)}", file=sys.stderr)


def outcome(question_id: UUID) -> dict[str, Any]:
    with SessionLocal() as db:
        question = db.get_one(Message, question_id)
        conversation = db.get_one(Conversation, question.conversation_id)
        job = db.scalars(select(Job).where(Job.message_id == question_id)).one()
        answer = db.scalar(select(Message).where(Message.reply_to == question_id))
        names = entry_ids(db)
        project_entries = set(
            db.scalars(
                select(ProjectEntry.entry_id).where(
                    ProjectEntry.project_id == conversation.project_id
                )
            )
        )
        cited = (
            list(
                db.scalars(
                    select(Citation.entry_id)
                    .where(Citation.message_id == answer.id)
                    .order_by(Citation.n)
                )
            )
            if answer
            else []
        )
        trace = question.trace or {}
        return {
            "status": job.status,
            "error": job.error_code,
            "answer": answer.content if answer else "",
            "insufficient": bool(answer and answer.insufficient),
            "cited": [names.get(entry_id, "?") for entry_id in cited],
            "outside": [names.get(e, "?") for e in cited if e not in project_entries],
            "evidence": [
                names.get(UUID(item["entry_id"]), "?")
                for item in trace.get("evidence", [])
            ],
            "model": trace.get("model"),
        }


def main() -> None:
    no_answer = load("no_answer")["questions"]
    scoped = load("scope")["answer"]
    asked = {item["id"]: ask(item) for item in [*no_answer, *scoped]}
    print(f"Asked {len(asked)} questions. Answering…", file=sys.stderr)
    run_all(list(asked.values()))
    results = {key: outcome(message_id) for key, message_id in asked.items()}

    lines = [
        f"# 问答评测 {date.today().isoformat()}",
        "",
        (
            "> 题目与材料由开发助手编写，结果偏乐观。自动检查只覆盖“是否声明资料不足”“引用是否在项目内”（资料不足时允许引用部分相关证据）"
            "“是否出现其他资料独有的答案片段”；引用是否真正支持回答需要人工判断，下方列出全文供检查。"
        ),
        "",
        f"- heavy 模型：{next((r['model'] for r in results.values() if r['model']), '—')}",
        f"- 每题证据数：{answering.EVIDENCE}，prompt_version {answering.PROMPT_VERSION}",
        "",
        "## 无答案题（no_answer.yaml）",
        "",
        "| 题目 | 资料不足 | 引用 | 越界引用 | 泄露词 | 结果 |",
        "|---|---|---|---|---|---|",
    ]
    passed = 0
    for item in no_answer:
        result = results[item["id"]]
        leaks = [
            term for term in item.get("leak_terms", []) if term in result["answer"]
        ]
        # Insufficient answers may cite partly relevant evidence from the project.
        ok = (
            result["status"] == "succeeded"
            and result["insufficient"]
            and not result["outside"]
            and not leaks
        )
        passed += ok
        lines.append(
            f"| {item['id']} {item['query']} | {'是' if result['insufficient'] else '否'} | "
            f"{', '.join(result['cited']) or '—'} | {', '.join(result['outside']) or '—'} | "
            f"{', '.join(leaks) or '—'} | {'通过' if ok else '失败'} |"
        )
    lines += [
        "",
        f"通过 {passed}/{len(no_answer)}。",
        "",
        "## 范围题（scope.yaml answer）",
        "",
    ]
    lines += ["| 题目 | 引用 | 越界引用 | 命中预期 | 结果 |", "|---|---|---|---|---|"]
    for item in scoped:
        result = results[item["id"]]
        hit = sorted(set(result["cited"]) & set(item["expected"]))
        ok = result["status"] == "succeeded" and not result["outside"] and bool(hit)
        lines.append(
            f"| {item['id']} {item['query']} | {', '.join(result['cited']) or '—'} | "
            f"{', '.join(result['outside']) or '—'} | {', '.join(hit) or '—'} | "
            f"{'通过' if ok else '失败'} |"
        )
    lines += ["", "## 回答全文（人工检查引用支持度）", ""]
    for key, result in results.items():
        lines += [
            f"### {key}",
            "",
            f"- 状态：{result['status']}{'（' + result['error'] + '）' if result['error'] else ''}",
            f"- 检索到的证据：{', '.join(result['evidence']) or '无'}",
            "",
            *[f"> {line}" for line in (result["answer"] or "（无回答）").splitlines()],
            "",
        ]
    path = DOCS / "results" / f"{date.today().isoformat()}-answers.md"
    path.write_text("\n".join(lines))
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
