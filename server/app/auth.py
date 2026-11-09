import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.contracts import (
    Account,
    Credentials,
    ErrorDetail,
    ErrorResponses,
    Login,
    Refresh,
    Register,
)
from app.db import get_db
from app.models import AuthLimit, InviteCode, LoginSession, User

router = APIRouter(prefix="/v1", tags=["accounts"])
Db = Annotated[Session, Depends(get_db)]
bearer = HTTPBearer(auto_error=False)
Bearer = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
ACCESS_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(days=30)
ATTEMPT_LIMIT = 10
password_hasher = PasswordHasher()
dummy_hash = password_hasher.hash(secrets.token_urlsafe(32))


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def unauthorized(message: str = "invalid or expired session") -> HTTPException:
    return HTTPException(401, message, headers={"WWW-Authenticate": "Bearer"})


def current_owner(db: Db, auth: Bearer) -> User:
    if auth is None or len(auth.credentials) > 128:
        raise unauthorized()
    now = datetime.now(UTC)
    user = db.scalar(
        select(User)
        .join(LoginSession, LoginSession.owner_id == User.id)
        .where(
            LoginSession.access_hash == digest(auth.credentials),
            LoginSession.access_expires_at > now,
            LoginSession.expires_at > now,
            LoginSession.revoked_at.is_(None),
        )
    )
    if user is None:
        raise unauthorized()
    return user


Owner = Annotated[User, Depends(current_owner)]
AUTH_ERROR: ErrorResponses = {
    401: {
        "model": ErrorDetail,
        "description": "Missing, expired or invalid credentials.",
    }
}
RATE_ERROR: ErrorResponses = {
    429: {"model": ErrorDetail, "description": "Too many attempts. See Retry-After."}
}


def limit_attempt(db: Session, request: Request, operation: str) -> None:
    now = datetime.now(UTC)
    window = now.replace(second=0, microsecond=0)
    client = request.client.host if request.client else "unknown"
    db.execute(
        delete(AuthLimit).where(AuthLimit.window_start < window - timedelta(days=1))
    )
    attempt = (
        insert(AuthLimit)
        .values(key=f"{operation}:{client}", window_start=window, attempts=1)
        .on_conflict_do_update(
            index_elements=[AuthLimit.key],
            set_={
                "window_start": case(
                    (AuthLimit.window_start < window, window),
                    else_=AuthLimit.window_start,
                ),
                "attempts": case(
                    (AuthLimit.window_start < window, 1), else_=AuthLimit.attempts + 1
                ),
            },
            where=or_(
                AuthLimit.window_start < window, AuthLimit.attempts < ATTEMPT_LIMIT
            ),
        )
        .returning(AuthLimit.attempts)
    )
    allowed = db.scalar(attempt) is not None
    db.commit()
    if not allowed:
        raise HTTPException(
            429, "too many attempts", headers={"Retry-After": str(60 - now.second)}
        )


def issue_credentials(
    db: Session, user: User, session: LoginSession | None = None
) -> Credentials:
    now = datetime.now(UTC)
    if session is None:
        session = LoginSession(owner_id=user.id, expires_at=now + SESSION_TTL)
        db.add(session)
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    session.access_hash = digest(access)
    session.refresh_hash = digest(refresh)
    session.access_expires_at = min(now + ACCESS_TTL, session.expires_at)
    db.commit()
    return Credentials(
        account=Account.model_validate(user),
        access_token=access,
        refresh_token=refresh,
        expires_in=int((session.access_expires_at - now).total_seconds()),
    )


@router.post(
    "/auth/register",
    status_code=201,
    responses={
        400: {
            "model": ErrorDetail,
            "description": "Invalid, used or expired invitation.",
        },
        409: {"model": ErrorDetail, "description": "Username already exists."},
        **RATE_ERROR,
    },
)
def register(body: Register, request: Request, db: Db) -> Credentials:
    """Consume a single-use invitation and create an account and its first session."""
    limit_attempt(db, request, "register")
    invite = db.scalar(
        select(InviteCode)
        .where(
            InviteCode.code_hash == digest(body.invite_code.get_secret_value().strip())
        )
        .with_for_update()
    )
    if (
        invite is None
        or invite.used_by is not None
        or invite.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(400, "invalid or expired invitation")

    user = db.scalar(
        insert(User)
        .values(
            username=body.username,
            password_hash=password_hasher.hash(body.password.get_secret_value()),
        )
        .on_conflict_do_nothing(index_elements=[User.username])
        .returning(User)
    )
    if user is None:
        raise HTTPException(409, "username already exists")
    invite.used_by = user.id
    return issue_credentials(db, user)


@router.post("/auth/login", responses={**AUTH_ERROR, **RATE_ERROR})
def login(body: Login, request: Request, db: Db) -> Credentials:
    """Create a new session. Login and registration are rate-limited per client address."""
    limit_attempt(db, request, "login")
    user = db.scalar(
        select(User).where(User.username == body.username).with_for_update()
    )
    password = body.password.get_secret_value()
    try:
        password_hasher.verify(user.password_hash if user else dummy_hash, password)
    except VerifyMismatchError:
        raise unauthorized("invalid username or password") from None
    if user is None:
        raise unauthorized("invalid username or password")
    if password_hasher.check_needs_rehash(user.password_hash):
        user.password_hash = password_hasher.hash(password)
    return issue_credentials(db, user)


@router.post("/auth/refresh", responses=AUTH_ERROR)
def refresh(body: Refresh, db: Db) -> Credentials:
    """Rotate both tokens once. The session keeps its original 30-day deadline."""
    session = db.scalar(
        select(LoginSession)
        .where(
            LoginSession.refresh_hash == digest(body.refresh_token.get_secret_value()),
            LoginSession.expires_at > datetime.now(UTC),
            LoginSession.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if session is None or session.expires_at <= datetime.now(UTC):
        raise unauthorized()
    user = db.get(User, session.owner_id)
    if user is None:
        raise unauthorized()
    return issue_credentials(db, user, session)


@router.post("/auth/logout", status_code=204)
def logout(body: Refresh, db: Db) -> Response:
    """Revoke the session identified by its current refresh token. Safe to repeat."""
    db.execute(
        update(LoginSession)
        .where(
            LoginSession.refresh_hash == digest(body.refresh_token.get_secret_value()),
            LoginSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    db.commit()
    return Response(status_code=204)


@router.get("/me", responses=AUTH_ERROR)
def me(owner: Owner) -> Account:
    return Account.model_validate(owner)
