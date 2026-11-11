"""Grounded generation: numbered evidence, [n] markers, validation and repair.

Answers, summaries, decision drafts and reviews all cite numbered source
chunks. The model replies with JSON; the reply is parsed with a Pydantic
model, its [n] markers are checked against the evidence numbers, and an
invalid reply gets exactly one repair turn before the task fails permanently.
"""

import json
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from app.jobs import Permanent
from app.providers import Recorder, Router

MARKER = re.compile(r"\[(\d+)\]")


def markers(text: str) -> set[int]:
    return {int(n) for n in MARKER.findall(text)}


def strip_markers(text: str) -> str:
    return MARKER.sub("", text).strip()


def parse[T: BaseModel](content: str, model: type[T]) -> T:
    """Parse a JSON reply. Raises ValueError with a message for the repair turn."""
    try:
        return model.model_validate(json.loads(content))
    except json.JSONDecodeError as error:
        raise ValueError(f"不是合法 JSON：{error.msg}") from error
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
            for item in error.errors()
        )
        raise ValueError(f"字段不符合要求：{problems}") from error


def check_markers(
    texts: Iterable[str], cited: Iterable[int], numbers: set[int]
) -> None:
    """Every [n] in the texts and every cited number must be known evidence,
    and the markers must match the declared citations exactly."""
    found = set().union(*(markers(text) for text in texts))
    declared = set(cited)
    if unknown := (found | declared) - numbers:
        raise ValueError(f"引用了不存在的证据编号：{sorted(unknown)}")
    if found != declared:
        raise ValueError(
            f"正文中的标注 {sorted(found)} 与 citations {sorted(declared)} 不一致"
        )


def evidence_block(evidence: Sequence[dict[str, Any]], limit: int) -> list[str]:
    """Prompt lines for numbered evidence, labelled personal or external."""
    lines = []
    for item in evidence:
        kind = "个人记录" if item["type"] == "thought" else "外部资料"
        label = {"thought": "想法", "url": "网页", "image": "截图"}[item["type"]]
        title = f" · {item['title']}" if item.get("title") else ""
        lines.append(
            f"[{item['n']}]（{kind} · {label}{title} · 记录于 {item['created_at'][:10]}）\n"
            f"{item['text'][:limit]}"
        )
    return lines


def ask[T](
    router: Router,
    tier: str,
    messages: list[dict[str, str]],
    task: str,
    record: Recorder,
    check: Callable[[str], T],
) -> tuple[T, str]:
    """Call the model, validate with ``check``, repair once, else fail."""
    content, route = router.chat_json(tier, messages, task, record)
    try:
        return check(content), route.model
    except ValueError as error:
        messages = [
            *messages,
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": f"上一次输出不合格：{error}。请只输出修正后的 JSON。",
            },
        ]
    content, route = router.chat_json(tier, messages, task, record)
    try:
        return check(content), route.model
    except ValueError as error:
        raise Permanent("invalid_output") from error
