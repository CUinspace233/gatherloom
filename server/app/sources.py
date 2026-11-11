"""Citation views for anything that cites source chunks.

Answers, summaries, decision drafts and reviews all keep a list of cited
source chunks as they were. This turns them into ``CitationView`` with the
entry's title and type, and marks a citation stale when the entry has since
been edited, deleted or removed from the project.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts import CitationView
from app.entries import views
from app.models import Entry, ProjectEntry

TITLE_CHARS = 40


@dataclass(frozen=True)
class SourceRef:
    n: int
    entry_id: UUID
    revision: int
    chunk_id: UUID
    source: str
    start_offset: int
    end_offset: int
    snippet: str

    @classmethod
    def from_evidence(cls, item: Mapping[str, Any]) -> "SourceRef":
        """From an evidence item stored in a trace, payload or sources list."""
        return cls(
            n=int(item["n"]),
            entry_id=UUID(str(item["entry_id"])),
            revision=int(item["revision"]),
            chunk_id=UUID(str(item["chunk_id"])),
            source=str(item.get("source", "text")),
            start_offset=int(item.get("start_offset", 0)),
            end_offset=int(item.get("end_offset", 0)),
            snippet=str(item.get("text", item.get("snippet", ""))),
        )


def citation_views(
    db: Session, owner_id: UUID, project_id: UUID, refs: Iterable[SourceRef]
) -> list[CitationView]:
    refs = list(refs)
    cited = {ref.entry_id for ref in refs}
    if not cited:
        return []
    entries = {
        entry.id: entry
        for entry in db.scalars(
            select(Entry).where(Entry.owner_id == owner_id, Entry.id.in_(cited))
        )
    }
    in_project = set(
        db.scalars(
            select(ProjectEntry.entry_id).where(
                ProjectEntry.owner_id == owner_id,
                ProjectEntry.project_id == project_id,
                ProjectEntry.entry_id.in_(cited),
            )
        )
    )
    summaries = {view.id: view for view in views(db, owner_id, list(entries.values()))}
    result = []
    for ref in refs:
        entry = entries.get(ref.entry_id)
        if entry is None:
            continue
        summary = summaries[entry.id]
        result.append(
            CitationView(
                n=ref.n,
                entry_id=ref.entry_id,
                revision=ref.revision,
                chunk_id=ref.chunk_id,
                title=summary.title
                or summary.text[:TITLE_CHARS]
                or summary.source_url
                or "",
                type=summary.type,
                created_at=entry.created_at,
                source=ref.source,
                snippet=ref.snippet,
                start_offset=ref.start_offset,
                end_offset=ref.end_offset,
                stale=entry.deleted_at is not None
                or entry.current_revision != ref.revision
                or entry.id not in in_project,
            )
        )
    return result


def any_stale(views: Iterable[CitationView]) -> bool:
    return any(view.stale for view in views)
