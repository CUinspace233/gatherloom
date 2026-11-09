from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
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
EntryText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=20000, pattern=r"^[^\x00]*$"
    ),
]
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


class EntryCreate(Input):
    client_submission_id: UUID
    type: Literal["thought"] = "thought"
    text: EntryText
    note: ProjectGoal = ""
    project_id: UUID | None = None


class EntryView(BaseModel):
    id: UUID
    client_submission_id: UUID
    type: str
    text: str
    note: str
    project_ids: list[UUID]
    possible_duplicate: bool
    created_at: datetime


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None
