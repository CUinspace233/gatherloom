"""Optional real HTTP checks against the committed local Postman simulation.

Run instructions: postman/README.md. No provider credentials are needed.
"""

import os

import httpx
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_web import BODY, link
from test_worker import Clock, account, claim

from app import jobs, web
from app.ingestion import Pipeline
from app.models import Chunk

BASE_URL = os.environ.get("GATHERLOOM_FIRECRAWL_TEST_URL", "")
pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="Start local Postman simulation and set GATHERLOOM_FIRECRAWL_TEST_URL",
)


@pytest.mark.parametrize(
    ("scenario", "code", "retryable"),
    [
        ("status-401", "web_auth", False),
        ("status-402", "web_quota", False),
        ("status-403", "web_forbidden", False),
        ("status-429", "web_rate_limit", True),
        ("status-503", "web_provider_error", True),
        ("html-error", "web_provider_error", True),
        ("empty", "web_empty", False),
        ("long", "web_too_long", False),
        ("page-403", "web_page_unavailable", False),
        ("invalid-json", "web_invalid_response", False),
        ("timeout", "web_timeout", True),
    ],
)
def test_real_http_failure(scenario: str, code: str, retryable: bool) -> None:
    with httpx.Client(
        base_url=BASE_URL, timeout=0.05 if scenario == "timeout" else 5
    ) as client:
        with pytest.raises(jobs.JobError) as caught:
            web.Firecrawl("test-secret", client=client).scrape(
                f"https://example.com/{scenario}"
            )
        assert (caught.value.code, caught.value.retryable) == (code, retryable)


def test_real_http_success_flows_into_index(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    saved = link(committed, account(committed))
    with httpx.Client(base_url=BASE_URL, timeout=5) as client:
        current = claim(committed, "web", jobs.utcnow())
        assert (
            Pipeline(
                committed,
                saver,
                "web",
                clock=Clock(),
                extractor=web.Firecrawl("test-secret", client=client),
            ).run(current)
            == "succeeded"
        )
    with committed() as db:
        chunks = db.scalars(select(Chunk).where(Chunk.entry_id == saved)).all()
        assert chunks and all(chunk.source == "extract" for chunk in chunks)
        assert "@Observable" in chunks[0].display_text
        for chunk in chunks:
            assert BODY[chunk.start_offset : chunk.end_offset] == chunk.display_text
