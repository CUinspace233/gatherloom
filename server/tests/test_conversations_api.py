"""Conversations API: idempotent questions, runs, citations, isolation [T2]."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Connection, insert, select, update
from test_accounts_projects import headers, invitation, register

from app.models import Citation, Entry, Job, Message


def setup_account(
    client: TestClient, connection: Connection, name: str
) -> dict[str, Any]:
    credentials = register(client, name, invitation(connection))
    auth = headers(credentials)
    project = client.post("/v1/projects", json={"name": "项目"}, headers=auth).json()
    return {"id": credentials.account.id, "headers": auth, "project": project["id"]}


def test_questions_are_idempotent_and_answers_carry_citations(
    client: TestClient, connection: Connection
) -> None:
    a = setup_account(client, connection, "chat_a")
    b = setup_account(client, connection, "chat_b")
    created = client.post(
        f"/v1/projects/{a['project']}/conversations", json={}, headers=a["headers"]
    )
    assert created.status_code == 201, created.text
    conversation = created.json()
    assert conversation["title"] == ""
    path = f"/v1/conversations/{conversation['id']}/messages"
    body = {"client_message_id": str(uuid4()), "text": "SwiftUI 状态管理用什么？"}

    accepted = client.post(path, json=body, headers=a["headers"])
    assert accepted.status_code == 202, accepted.text
    first = accepted.json()
    assert first["message"]["role"] == "user"
    assert first["message"]["run"]["status"] == "queued"
    replay = client.post(
        path, json={**body, "text": "改过的文字"}, headers=a["headers"]
    )
    assert replay.status_code == 202
    assert replay.json()["run_id"] == first["run_id"]
    assert replay.json()["message"]["content"] == body["text"]
    assert (
        connection.scalar(select(Job.kind).where(Job.id == UUID(first["run_id"])))
        == "project_answer"
    )

    listed = client.get(
        f"/v1/projects/{a['project']}/conversations", headers=a["headers"]
    ).json()
    assert listed["items"][0]["title"] == "SwiftUI 状态管理用什么？"

    # Simulate the worker's published answer citing an entry.
    entry = client.post(
        "/v1/entries",
        json={
            "client_submission_id": str(uuid4()),
            "text": "改用 @Observable 宏",
            "project_id": a["project"],
        },
        headers=a["headers"],
    ).json()
    question_id = UUID(first["message"]["id"])
    answer_id = uuid4()
    connection.execute(
        insert(Message).values(
            id=answer_id,
            owner_id=a["id"],
            conversation_id=UUID(conversation["id"]),
            role="assistant",
            content="用 @Observable [1]。",
            reply_to=question_id,
        )
    )
    connection.execute(
        insert(Citation).values(
            owner_id=a["id"],
            message_id=answer_id,
            n=1,
            entry_id=UUID(entry["id"]),
            revision=1,
            chunk_id=uuid4(),
            start_offset=0,
            end_offset=13,
            snippet="改用 @Observable 宏",
        )
    )
    connection.execute(
        update(Job)
        .where(Job.id == UUID(first["run_id"]))
        .values(status="succeeded", stage="done")
    )
    run = client.get(f"/v1/runs/{first['run_id']}", headers=a["headers"]).json()
    assert (run["status"], run["answer_message_id"]) == ("succeeded", str(answer_id))

    messages = client.get(path, headers=a["headers"]).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    [citation] = messages[1]["citations"]
    assert citation["n"] == 1 and citation["stale"] is False
    assert citation["title"] == "改用 @Observable 宏"
    assert citation["type"] == "thought"
    assert messages[1]["run"] is None

    # Editing or deleting the cited entry marks the citation stale.
    client.patch(
        f"/v1/entries/{entry['id']}",
        json={"base_revision": 1, "text": "第二版"},
        headers=a["headers"],
    )
    assert client.get(path, headers=a["headers"]).json()[1]["citations"][0]["stale"]
    connection.execute(
        update(Entry)
        .where(Entry.id == UUID(entry["id"]))
        .values(deleted_at=datetime.now(UTC))
    )
    assert client.get(path, headers=a["headers"]).json()[1]["citations"][0]["stale"]

    # [T2] Another account sees none of it.
    for method, url in [
        ("GET", path),
        ("POST", path),
        ("GET", f"/v1/runs/{first['run_id']}"),
        ("POST", f"/v1/runs/{first['run_id']}/retry"),
        ("GET", f"/v1/projects/{a['project']}/conversations"),
        ("POST", f"/v1/projects/{a['project']}/conversations"),
    ]:
        response = client.request(
            method,
            url,
            json=body if url == path else {},
            headers=b["headers"],
        )
        assert response.status_code == 404, (method, url, response.text)

    # A client message id is scoped to its conversation.
    other = client.post(
        f"/v1/projects/{a['project']}/conversations", json={}, headers=a["headers"]
    ).json()
    clash = client.post(
        f"/v1/conversations/{other['id']}/messages", json=body, headers=a["headers"]
    )
    assert clash.status_code == 409


def test_failed_runs_can_be_retried(client: TestClient, connection: Connection) -> None:
    a = setup_account(client, connection, "chat_c")
    conversation = client.post(
        f"/v1/projects/{a['project']}/conversations",
        json={"title": "测试"},
        headers=a["headers"],
    ).json()
    accepted = client.post(
        f"/v1/conversations/{conversation['id']}/messages",
        json={"client_message_id": str(uuid4()), "text": "问题"},
        headers=a["headers"],
    ).json()
    run_path = f"/v1/runs/{accepted['run_id']}"
    assert (
        client.post(f"{run_path}/retry", headers=a["headers"]).json()["status"]
        == "queued"
    )
    connection.execute(
        update(Job)
        .where(Job.id == UUID(accepted["run_id"]))
        .values(status="failed", error_code="invalid_output", attempts=1)
    )
    failed = client.get(run_path, headers=a["headers"]).json()
    assert failed["can_retry"] and failed["error_code"] == "invalid_output"
    retried = client.post(f"{run_path}/retry", headers=a["headers"]).json()
    assert (retried["status"], retried["attempts"], retried["error_code"]) == (
        "queued",
        0,
        None,
    )
    connection.execute(
        update(Job).where(Job.id == UUID(accepted["run_id"])).values(status="succeeded")
    )
    assert client.post(f"{run_path}/retry", headers=a["headers"]).status_code == 409
    assert client.get(f"/v1/runs/{uuid4()}", headers=a["headers"]).status_code == 404
