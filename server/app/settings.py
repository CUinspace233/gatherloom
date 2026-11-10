"""Model configuration from GATHERLOOM_* environment variables (ADR 0001, 0002).

Only the worker needs model keys; the API process never reads them.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from app.models import EMBEDDING_DIMENSION

PROVIDERS = ("qwen", "deepseek")


@dataclass(frozen=True)
class Route:
    provider: str
    model: str


@dataclass(frozen=True)
class Settings:
    bailian_api_key: str
    bailian_base_url: str
    deepseek_api_key: str
    deepseek_base_url: str
    embedding_model: str
    embedding_dimension: int
    light: Route
    heavy: Route
    fallback: Route | None

    def api_key(self, provider: str) -> str:
        return self.bailian_api_key if provider == "qwen" else self.deepseek_api_key

    def base_url(self, provider: str) -> str:
        return self.bailian_base_url if provider == "qwen" else self.deepseek_base_url

    def problems(self) -> list[str]:
        """Configuration errors that stop the worker from starting."""
        found = []
        routes = [self.light, self.heavy, *([self.fallback] if self.fallback else [])]
        for route in routes:
            if route.provider not in PROVIDERS:
                found.append(f"unknown provider {route.provider!r}")
            elif not route.model:
                found.append(f"no model set for provider {route.provider!r}")
        needed = {"qwen"} | {route.provider for route in routes}
        if "qwen" in needed and not self.bailian_api_key:
            found.append("GATHERLOOM_BAILIAN_API_KEY is not set")
        if "deepseek" in needed and not self.deepseek_api_key:
            found.append("GATHERLOOM_DEEPSEEK_API_KEY is not set")
        if self.embedding_dimension != EMBEDDING_DIMENSION:
            found.append(
                f"embedding dimension {self.embedding_dimension} does not match "
                f"the index ({EMBEDDING_DIMENSION}); a new index_version is required"
            )
        return found


def route(env: Mapping[str, str], tier: str, provider: str, model: str) -> Route:
    prefix = f"GATHERLOOM_LLM_{tier.upper()}"
    return Route(
        env.get(f"{prefix}_PROVIDER") or provider, env.get(f"{prefix}_MODEL") or model
    )


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    fallback_provider = env.get("GATHERLOOM_LLM_FALLBACK_PROVIDER", "")
    return Settings(
        bailian_api_key=env.get("GATHERLOOM_BAILIAN_API_KEY", ""),
        bailian_base_url=env.get(
            "GATHERLOOM_BAILIAN_BASE_URL",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        ).rstrip("/"),
        deepseek_api_key=env.get("GATHERLOOM_DEEPSEEK_API_KEY", ""),
        deepseek_base_url=env.get(
            "GATHERLOOM_DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ).rstrip("/"),
        embedding_model=env.get("GATHERLOOM_EMBEDDING_MODEL")
        or "qwen3.7-text-embedding-flash",
        embedding_dimension=int(
            env.get("GATHERLOOM_EMBEDDING_DIMENSION") or EMBEDDING_DIMENSION
        ),
        light=route(env, "light", "qwen", "qwen3.7-flash-2026-07-15"),
        heavy=route(env, "heavy", "deepseek", "deepseek-v4-pro"),
        fallback=(
            Route(fallback_provider, env.get("GATHERLOOM_LLM_FALLBACK_MODEL", ""))
            if fallback_provider
            else None
        ),
    )
