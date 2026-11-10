"""Knowledge cards (ADR 0009): one light-tier call per entry revision.

A card is a derived navigation aid, never evidence: answers cite source chunks,
not cards. Output is validated with Pydantic, including that key points refer
to chunk numbers that exist; an invalid reply gets one repair attempt.
"""

import json
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.chunking import Material
from app.jobs import Permanent
from app.providers import Recorder, Router
from app.settings import Route

PROMPT_VERSION = 1
MAX_INPUT_CHARS = 12_000
TASK = "knowledge_card"

Short = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
]


class KeyPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
    ]
    chunks: Annotated[list[int], Field(max_length=5)]


class Card(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Short
    summary: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)
    ]
    key_points: Annotated[list[KeyPoint], Field(max_length=5)]
    keywords: Annotated[
        list[
            Annotated[
                str,
                StringConstraints(strip_whitespace=True, min_length=1, max_length=40),
            ]
        ],
        Field(max_length=8),
    ]
    questions: Annotated[
        list[Annotated[str, StringConstraints(max_length=200)]], Field(max_length=3)
    ]
    kind_hint: Literal["观点", "事实", "教程", "数据", "个人想法"]

    def index_text(self) -> str:
        """What the card path indexes (ADR 0009 R2; questions stay out for now)."""
        return "\n".join(
            [
                self.title,
                self.summary,
                " ".join(self.keywords),
                *(point.text for point in self.key_points),
            ]
        )


SYSTEM = """你是个人知识库的整理助手。阅读一条用户收藏的资料，输出一个 JSON 对象，字段如下：
- title：不超过 30 字的中文标题，概括资料主题。
- summary：1–3 句中文摘要，只写资料里有的信息。
- key_points：最多 5 条要点，每条 {"text": 要点, "chunks": [来源片段编号]}，编号必须来自资料中的 [片段 N]。
- keywords：最多 8 个主题词或实体，专有名词保留原文写法（如 SwiftUI）。
- questions：最多 3 个这条资料能回答的问题。
- kind_hint：观点、事实、教程、数据、个人想法 之一。
不要编造资料中没有的事实。只输出 JSON。"""


def user_prompt(material: Material, chunks: Sequence[tuple[int, str]]) -> str:
    kind = {"thought": "个人想法", "url": "网页链接", "image": "截图"}[material.type]
    lines = [f"资料类型：{kind}"]
    if material.title:
        lines.append(f"页面标题：{material.title}")
    if material.source_url:
        lines.append(f"来源：{material.source_url}")
    if material.note.strip():
        lines.append(f"用户备注：{material.note.strip()}")
    budget = MAX_INPUT_CHARS
    body = []
    for ordinal, text in chunks:
        if budget <= 0:
            break
        body.append(f"[片段 {ordinal}]\n{text[:budget]}")
        budget -= len(text)
    if budget < 0 or len(body) < len(chunks):
        lines.append("（资料较长，以下为节选。）")
    lines.append("资料内容：\n" + ("\n\n".join(body) if body else "（无正文）"))
    return "\n".join(lines)


def parse(content: str, ordinals: set[int]) -> Card:
    """Validate a reply. Raises ValueError with a message for the repair turn."""
    try:
        card = Card.model_validate(json.loads(content))
    except json.JSONDecodeError as error:
        raise ValueError(f"不是合法 JSON：{error.msg}") from error
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
            for item in error.errors()
        )
        raise ValueError(f"字段不符合要求：{problems}") from error
    unknown = {n for point in card.key_points for n in point.chunks} - ordinals
    if unknown:
        raise ValueError(f"key_points 引用了不存在的片段编号：{sorted(unknown)}")
    return card


def generate(
    router: Router,
    material: Material,
    chunks: Sequence[tuple[int, str]],
    record: Recorder,
) -> tuple[Card, Route]:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_prompt(material, chunks)},
    ]
    ordinals = {ordinal for ordinal, _ in chunks}
    content, route = router.chat_json("light", messages, TASK, record)
    try:
        return parse(content, ordinals), route
    except ValueError as error:
        messages += [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": f"上一次输出不合格：{error}。请只输出修正后的 JSON。",
            },
        ]
    content, route = router.chat_json("light", messages, TASK, record)
    try:
        return parse(content, ordinals), route
    except ValueError as error:
        raise Permanent("invalid_output") from error
