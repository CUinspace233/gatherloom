"""Split an entry revision into retrieval chunks.

Short material is indexed whole. Longer text is split on paragraph and then
sentence boundaries into chunks of about TARGET characters, never more than
MAX, and each chunk repeats the previous chunk's last sentence when it is short.
Every chunk keeps its offsets into the source text so citations can point back
to the original.

Each chunk's index text is prefixed with a context header (ADR 0009 R1): the
entry title and the start of the personal note. Display text stays original.
"""

import re
from dataclasses import dataclass
from typing import Any

CONTEXT_HEADER_VERSION = 1
WHOLE_LIMIT = 600
TARGET = 500
MAX = 800
OVERLAP_MAX = 200
NOTE_EXCERPT = 120
_SENTENCE_END = re.compile(r"[。！？!?；;]+[”’\"')）]*|\n+|(?<=[.])\s+")


@dataclass(frozen=True)
class Material:
    type: str
    text: str
    note: str
    source_url: str | None = None
    client_extract: dict[str, Any] | None = None
    client_ocr: dict[str, Any] | None = None

    @property
    def title(self) -> str:
        return str((self.client_extract or {}).get("title") or "").strip()


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    source: str
    start: int
    end: int
    display_text: str
    index_text: str


def context_header(material: Material) -> str:
    parts = [material.title] if material.title else []
    note = " ".join(material.note.split())
    if note:
        parts.append(note[:NOTE_EXCERPT])
    return " · ".join(parts)


def sources(material: Material) -> list[tuple[str, str]]:
    """The bodies of text to chunk, labelled by where they came from."""
    bodies = [("text", material.text)]
    if material.type == "url":
        bodies.append(("extract", str((material.client_extract or {}).get("text", ""))))
    elif material.type == "image":
        bodies.append(("ocr", str((material.client_ocr or {}).get("text", ""))))
    return [(source, body) for source, body in bodies if body.strip()]


def chunk_material(material: Material) -> list[ChunkDraft]:
    header = context_header(material)
    drafts: list[ChunkDraft] = []
    for source, body in sources(material):
        for start, end in spans(body):
            text = body[start:end]
            drafts.append(
                ChunkDraft(
                    ordinal=len(drafts),
                    source=source,
                    start=start,
                    end=end,
                    display_text=text,
                    index_text=f"{header}\n{text}" if header else text,
                )
            )
    if not drafts:
        # Keep links and annotated images findable by what little they have.
        meta = "\n".join(
            part
            for part in (material.title, material.note, material.source_url)
            if part
        )
        if meta.strip():
            drafts.append(ChunkDraft(0, "meta", 0, 0, meta, meta))
    return drafts


def spans(body: str) -> list[tuple[int, int]]:
    """Character ranges of body, each a chunk."""
    units = sentence_units(body)
    if not units:
        return []
    if units[-1][1] - units[0][0] <= WHOLE_LIMIT:
        return [(units[0][0], units[-1][1])]
    chunks: list[list[tuple[int, int, bool]]] = []
    current: list[tuple[int, int, bool]] = []
    for unit in units:
        start, end, new_paragraph = unit
        size = end - start
        if current:
            length = current[-1][1] - current[0][0]
            if (
                length + size > MAX
                or length >= TARGET
                or (new_paragraph and length + size > TARGET)
            ):
                chunks.append(current)
                last = current[-1]
                overlap = last[1] - last[0]
                keep = len(current) > 1 and overlap <= OVERLAP_MAX
                current = [last] if keep and overlap + size <= MAX else []
        current.append(unit)
    chunks.append(current)
    return [(chunk[0][0], chunk[-1][1]) for chunk in chunks]


def sentence_units(body: str) -> list[tuple[int, int, bool]]:
    """Sentences as (start, end, starts_paragraph), whitespace trimmed, ≤ MAX."""
    units: list[tuple[int, int, bool]] = []
    position = 0
    new_paragraph = True
    for match in [*_SENTENCE_END.finditer(body), None]:
        end = len(body) if match is None else match.end()
        start, stop = trim(body, position, end)
        if start < stop:
            for piece in range(start, stop, MAX):
                units.append((piece, min(piece + MAX, stop), new_paragraph))
                new_paragraph = False
        if match is not None and "\n" in match.group():
            new_paragraph = True
        position = end
        if match is None or position >= len(body):
            break
    return units


def trim(body: str, start: int, end: int) -> tuple[int, int]:
    while start < end and body[start].isspace():
        start += 1
    while end > start and body[end - 1].isspace():
        end -= 1
    return start, end
