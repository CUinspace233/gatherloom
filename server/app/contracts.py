from datetime import datetime
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    model_validator,
)

Username = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]{2,31}$",
    ),
]
Password = Annotated[SecretStr, Field(min_length=8, max_length=128)]
Token = Annotated[SecretStr, Field(min_length=1, max_length=128)]
ProjectName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=120, pattern=r"^[^\x00]*$"
    ),
]
ProjectGoal = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=4000, pattern=r"^[^\x00]*$"),
]
QuestionText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=2000, pattern=r"^[^\x00]*$"
    ),
]
ProjectStatus = Literal["active", "archived"]
QuestionStatus = Literal["open", "resolved"]
ErrorResponses = dict[int | str, dict[str, Any]]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: Username
    password: Password


class Register(Login):
    invite_code: Token


class Refresh(Input):
    refresh_token: Token


class Account(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    timezone: str


class Credentials(BaseModel):
    account: Account
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class ErrorDetail(BaseModel):
    detail: str


class Patch(Input):
    @model_validator(mode="after")
    def require_changes(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError(
                "provide at least one field and omit fields that are not changing"
            )
        return self


class ProjectCreate(Input):
    name: ProjectName
    goal: ProjectGoal = ""


class ProjectUpdate(Patch):
    name: ProjectName | None = None
    goal: ProjectGoal | None = None
    status: ProjectStatus | None = None


class QuestionCreate(Input):
    text: QuestionText


class QuestionUpdate(Patch):
    text: QuestionText | None = None
    status: QuestionStatus | None = None


class ProjectView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    goal: str
    status: ProjectStatus
    created_at: datetime


class QuestionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    text: str
    status: QuestionStatus
    created_at: datetime


OptionalEntryText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=20000, pattern=r"^[^\x00]*$"),
]
LongText = Annotated[str, StringConstraints(max_length=100000, pattern=r"^[^\x00]*$")]
EntryType = Literal["thought", "url", "image"]
ProcessingStatus = Literal[
    "queued", "running", "searchable", "succeeded", "partial", "failed", "cancelled"
]


def check_web_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme.lower() not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("must be an http or https URL without credentials")
    return value


WebURL = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2048),
    AfterValidator(check_web_url),
]


class OCRLine(Input):
    text: Annotated[str, StringConstraints(max_length=2000, pattern=r"^[^\x00]*$")]
    box: Annotated[
        list[Annotated[float, Field(ge=0, le=1)]], Field(min_length=4, max_length=4)
    ] = Field(description="Normalized x, y, width, height with a top-left origin.")


class ClientOCR(Input):
    text: LongText
    lines: Annotated[list[OCRLine], Field(max_length=2000)] = []
    engine: Annotated[str, StringConstraints(max_length=64)] = ""


class ClientExtract(Input):
    title: Annotated[str, StringConstraints(max_length=500, pattern=r"^[^\x00]*$")] = ""
    text: LongText = ""
    canonical_url: WebURL | None = None
    captured_at: datetime | None = None


class EntryCreate(Input):
    client_submission_id: UUID
    type: EntryType = "thought"
    text: OptionalEntryText = ""
    note: ProjectGoal = ""
    project_id: UUID | None = None
    url: WebURL | None = None
    attachment_id: UUID | None = None
    client_ocr: ClientOCR | None = None
    client_extract: ClientExtract | None = None

    @model_validator(mode="after")
    def match_type(self) -> Self:
        if self.type == "thought" and not self.text:
            raise ValueError("thought entries need text")
        if (self.url is not None) != (self.type == "url"):
            raise ValueError("url is required for url entries and only allowed there")
        if (self.attachment_id is not None) != (self.type == "image"):
            raise ValueError(
                "attachment_id is required for image entries and only allowed there"
            )
        if self.client_ocr is not None and self.type != "image":
            raise ValueError("client_ocr is only allowed for image entries")
        if self.client_extract is not None and self.type != "url":
            raise ValueError("client_extract is only allowed for url entries")
        return self


class EntryUpdate(Input):
    base_revision: Annotated[int, Field(ge=1)]
    text: OptionalEntryText | None = None
    note: ProjectGoal | None = None

    @model_validator(mode="after")
    def require_changes(self) -> Self:
        if self.text is None and self.note is None:
            raise ValueError("provide text or note")
        return self


class AttachmentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_submission_id: UUID
    content_type: str
    size: int
    sha256: str


class EntryView(BaseModel):
    id: UUID
    client_submission_id: UUID
    type: EntryType
    revision: int
    text: str
    note: str
    source_url: str | None
    title: str | None = Field(
        description="Page title captured by the client, else the knowledge card title."
    )
    attachment: AttachmentView | None
    project_ids: list[UUID]
    possible_duplicate: bool
    processing: ProcessingStatus = Field(
        description="State of the current revision's processing; see ProcessingView."
    )
    created_at: datetime
    updated_at: datetime


class CardView(BaseModel):
    """AI-generated knowledge card. Navigation aid only, never cited as evidence."""

    title: str
    summary: str
    key_points: list[str]
    keywords: list[str]
    model: str
    created_at: datetime


class EntryDetail(EntryView):
    ocr_text: str | None
    extract_text: str | None
    card: CardView | None = Field(
        description="Knowledge card for the current revision, once generated."
    )


class ProcessingView(BaseModel):
    status: ProcessingStatus = Field(
        description="queued/running: saved and being processed; searchable: "
        "indexed, enrichment still running; succeeded: done; partial: "
        "searchable but enrichment failed; failed: not searchable yet."
    )
    searchable: bool
    stage: str | None = Field(description="Last step the worker reached.")
    failed_step: Literal["index", "card"] | None
    error_code: str | None
    attempts: int
    can_retry: bool
    updated_at: datetime


class OCRLineView(BaseModel):
    text: str
    box: list[float] = Field(description="Normalized x, y, width, height, top-left.")
    start: int | None = Field(description="Offset of the line in ocr_text, if known.")
    end: int | None


class RevisionDetail(BaseModel):
    """One revision's full text, for locating citations in the original."""

    revision: int
    current_revision: int
    type: EntryType
    text: str
    note: str
    title: str | None
    extract_text: str | None
    ocr_text: str | None
    ocr_lines: list[OCRLineView]
    attachment_id: UUID | None
    created_at: datetime


class RevisionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    revision: int
    text: str
    note: str
    created_at: datetime


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None


class SearchHit(BaseModel):
    entry: EntryView
    score: float
    chunk_id: UUID = Field(description="Source chunk that best represents the entry.")
    source: str = Field(description="text, extract, ocr or meta.")
    snippet: str
    highlights: list[tuple[int, int]] = Field(
        description="Character ranges in snippet that match the query."
    )
    matched_by: list[Literal["keyword", "semantic", "card"]]


class SearchPage(BaseModel):
    items: list[SearchHit]
    next_cursor: str | None
    degraded: bool = Field(
        description="True when semantic search was unavailable; keyword results only."
    )


ConversationTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=120, pattern=r"^[^\x00]*$")
]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class ConversationCreate(Input):
    title: ConversationTitle = ""


class ConversationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class MessageCreate(Input):
    client_message_id: UUID
    text: QuestionText


class RunView(BaseModel):
    id: UUID
    status: JobStatus
    stage: str | None
    error_code: str | None
    attempts: int
    can_retry: bool
    answer_message_id: UUID | None
    updated_at: datetime


class CitationView(BaseModel):
    n: int
    entry_id: UUID
    revision: int
    chunk_id: UUID
    title: str
    type: EntryType
    created_at: datetime
    source: str = Field(
        description="Which text the offsets refer to: text, extract, ocr or meta."
    )
    snippet: str = Field(description="The cited source text as it was when answered.")
    start_offset: int
    end_offset: int
    stale: bool = Field(
        description="The entry was edited, deleted or removed from the project since."
    )


class MessageView(BaseModel):
    id: UUID
    role: Literal["user", "assistant"]
    content: str = Field(description="Answers mark citations as [n].")
    insufficient: bool
    reply_to: UUID | None
    run: RunView | None = Field(description="Answer job for a question.")
    citations: list[CitationView]
    created_at: datetime


class AnswerAccepted(BaseModel):
    message: MessageView
    run_id: UUID
