from decimal import Decimal

import httpx
import pytest
from fake_models import CARD, FakeAPI, chat_reply, collector

from app import cards, jobs
from app.chunking import Material
from app.providers import Call
from app.settings import load_settings


def test_light_chat_uses_qwen_without_thinking_and_reports_usage() -> None:
    api = FakeAPI()
    calls, record = collector()
    content, route = api.models().router.chat_json(
        "light", [{"role": "user", "content": "你好"}], "test", record
    )
    assert content.startswith("{")
    assert route.provider == "qwen" and route.model == "qwen3.8-flash"
    [(path, body)] = api.requests
    assert path == "completions"
    assert body["enable_thinking"] is False
    assert body["response_format"] == {"type": "json_object"}
    [call] = calls
    assert (call.input_tokens, call.output_tokens, call.error_code) == (1000, 200, None)
    cost, price = call.cost()
    assert price is not None and price.currency == "CNY"
    assert cost == Decimal("0.001340")  # 1000 × 0.8 + 200 × 2.7 per million


def test_heavy_tier_routes_to_deepseek() -> None:
    api = FakeAPI()
    _calls, record = collector()
    _content, route = api.models().router.chat_json("heavy", [], "test", record)
    assert route.provider == "deepseek" and route.model == "deepseek-flash"
    assert "enable_thinking" not in api.requests[0][1]
    assert api.requests[0][1]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(
    ("reply", "error", "code"),
    [
        (429, jobs.Retryable, "rate_limited"),
        (503, jobs.Retryable, "provider_error"),
        (httpx.ConnectError("down"), jobs.Retryable, "network"),
        (401, jobs.Permanent, "auth_failed"),
        (402, jobs.Permanent, "insufficient_balance"),
        (404, jobs.Permanent, "model_unavailable"),
    ],
)
def test_errors_are_classified_and_recorded(
    reply: int | Exception, error: type[jobs.JobError], code: str
) -> None:
    api = FakeAPI()
    api.chat.append(reply)
    calls, record = collector()
    with pytest.raises(error) as raised:
        api.models().router.chat_json("light", [], "test", record)
    assert raised.value.code == code
    assert [call.error_code for call in calls] == [code]


def test_fallback_only_for_retryable_errors() -> None:
    fallback = {
        "GATHERLOOM_LLM_FALLBACK_PROVIDER": "deepseek",
        "GATHERLOOM_LLM_FALLBACK_MODEL": "deepseek-flash",
    }
    api = FakeAPI()
    api.chat.append(503)
    calls, record = collector()
    _content, route = api.models(**fallback).router.chat_json("light", [], "t", record)
    assert route.model == "deepseek-flash"
    assert [call.model for call in calls] == ["qwen3.8-flash", "deepseek-flash"]

    api = FakeAPI()
    api.chat.append(401)
    with pytest.raises(jobs.Permanent):
        api.models(**fallback).router.chat_json("light", [], "t", record)
    assert len(api.requests) == 1


def test_embeddings_are_batched_by_25_and_checked() -> None:
    api = FakeAPI()
    calls, record = collector()
    vectors = api.models().embedder.embed([str(n) for n in range(30)], "embed", record)
    assert len(vectors) == 30
    assert [len(body["input"]) for _path, body in api.requests] == [25, 5]
    assert len(calls) == 2

    api = FakeAPI()
    api.embeddings.append({"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
    with pytest.raises(jobs.Permanent, match="embedding_shape"):
        api.models().embedder.embed(["x"], "embed", record)


def test_unknown_price_is_not_guessed() -> None:
    call = Call("t", "qwen", "some-new-model", 10, 10, 5)
    assert call.cost() == (None, None)
    big = Call("t", "qwen", "qwen3.7-flash-2026-07-15", 40_000, 10, 5)  # tiered
    assert big.cost()[0] is None


def test_worker_settings_report_missing_keys() -> None:
    problems = load_settings({}).problems()
    assert "GATHERLOOM_BAILIAN_API_KEY is not set" in problems
    assert "GATHERLOOM_DEEPSEEK_API_KEY is not set" in problems


def test_card_repair_once_then_permanent() -> None:
    material = Material(type="thought", text="离线优先", note="")
    chunks = [(0, "离线优先")]
    api = FakeAPI()
    bad = {**CARD, "key_points": [{"text": "x", "chunks": [7]}]}
    api.chat += [chat_reply(bad), chat_reply(CARD)]
    calls, record = collector()
    card, _route = cards.generate(api.models().router, material, chunks, record)
    assert card.title == CARD["title"]
    assert len(calls) == 2
    repair = api.requests[1][1]["messages"][-1]["content"]
    assert "不存在的片段编号：[7]" in repair

    api = FakeAPI()
    api.chat += [chat_reply("不是 JSON"), chat_reply({**CARD, "kind_hint": "其他"})]
    with pytest.raises(jobs.Permanent, match="invalid_output"):
        cards.generate(api.models().router, material, chunks, record)
