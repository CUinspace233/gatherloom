"""Model providers over OpenAI-compatible HTTP APIs (DeepSeek, Bailian).

Every HTTP call, successful or not, is reported to a ``Recorder`` so the
caller can store usage and cost. Errors are classified for the job layer:
network trouble, rate limits and server errors are retryable; credentials,
balance, unknown models and bad requests are not. A fallback route is only
tried for retryable errors, never after an auth or billing failure (ADR 0002).
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from app.jobs import JobError, Permanent, Retryable
from app.settings import Route, Settings

EMBEDDING_BATCH = 25  # Bailian rejects larger batches.


@dataclass(frozen=True)
class Price:
    """Per million tokens. Dated, because providers change prices."""

    input: Decimal
    output: Decimal
    currency: str
    checked: date
    max_input_tokens: int | None = None  # beyond this the tier price differs


PRICE_DATE = date(2026, 11, 10)
PRICES: dict[tuple[str, str], Price] = {
    # Bailian China site, first tier (input ≤ 32K tokens), thinking or not.
    ("qwen", "qwen3.7-flash-2026-07-15"): Price(
        Decimal("0.2"), Decimal("0.8"), "CNY", PRICE_DATE, 32_000
    ),
    ("qwen", "qwen3.7-text-embedding-flash"): Price(
        Decimal("0.125"), Decimal(0), "CNY", PRICE_DATE
    ),
    # DeepSeek peak-hour rates; off-peak is half, so this is an upper bound.
    ("deepseek", "deepseek-v4-pro"): Price(
        Decimal("1.32"), Decimal("3.96"), "USD", PRICE_DATE
    ),
    ("deepseek", "deepseek-flash"): Price(
        Decimal("0.3"), Decimal("1.2"), "USD", PRICE_DATE
    ),
}


@dataclass(frozen=True)
class Call:
    """One HTTP call to a model, for the runs table."""

    task: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    error_code: str | None = None

    def cost(self) -> tuple[Decimal | None, Price | None]:
        price = PRICES.get((self.provider, self.model))
        if price is None or (
            price.max_input_tokens is not None
            and self.input_tokens > price.max_input_tokens
        ):
            return None, price
        total = (
            price.input * self.input_tokens + price.output * self.output_tokens
        ) / Decimal(1_000_000)
        return total.quantize(Decimal("0.000001")), price


Recorder = Callable[[Call], None]


def classify(response: httpx.Response) -> JobError:
    status = response.status_code
    if status == 429:
        return Retryable("rate_limited")
    if status >= 500:
        return Retryable("provider_error")
    if status in (401, 403):
        return Permanent("auth_failed")
    if status == 402:
        return Permanent("insufficient_balance")
    if status == 404:
        return Permanent("model_unavailable")
    if "arrearage" in response.text.lower() or "quota" in response.text.lower():
        return Permanent("insufficient_balance")
    return Permanent("bad_request")


class Client:
    """A minimal OpenAI-compatible client for one provider."""

    def __init__(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.provider = provider
        self.http = httpx.Client(
            base_url=base_url + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def post(
        self, path: str, body: dict[str, Any], task: str, record: Recorder
    ) -> dict[str, Any]:
        model = str(body["model"])
        started = time.monotonic()

        def report(usage: dict[str, Any], error: str | None) -> None:
            record(
                Call(
                    task=task,
                    provider=self.provider,
                    model=model,
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error_code=error,
                )
            )

        try:
            response = self.http.post(path, json=body)
        except httpx.TimeoutException as error:
            report({}, "timeout")
            raise Retryable("timeout") from error
        except httpx.TransportError as error:
            report({}, "network")
            raise Retryable("network") from error
        if response.status_code != 200:
            failure = classify(response)
            report({}, failure.code)
            raise failure
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as error:
            report({}, "invalid_response")
            raise Retryable("invalid_response") from error
        report(payload.get("usage") or {}, None)
        return payload


class ChatModel:
    def __init__(self, client: Client, route: Route) -> None:
        self.client = client
        self.route = route

    def chat_json(
        self, messages: Sequence[dict[str, str]], task: str, record: Recorder
    ) -> str:
        """Ask for a JSON object and return the raw message content."""
        body: dict[str, Any] = {
            "model": self.route.model,
            "messages": list(messages),
            "response_format": {"type": "json_object"},
        }
        if self.route.provider == "qwen":
            # Qwen 3.x thinks by default: ~10x slower and costlier for no gain here.
            body["enable_thinking"] = False
        payload = self.client.post("chat/completions", body, task, record)
        try:
            return str(payload["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as error:
            raise Retryable("invalid_response") from error


class Router:
    """Chooses a chat model by task tier: light or heavy (ADR 0002)."""

    def __init__(
        self,
        light: ChatModel,
        heavy: ChatModel,
        fallback: ChatModel | None = None,
    ) -> None:
        self.tiers = {"light": light, "heavy": heavy}
        self.fallback = fallback

    def chat_json(
        self,
        tier: str,
        messages: Sequence[dict[str, str]],
        task: str,
        record: Recorder,
    ) -> tuple[str, Route]:
        model = self.tiers[tier]
        try:
            return model.chat_json(messages, task, record), model.route
        except Retryable:
            if self.fallback is None:
                raise
            return self.fallback.chat_json(messages, task, record), self.fallback.route


class Embedder:
    def __init__(self, client: Client, model: str, dimension: int) -> None:
        self.client = client
        self.model = model
        self.dimension = dimension

    def embed(
        self, texts: Sequence[str], task: str, record: Recorder
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBEDDING_BATCH):
            batch = list(texts[start : start + EMBEDDING_BATCH])
            payload = self.client.post(
                "embeddings", {"model": self.model, "input": batch}, task, record
            )
            try:
                data = sorted(payload["data"], key=lambda item: item["index"])
                result = [list(map(float, item["embedding"])) for item in data]
            except (KeyError, TypeError, ValueError) as error:
                raise Retryable("invalid_response") from error
            if len(result) != len(batch) or any(
                len(vector) != self.dimension for vector in result
            ):
                raise Permanent("embedding_shape")
            vectors.extend(result)
        return vectors


@dataclass(frozen=True)
class Models:
    router: Router
    embedder: Embedder


def build_models(
    settings: Settings, transport: httpx.BaseTransport | None = None
) -> Models:
    clients: dict[str, Client] = {}

    def client(provider: str) -> Client:
        if provider not in clients:
            clients[provider] = Client(
                provider,
                settings.base_url(provider),
                settings.api_key(provider),
                transport=transport,
            )
        return clients[provider]

    def chat(route: Route) -> ChatModel:
        return ChatModel(client(route.provider), route)

    return Models(
        router=Router(
            chat(settings.light),
            chat(settings.heavy),
            chat(settings.fallback) if settings.fallback else None,
        ),
        embedder=Embedder(
            client("qwen"), settings.embedding_model, settings.embedding_dimension
        ),
    )
