"""GET /v1/search: hybrid search across the current account's entries."""

import base64
import binascii
from datetime import datetime
from functools import cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app import search
from app.auth import AUTH_ERROR, Db, Owner
from app.contracts import EntryType, ErrorDetail, SearchHit, SearchPage
from app.entries import views
from app.models import Entry, Run
from app.projects import NOT_FOUND, get_project
from app.providers import Call, Embedder, build_models
from app.settings import load_settings

router = APIRouter(prefix="/v1", tags=["search"], responses=AUTH_ERROR)
MAX_RESULTS = search.CANDIDATES


@cache
def configured_embedder() -> Embedder | None:
    settings = load_settings()
    if not settings.bailian_api_key:
        return None
    return build_models(settings).embedder


def get_embedder() -> Embedder | None:
    """Overridden in tests. None means keyword search only."""
    return configured_embedder()


def encode_offset(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode()


def decode_offset(cursor: str) -> int:
    try:
        kind, _, value = base64.urlsafe_b64decode(cursor).decode().partition(":")
        offset = int(value)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(400, "invalid cursor") from None
    if kind != "o" or not 0 <= offset < MAX_RESULTS:
        raise HTTPException(400, "invalid cursor")
    return offset


@router.get(
    "/search",
    responses={
        **NOT_FOUND,
        400: {"model": ErrorDetail, "description": "Invalid cursor or time range."},
    },
)
def search_entries(
    owner: Owner,
    db: Db,
    embedder: Annotated[Embedder | None, Depends(get_embedder)],
    q: Annotated[str, Query(min_length=1, max_length=200)],
    project_id: UUID | None = None,
    type: Annotated[list[EntryType], Query()] = [],  # noqa: B006 - FastAPI query list
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
    cursor: str | None = None,
) -> SearchPage:
    """Search entries by keywords and meaning, newest revision only.

    With project_id, only that project's entries are searched. Results are
    entries ranked by relevance, each with the best matching source excerpt.
    """
    if not q.strip():
        raise HTTPException(422, "query is empty")
    if since is not None and until is not None and since >= until:
        raise HTTPException(400, "since must be before until")
    if project_id is not None:
        get_project(db, owner.id, project_id)
    offset = 0 if cursor is None else decode_offset(cursor)

    def record(call: Call) -> None:
        cost, price = call.cost()
        db.add(
            Run(
                owner_id=owner.id,
                task=call.task,
                provider=call.provider,
                model=call.model,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cost_estimate=cost,
                currency=price.currency if price else None,
                price_date=price.checked if price else None,
                latency_ms=call.latency_ms,
                error_code=call.error_code,
            )
        )

    result = search.retrieve(
        db,
        owner.id,
        q.strip(),
        filters=search.Filters(project_id, type, since, until),
        embedder=embedder,
        record=record,
        max_distance=search.SEARCH_MAX_DISTANCE,
    )
    page = result.hits[offset : offset + limit]
    entries = {
        entry.id: entry
        for entry in db.scalars(
            select(Entry).where(
                Entry.owner_id == owner.id,
                Entry.id.in_([hit.entry_id for hit in page]),
            )
        )
    }
    entry_views = {
        view.id: view
        for view in views(db, owner.id, [entries[hit.entry_id] for hit in page])
    }
    items = []
    for hit in page:
        text, highlights = search.snippet(hit.display_text, result.terms)
        items.append(
            SearchHit(
                entry=entry_views[hit.entry_id],
                score=round(hit.score, 6),
                chunk_id=hit.chunk_id,
                source=hit.source,
                snippet=text,
                highlights=highlights,
                matched_by=sorted(hit.matched_by),  # type: ignore[arg-type]
            )
        )
    end = offset + limit
    db.commit()
    return SearchPage(
        items=items,
        next_cursor=encode_offset(end) if end < len(result.hits) else None,
        degraded=result.degraded,
    )
