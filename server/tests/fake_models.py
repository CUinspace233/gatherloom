"""Scripted OpenAI-compatible endpoints for tests; no network or keys."""

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx

from app.models import EMBEDDING_DIMENSION
from app.providers import Call, Models, build_models
from app.settings import load_settings

CARD = {
    "title": "离线收集的可靠性",
    "summary": "记录了离线收集需要满足的可靠性要求。",
    "key_points": [{"text": "断网时先写入本机", "chunks": [0]}],
    "keywords": ["离线", "可靠性"],
    "questions": ["离线收集要满足什么？"],
    "kind_hint": "个人想法",
}

Reply = dict[str, Any] | int | Exception


class FakeAPI:
    """Answers chat and embedding requests from queues of scripted replies."""

    def __init__(self) -> None:
        self.chat: list[Reply] = []
        self.embeddings: list[Reply] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.by_task: dict[str, list[Reply]] = {}
        # Relations and decision drafts default to none; tests script them.
        self.defaults: list[tuple[str, Reply]] = [
            ("帮用户整理一个项目里的资料", chat_reply({"relations": []})),
            ("值得做出决定", chat_reply({"drafts": []})),
            (
                "阶段回顾",
                chat_reply(
                    {
                        "new_information": [],
                        "understanding_changes": [],
                        "decisions": [],
                        "open_questions": [],
                        "next_steps": [],
                    }
                ),
            ),
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        path = request.url.path.rsplit("/", 1)[-1]
        self.requests.append((path, body))
        if path == "embeddings":
            reply = self.embeddings.pop(0) if self.embeddings else None
            if reply is None:
                reply = embedding_reply(len(body["input"]))
        else:
            reply = self.scripted(body)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "scripted"}})
        return httpx.Response(200, json=reply)

    def script(self, marker: str, *replies: Reply) -> None:
        """Queue replies for requests whose system prompt contains marker."""
        self.by_task.setdefault(marker, []).extend(replies)

    def scripted(self, body: dict[str, Any]) -> Reply:
        system = body["messages"][0]["content"] if body.get("messages") else ""
        for marker, queue in self.by_task.items():
            if marker in system and queue:
                return queue.pop(0)
        return self.chat.pop(0) if self.chat else self.default_chat(body)

    def default_chat(self, body: dict[str, Any]) -> Reply:
        """Unscripted replies, chosen by the task's system prompt."""
        system = body["messages"][0]["content"] if body.get("messages") else ""
        for marker, reply in self.defaults:
            if marker in system:
                return reply
        return chat_reply(CARD)

    def count(self, path: str) -> int:
        return sum(1 for name, _ in self.requests if name == path)

    def models(self, **env: str) -> Models:
        settings = load_settings(
            {
                "GATHERLOOM_BAILIAN_API_KEY": "test-bailian",
                "GATHERLOOM_BAILIAN_BASE_URL": "https://bailian.test/v1",
                "GATHERLOOM_DEEPSEEK_API_KEY": "test-deepseek",
                "GATHERLOOM_DEEPSEEK_BASE_URL": "https://deepseek.test",
                **env,
            }
        )
        return build_models(settings, transport=httpx.MockTransport(self.handle))


def chat_reply(content: dict[str, Any] | str) -> dict[str, Any]:
    text = (
        content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    )
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
    }


def embedding_reply(count: int) -> dict[str, Any]:
    return {
        "data": [
            {"index": index, "embedding": [0.01 * (index + 1)] * EMBEDDING_DIMENSION}
            for index in range(count)
        ],
        "usage": {"prompt_tokens": 40 * count, "total_tokens": 40 * count},
    }


def collector() -> tuple[list[Call], Callable[[Call], None]]:
    calls: list[Call] = []
    return calls, calls.append


ZERO = Decimal(0)
