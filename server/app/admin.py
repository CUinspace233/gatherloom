import argparse
import secrets
from datetime import UTC, datetime, timedelta
from getpass import getpass
from typing import Annotated

from pydantic import Field, SecretStr, TypeAdapter, ValidationError
from sqlalchemy import select, update

from app.auth import digest, password_hasher
from app.contracts import Password, Username
from app.db import SessionLocal
from app.models import InviteCode, LoginSession, User

InviteHours = TypeAdapter(Annotated[int, Field(ge=1, le=168)])


def create_invite(hours: int) -> str:
    code = secrets.token_urlsafe(24)
    with SessionLocal.begin() as db:
        db.add(
            InviteCode(
                code_hash=digest(code),
                expires_at=datetime.now(UTC) + timedelta(hours=hours),
            )
        )
    return code


def reset_password(username: str, password: SecretStr) -> None:
    with SessionLocal.begin() as db:
        user = db.scalar(
            select(User).where(User.username == username).with_for_update()
        )
        if user is None:
            raise SystemExit("account not found")
        user.password_hash = password_hasher.hash(password.get_secret_value())
        db.execute(
            update(LoginSession)
            .where(LoginSession.owner_id == user.id, LoginSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage Gatherloom invitations and account recovery."
    )
    actions = parser.add_subparsers(dest="action", required=True)
    invite = actions.add_parser("invite", help="Issue one single-use invitation.")
    invite.add_argument("--hours", type=int, default=24)
    reset = actions.add_parser(
        "reset-password", help="Change a password and revoke all active sessions."
    )
    reset.add_argument("username")
    args = parser.parse_args()

    if args.action == "invite":
        try:
            hours = InviteHours.validate_python(args.hours)
        except ValidationError:
            parser.error("--hours must be between 1 and 168")
        print(create_invite(hours))
        return

    try:
        username = TypeAdapter(Username).validate_python(args.username)
        password = TypeAdapter(Password).validate_python(getpass("New password: "))
    except ValidationError:
        parser.error("provide a valid username and a password with 8–128 characters")
    if password.get_secret_value() != getpass("Repeat new password: "):
        parser.error("passwords do not match")
    reset_password(username, password)
    print("Password updated. Existing sessions revoked.")


if __name__ == "__main__":
    main()
