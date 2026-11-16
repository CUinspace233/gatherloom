"""Firecrawl page snapshots and bounded, restart-safe backfill scheduling.

Only the worker sends URLs to Firecrawl. Network calls never hold database
locks; the ingestion graph fences the result before publishing it.
"""

from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app import jobs
from app.contracts import check_web_url
from app.models import Entry, EntryRevision, Job

MAX_TEXT = 100_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
BACKFILL_BATCH = 50
BACKFILL_MARKER = "web_backfill"


def has_body(extract: dict[str, Any] | None) -> bool:
    return bool(str((extract or {}).get("text") or "").strip())


def effective_extract(
    client: dict[str, Any] | None, server: dict[str, Any] | None
) -> dict[str, Any]:
    """Never normalize a stored body here: citations use its exact offsets."""
    if has_body(client):
        return client or {}
    if has_body(server):
        return {
            **(server or {}),
            "title": (server or {}).get("title") or (client or {}).get("title", ""),
        }
    return client or server or {}


def response_error(status: int) -> jobs.JobError:
    if status == 429:
        return jobs.Retryable("web_rate_limit")
    if status >= 500 or status == 408:
        return jobs.Retryable("web_provider_error")
    return jobs.Permanent(
        {401: "web_auth", 402: "web_quota", 403: "web_forbidden"}.get(
            status, "web_request_rejected"
        )
    )


class Firecrawl:
    def __init__(self, api_key: str, *, client: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self.client = client or httpx.Client(
            base_url="https://api.firecrawl.dev/v2/",
            timeout=75.0,
            follow_redirects=False,
        )

    def scrape(self, url: str) -> dict[str, Any]:
        try:
            check_web_url(url)
        except ValueError:
            raise jobs.Permanent("web_invalid_url") from None
        try:
            with self.client.stream(
                "POST",
                "scrape",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "url": url,
                    "formats": ["markdown"],
                    "onlyMainContent": True,
                    "storeInCache": False,
                    "timeout": 60_000,
                },
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise response_error(response.status_code)
                content = bytearray()
                for piece in response.iter_bytes():
                    content.extend(piece)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise jobs.Permanent("web_response_too_large")
                # Parse only after enforcing the size limit. Provider error bodies
                # and exception messages must never be copied into logs.
                payload = httpx.Response(200, content=bytes(content)).json()
        except httpx.TimeoutException:
            raise jobs.Retryable("web_timeout") from None
        except httpx.HTTPError:
            raise jobs.Retryable("web_network") from None
        except (ValueError, UnicodeError):
            raise jobs.Permanent("web_invalid_response") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise jobs.Permanent("web_invalid_response")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
            raise jobs.Permanent("web_invalid_response")
        metadata = data["metadata"]
        status = metadata.get("statusCode")
        if type(status) is not int:
            raise jobs.Permanent("web_invalid_response")
        if not (200 <= status < 300 or status == 304):
            if status == 429 or status >= 500 or status == 408:
                raise jobs.Retryable("web_page_unavailable")
            raise jobs.Permanent("web_page_unavailable")
        text = data.get("markdown")
        if not isinstance(text, str) or not text.strip():
            raise jobs.Permanent("web_empty")
        if len(text) > MAX_TEXT:
            raise jobs.Permanent("web_too_long")
        if "\x00" in text:
            raise jobs.Permanent("web_invalid_response")
        title = metadata.get("title")
        return {
            "title": title[:500].replace("\x00", "") if isinstance(title, str) else "",
            "text": text,
            "source_url": url,
            "provider": "firecrawl",
            "captured_at": datetime.now(UTC).isoformat(),
        }


def enqueue_missing(db: Session, now: datetime) -> int:
    """Queue at most 50 old links once; caller commits, and enables with a key.

    Lock entries first, matching API edits/retries. Active jobs are left alone:
    even a job checkpointed before the new extract node gets picked up once
    it finishes. The marker survives failures so scans never reset retries.
    """
    empty_client = (
        func.btrim(
            func.coalesce(EntryRevision.client_extract["text"].astext, ""),
            " \t\r\n\v\f\u3000",
        )
        == ""
    )
    empty_server = (
        func.btrim(
            func.coalesce(EntryRevision.server_extract["text"].astext, ""),
            " \t\r\n\v\f\u3000",
        )
        == ""
    )
    entries = db.scalars(
        select(Entry)
        .join(
            EntryRevision,
            and_(
                EntryRevision.owner_id == Entry.owner_id,
                EntryRevision.entry_id == Entry.id,
                EntryRevision.revision == Entry.current_revision,
            ),
        )
        .join(
            Job,
            and_(
                Job.owner_id == Entry.owner_id,
                Job.entry_id == Entry.id,
                Job.revision == Entry.current_revision,
                Job.kind == jobs.INGESTION,
            ),
        )
        .where(
            Entry.type == "url",
            Entry.deleted_at.is_(None),
            empty_client,
            empty_server,
            Job.status.in_(("succeeded", "failed")),
            func.coalesce(Job.payload[BACKFILL_MARKER].as_integer(), 0) != 1,
        )
        .order_by(Entry.created_at, Entry.id)
        .limit(BACKFILL_BATCH)
        .with_for_update(of=Entry, skip_locked=True)
    ).all()
    for entry in entries:
        job = db.scalars(
            select(Job)
            .where(
                Job.owner_id == entry.owner_id,
                Job.entry_id == entry.id,
                Job.revision == entry.current_revision,
                Job.kind == jobs.INGESTION,
            )
            .with_for_update()
        ).one()
        job.payload = {**(job.payload or {}), BACKFILL_MARKER: 1}
        job.generation += 1
        job.status = "queued"
        job.stage = None
        job.attempts = 0
        job.run_after = now
        job.finished_at = None
        job.error_code = None
        job.error_step = None
        job.updated_at = now
    return len(entries)
