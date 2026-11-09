import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth
from app.contracts import Credentials, Page, ProjectView, QuestionView
from app.models import InviteCode, ProjectQuestion

PASSWORD = "example-password-123"


def invitation(connection: Connection, *, expired: bool = False) -> str:
    code = secrets.token_urlsafe(24)
    with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
        delta = timedelta(days=-1 if expired else 1)
        db.add(
            InviteCode(
                code_hash=auth.digest(code), expires_at=datetime.now(UTC) + delta
            )
        )
        db.commit()
    return code


def register(client: TestClient, username: str, code: str) -> Credentials:
    response = client.post(
        "/v1/auth/register",
        json={"username": username, "password": PASSWORD, "invite_code": code},
    )
    assert response.status_code == 201, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return Credentials.model_validate(response.json())


def headers(credentials: Credentials) -> dict[str, str]:
    return {"Authorization": f"Bearer {credentials.access_token}"}


def test_invitation_and_session_lifecycle(
    client: TestClient, connection: Connection
) -> None:
    code = invitation(connection)
    original = register(client, "First_User", code)
    assert original.account.username == "first_user"
    assert original.account.timezone == "Asia/Shanghai"
    assert client.get("/v1/me", headers=headers(original)).json()["id"] == str(
        original.account.id
    )

    for invalid in (code, invitation(connection, expired=True)):
        response = client.post(
            "/v1/auth/register",
            json={
                "username": "other_user",
                "password": PASSWORD,
                "invite_code": invalid,
            },
        )
        assert response.status_code == 400
    fresh = invitation(connection)
    response = client.post(
        "/v1/auth/register",
        json={"username": "first_user", "password": PASSWORD, "invite_code": fresh},
    )
    assert response.status_code == 409
    register(client, "other_user", fresh)

    refreshed = client.post(
        "/v1/auth/refresh", json={"refresh_token": original.refresh_token}
    )
    assert refreshed.status_code == 200
    current = Credentials.model_validate(refreshed.json())
    assert current.refresh_token != original.refresh_token
    assert (
        client.post(
            "/v1/auth/refresh", json={"refresh_token": original.refresh_token}
        ).status_code
        == 401
    )
    assert client.get("/v1/me", headers=headers(original)).status_code == 401
    assert client.get("/v1/me", headers=headers(current)).status_code == 200

    for _ in range(2):
        assert (
            client.post(
                "/v1/auth/logout", json={"refresh_token": current.refresh_token}
            ).status_code
            == 204
        )
    assert client.get("/v1/me", headers=headers(current)).status_code == 401
    assert (
        client.post(
            "/v1/auth/refresh", json={"refresh_token": current.refresh_token}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/v1/auth/login", json={"username": "FIRST_USER", "password": PASSWORD}
        ).status_code
        == 200
    )


def test_login_limits_and_validation_does_not_echo_secrets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "ATTEMPT_LIMIT", 2)
    response = client.post(
        "/v1/auth/login", json={"username": "test_user", "password": "secret"}
    )
    assert response.status_code == 422
    assert "secret" not in response.text
    for _ in range(2):
        response = client.post(
            "/v1/auth/login", json={"username": "test_user", "password": PASSWORD}
        )
        assert response.status_code == 401
    response = client.post(
        "/v1/auth/login", json={"username": "test_user", "password": PASSWORD}
    )
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 60


def test_projects_and_questions_are_isolated_in_api_and_db(
    client: TestClient, connection: Connection
) -> None:
    account_a = register(client, "account_a", invitation(connection))
    account_b = register(client, "account_b", invitation(connection))
    auth_a, auth_b = headers(account_a), headers(account_b)

    def make_project(auth_headers: dict[str, str], name: str) -> UUID:
        response = client.post(
            "/v1/projects",
            json={"name": name, "goal": "建立知识库"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        return ProjectView.model_validate(response.json()).id

    a1 = make_project(auth_a, "A1")
    a2 = make_project(auth_a, "A2")
    b1 = make_project(auth_b, "B1")
    b2 = make_project(auth_b, "B2")
    response = client.post(
        f"/v1/projects/{b1}/questions",
        json={"text": "如何验证项目隔离？"},
        headers=auth_b,
    )
    assert response.status_code == 201
    question = QuestionView.model_validate(response.json())
    assert question.status == "open"

    first = Page[ProjectView].model_validate(
        client.get("/v1/projects?limit=1", headers=auth_a).json()
    )
    assert first.next_cursor is not None and len(first.items) == 1
    second = Page[ProjectView].model_validate(
        client.get(
            "/v1/projects",
            params={"limit": 1, "cursor": first.next_cursor},
            headers=auth_a,
        ).json()
    )
    assert second.next_cursor is None and len(second.items) == 1
    assert {item.id for item in first.items + second.items} == {a1, a2}
    b_page = Page[ProjectView].model_validate(
        client.get("/v1/projects", headers=auth_b).json()
    )
    assert {item.id for item in b_page.items} == {b1, b2}

    for method, path, body in (
        ("GET", f"/v1/projects/{b1}", None),
        ("PATCH", f"/v1/projects/{b1}", {"name": "intruder"}),
        ("GET", f"/v1/projects/{b1}/questions", None),
        ("POST", f"/v1/projects/{b1}/questions", {"text": "intruder"}),
        ("PATCH", f"/v1/questions/{question.id}", {"status": "resolved"}),
    ):
        response = client.request(method, path, json=body, headers=auth_a)
        assert response.status_code == 404, (method, path, response.text)
    assert client.get(f"/v1/projects/{uuid4()}", headers=auth_a).json() == {
        "detail": "project not found"
    }
    assert client.get(f"/v1/projects/{b1}", headers=auth_b).json()["name"] == "B1"
    assert (
        client.get(f"/v1/projects/{b1}/questions", headers=auth_b).json()["items"][0][
            "status"
        ]
        == "open"
    )
    assert (
        client.patch(
            f"/v1/questions/{question.id}",
            json={"status": "resolved", "text": "已验证隔离"},
            headers=auth_b,
        ).json()["status"]
        == "resolved"
    )
    assert (
        client.patch(
            f"/v1/projects/{b1}", json={"status": "archived"}, headers=auth_b
        ).json()["status"]
        == "archived"
    )
    assert (
        client.patch(
            f"/v1/projects/{b1}", json={"goal": None}, headers=auth_b
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/projects",
            json={"name": "bad", "owner_id": str(account_b.account.id)},
            headers=auth_a,
        ).status_code
        == 422
    )

    with (
        Session(bind=connection, join_transaction_mode="create_savepoint") as db,
        pytest.raises(IntegrityError),
    ):
        db.add(
            ProjectQuestion(
                owner_id=account_a.account.id, project_id=b1, text="cross-owner"
            )
        )
        db.flush()
