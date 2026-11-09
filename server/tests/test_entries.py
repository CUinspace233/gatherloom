from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Connection, func, select
from test_accounts_projects import headers, invitation, register

from app.models import Entry, EntryRevision, Job


def test_text_entry_replays_and_records_revision_and_job_once(
    client: TestClient, connection: Connection
) -> None:
    owner = register(client, "capture_a", invitation(connection))
    other = register(client, "capture_b", invitation(connection))
    owner_headers = headers(owner)
    other_headers = headers(other)
    request = {
        "client_submission_id": str(uuid4()),
        "text": "一条离线文字",
        "note": "第一版",
    }

    created = client.post("/v1/entries", json=request, headers=owner_headers)
    assert created.status_code == 201, created.text
    saved = created.json()
    replay = client.post(
        "/v1/entries", json={**request, "text": "保留第一次提交"}, headers=owner_headers
    )
    assert replay.status_code == 200
    assert replay.json() == saved
    assert saved["possible_duplicate"] is False

    for model in (Entry, EntryRevision, Job):
        assert (
            connection.scalar(
                select(func.count())
                .select_from(model)
                .where(model.owner_id == owner.account.id)
            )
            == 1
        )
    job = connection.execute(
        select(Job.entry_id, Job.revision, Job.kind, Job.status).where(
            Job.owner_id == owner.account.id
        )
    ).one()
    assert job == (UUID(saved["id"]), 1, "entry_ingestion", "queued")

    duplicate = client.post(
        "/v1/entries",
        json={**request, "client_submission_id": str(uuid4())},
        headers=owner_headers,
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["possible_duplicate"] is True
    assert duplicate.json()["id"] != saved["id"]

    independent = client.post("/v1/entries", json=request, headers=other_headers)
    assert independent.status_code == 201
    assert independent.json()["id"] != saved["id"]
    assert independent.json()["possible_duplicate"] is False
    detail_path = f"/v1/entries/{saved['id']}"
    assert client.get(detail_path, headers=owner_headers).json() == saved
    assert client.get(detail_path, headers=other_headers).status_code == 404

    first = client.get("/v1/entries?inbox=true&limit=1", headers=owner_headers).json()
    second = client.get(
        "/v1/entries",
        params={"inbox": "true", "limit": 1, "cursor": first["next_cursor"]},
        headers=owner_headers,
    ).json()
    assert {item["id"] for item in first["items"] + second["items"]} == {
        saved["id"],
        duplicate.json()["id"],
    }
    assert second["next_cursor"] is None


def test_text_entry_routing_and_invalid_project_rollback(
    client: TestClient, connection: Connection
) -> None:
    owner = register(client, "routing_a", invitation(connection))
    other = register(client, "routing_b", invitation(connection))
    owner_headers = headers(owner)
    other_headers = headers(other)
    project = client.post(
        "/v1/projects", json={"name": "本地开发"}, headers=owner_headers
    ).json()
    request = {
        "client_submission_id": str(uuid4()),
        "text": "项目里的想法",
        "project_id": project["id"],
    }

    rejected = client.post("/v1/entries", json=request, headers=other_headers)
    assert rejected.status_code == 404
    assert (
        client.get(
            "/v1/entries", params={"project_id": project["id"]}, headers=other_headers
        ).status_code
        == 404
    )
    assert (
        connection.scalar(
            select(func.count())
            .select_from(Entry)
            .where(Entry.owner_id == other.account.id)
        )
        == 0
    )
    retried = client.post(
        "/v1/entries", json={**request, "project_id": None}, headers=other_headers
    )
    assert retried.status_code == 201
    assert retried.json()["project_ids"] == []

    created = client.post("/v1/entries", json=request, headers=owner_headers)
    assert created.status_code == 201
    assert created.json()["project_ids"] == [project["id"]]
    assert (
        client.get("/v1/entries?inbox=true", headers=owner_headers).json()["items"]
        == []
    )
    scoped = client.get(
        "/v1/entries", params={"project_id": project["id"]}, headers=owner_headers
    )
    assert scoped.json()["items"] == [created.json()]
    assert client.get("/v1/entries?inbox=true", headers=other_headers).json()[
        "items"
    ] == [retried.json()]
    assert (
        client.post(
            "/v1/entries", json={**request, "text": "  "}, headers=owner_headers
        ).status_code
        == 422
    )
