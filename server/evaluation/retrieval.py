"""Retrieval evaluation: Top 5 hits and MRR per configuration.

    uv run --locked --env-file .env python -m evaluation.retrieval

Runs every question in retrieval.yaml under several retrieval
configurations, checks the search boundaries in scope.yaml, measures
semantic distances to help choose a cutoff, and writes a report to
docs/evaluation/results/<date>-retrieval.md.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app import cards, chunking, search, tokenizer
from app.db import SessionLocal
from app.models import EMBEDDING_DIMENSION, INDEX_VERSION, Chunk, Entry, ProjectEntry
from app.providers import Embedder, Recorder, build_models
from app.settings import load_settings
from evaluation.common import DOCS, entry_ids, load, materials, owner_id, project_id

CONFIGS: dict[str, dict[str, Any]] = {
    "keyword": {"paths": ("keyword",)},
    "semantic": {"paths": ("semantic",)},
    "hybrid": {"card_weight": 0.0},
    "hybrid+card0.5": {"card_weight": 0.5},
    "hybrid+card1.0": {"card_weight": 1.0},
}


class CachedEmbedder:
    """Embeds each query once across configurations."""

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder
        self.cache: dict[str, list[float]] = {}

    def embed(
        self, texts: Sequence[str], task: str, record: Recorder
    ) -> list[list[float]]:
        missing = [text for text in texts if text not in self.cache]
        if missing:
            for text, vector in zip(
                missing, self.embedder.embed(missing, task, record), strict=True
            ):
                self.cache[text] = vector
        return [self.cache[text] for text in texts]


@dataclass
class Outcome:
    question: dict[str, Any]
    ranks: dict[str, int | None]  # config → rank of the first target (1-based)


def scope_filters(db: Session, item: dict[str, Any]) -> search.Filters:
    if item["scope"] == "global":
        return search.Filters()
    return search.Filters(project_id=project_id(db, item["account"], item["scope"]))


def ranked(
    db: Session,
    item: dict[str, Any],
    embedder: CachedEmbedder,
    names: dict[UUID, str],
    config: dict[str, Any],
) -> list[str]:
    result = search.retrieve(
        db,
        owner_id(db, item["account"]),
        item["query"],
        filters=scope_filters(db, item),
        embedder=embedder,  # type: ignore[arg-type]
        record=lambda _call: None,
        **config,
    )
    return [names.get(hit.entry_id, "?") for hit in result.hits]


def first_rank(results: list[str], targets: list[str]) -> int | None:
    for rank, name in enumerate(results, start=1):
        if name in targets:
            return rank
    return None


def nearest(
    db: Session, item: dict[str, Any], vector: list[float], only: list[str] | None
) -> tuple[float | None, str | None]:
    """Smallest cosine distance from the query to a chunk in scope."""
    filters = scope_filters(db, item)
    query = (
        select(Chunk.entry_id, func.min(Chunk.embedding.cosine_distance(vector)))
        .join(
            Entry,
            and_(
                Entry.id == Chunk.entry_id,
                Entry.current_revision == Chunk.revision,
                Entry.deleted_at.is_(None),
            ),
        )
        .where(
            Chunk.owner_id == owner_id(db, item["account"]),
            Chunk.embedding.is_not(None),
        )
        .group_by(Chunk.entry_id)
    )
    if filters.project_id is not None:
        query = query.join(
            ProjectEntry,
            and_(
                ProjectEntry.entry_id == Chunk.entry_id,
                ProjectEntry.project_id == filters.project_id,
            ),
        )
    names = entry_ids(db)
    rows = [
        (distance, names.get(entry_id))
        for entry_id, distance in db.execute(query)
        if only is None or names.get(entry_id) in only
    ]
    if not rows:
        return None, None
    distance, name = min(rows, key=lambda row: row[0])
    return float(distance), name


def report(
    outcomes: list[Outcome],
    scope_rows: list[str],
    distances: list[str],
    models: dict[str, str],
) -> str:
    kinds = {item["id"]: item["type"] for item in materials().values()}
    lines = [
        f"# 检索评测 {date.today().isoformat()}",
        "",
        (
            "> 评测材料与题目由开发助手编写（见 materials.yaml），题目措辞虽尽量避免照搬原文，"
            "仍与材料出自同一作者，结果偏乐观，不能代替真实材料的评测。"
        ),
        "",
        "## 配置",
        "",
        f"- Embedding：{models['embedding']}，{EMBEDDING_DIMENSION} 维，index_version {INDEX_VERSION}",
        f"- 分词：jieba 搜索模式，tokenizer_version {tokenizer.TOKENIZER_VERSION}，查询侧去停用词",
        (
            f"- 分块：整条上限 {chunking.WHOLE_LIMIT} 字，目标 {chunking.TARGET} 字，"
            f"上限 {chunking.MAX} 字，重叠一句（≤ {chunking.OVERLAP_MAX} 字）"
        ),
        (
            f"- 上下文头：标题 + 备注前 {chunking.NOTE_EXCERPT} 字，"
            f"context_header_version {chunking.CONTEXT_HEADER_VERSION}（R1 始终开启，未做消融）"
        ),
        (
            f"- 知识卡片：prompt_version {cards.PROMPT_VERSION}，light = {models['light']}；"
            "卡片路不含“可回答的问题”（R3 关闭）"
        ),
        f"- 融合：RRF k = {search.RRF_K}，每路候选 {search.CANDIDATES}，按资料取最高分",
        "",
        "## 汇总（20 题）",
        "",
        "| 配置 | Top 5 命中 | MRR |",
        "|---|---:|---:|",
    ]
    for name in CONFIGS:
        ranks = [outcome.ranks[name] for outcome in outcomes]
        hits = sum(1 for rank in ranks if rank is not None and rank <= 5)
        mrr = sum(1 / rank for rank in ranks if rank is not None) / len(ranks)
        lines.append(f"| {name} | {hits}/{len(ranks)} | {mrr:.3f} |")
    lines += ["", "## 按材料类型（hybrid+card1.0，按首个目标的类型）", ""]
    by_kind: dict[str, list[int | None]] = defaultdict(list)
    for outcome in outcomes:
        by_kind[kinds[outcome.question["targets"][0]]].append(
            outcome.ranks["hybrid+card1.0"]
        )
    lines += ["| 类型 | 题数 | Top 5 命中 |", "|---|---:|---:|"]
    for kind, ranks in sorted(by_kind.items()):
        hits = sum(1 for rank in ranks if rank is not None and rank <= 5)
        lines.append(f"| {kind} | {len(ranks)} | {hits} |")
    lines += [
        "",
        "## 逐题排名（— 表示未进入候选）",
        "",
        "| 题目 | 查询 | 目标 | " + " | ".join(CONFIGS) + " |",
        "|---|---|---|" + "---:|" * len(CONFIGS),
    ]
    for outcome in outcomes:
        question = outcome.question
        cells = [str(outcome.ranks[name] or "—") for name in CONFIGS]
        lines.append(
            f"| {question['id']} | {question['query']} | {', '.join(question['targets'])} | "
            + " | ".join(cells)
            + " |"
        )
    lines += ["", "## 范围检查（scope.yaml）", "", *scope_rows]
    lines += ["", "## 语义距离（选择 max_distance 的参考）", "", *distances]
    return "\n".join(lines) + "\n"


def main() -> None:
    settings = load_settings()
    if not settings.bailian_api_key:
        raise SystemExit("GATHERLOOM_BAILIAN_API_KEY is required")
    embedder = CachedEmbedder(build_models(settings).embedder)
    questions = load("retrieval")["questions"]
    outcomes = []
    with SessionLocal() as db:
        names = entry_ids(db)
        for question in questions:
            ranks = {
                name: first_rank(
                    ranked(db, question, embedder, names, config), question["targets"]
                )
                for name, config in CONFIGS.items()
            }
            outcomes.append(Outcome(question, ranks))
            print(question["id"], ranks)

        scope_rows = ["| 检查 | 结果 | 说明 |", "|---|---|---|"]
        for check in load("scope")["search"]:
            results = ranked(db, check, embedder, names, CONFIGS["hybrid+card1.0"])
            leaked = [name for name in results if name in check["forbidden"]]
            expected = check.get("expected")
            found = expected is None or first_rank(results[:5], expected) is not None
            ok = not leaked and found
            detail = (
                f"越界：{', '.join(leaked)}"
                if leaked
                else ("" if found else "未命中预期")
            )
            scope_rows.append(
                f"| {check['id']} | {'通过' if ok else '失败'} | {check['tests']} {detail} |"
            )

        distances = [
            "| 题目 | 类型 | 最近距离 | 最近资料 |",
            "|---|---|---:|---|",
        ]
        for question in questions:
            [vector] = embedder.embed([question["query"]], "eval", lambda _call: None)
            distance, name = nearest(db, question, vector, question["targets"])
            distances.append(
                f"| {question['id']} | 有答案（目标） | {distance:.3f} | {name} |"
                if distance is not None
                else f"| {question['id']} | 有答案（目标） | — | — |"
            )
        for question in load("no_answer")["questions"]:
            [vector] = embedder.embed([question["query"]], "eval", lambda _call: None)
            distance, name = nearest(db, question, vector, None)
            distances.append(
                f"| {question['id']} | 无答案（范围内最近） | "
                + (f"{distance:.3f} | {name} |" if distance is not None else "— | — |")
            )

    models = {
        "embedding": settings.embedding_model,
        "light": f"{settings.light.provider}/{settings.light.model}",
    }
    path = DOCS / "results" / f"{date.today().isoformat()}-retrieval.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(report(outcomes, scope_rows, distances, models))
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
