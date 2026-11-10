from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Connection, func, insert, select, update
from test_accounts_projects import headers, invitation, register

from app.models import Chunk, Job


def set_job(connection: Connection, entry_id: str, **values: object) -> None:
    connection.execute(
        update(Job).where(Job.entry_id == UUID(entry_id)).values(**values)
    )


def test_processing_status_projection_and_retry(
    client: TestClient, connection: Connection
) -> None:
    owner = headers(register(client, "process_a", invitation(connection)))
    other = headers(register(client, "process_b", invitation(connection)))
    entry = client.post(
        "/v1/entries",
        json={"client_submission_id": str(uuid4()), "text": "需要整理的想法"},
        headers=owner,
    ).json()
    path = f"/v1/entries/{entry['id']}"
    assert entry["processing"] == "queued"

    state = client.get(f"{path}/processing", headers=owner).json()
    assert state["status"] == "queued"
    assert (state["searchable"], state["can_retry"], state["attempts"]) == (
        False,
        False,
        0,
    )
    assert client.post(f"{path}/retry", headers=owner).json()["status"] == "queued"

    now = datetime.now(UTC)
    set_job(connection, entry["id"], status="running", indexed_at=now, stage="card")
    assert client.get(path, headers=owner).json()["processing"] == "searchable"

    set_job(
        connection,
        entry["id"],
        status="failed",
        attempts=3,
        error_code="invalid_output",
        error_step="card",
    )
    state = client.get(f"{path}/processing", headers=owner).json()
    assert state["status"] == "partial"
    assert (state["failed_step"], state["error_code"], state["can_retry"]) == (
        "card",
        "invalid_output",
        True,
    )
    listed = client.get("/v1/entries", headers=owner).json()["items"]
    assert listed[0]["processing"] == "partial"

    assert client.post(f"{path}/retry", headers=other).status_code == 404
    assert client.get(f"{path}/processing", headers=other).status_code == 404

    retried = client.post(f"{path}/retry", headers=owner)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "searchable"  # index kept, card queued
    job = connection.execute(
        select(Job.status, Job.attempts, Job.generation, Job.error_code).where(
            Job.entry_id == UUID(entry["id"])
        )
    ).one()
    assert job == ("queued", 0, 1, None)
    # Retrying again while queued reuses the same job.
    client.post(f"{path}/retry", headers=owner)
    assert (
        connection.scalar(
            select(Job.generation).where(Job.entry_id == UUID(entry["id"]))
        )
        == 1
    )

    set_job(connection, entry["id"], status="failed", indexed_at=None)
    assert client.get(path, headers=owner).json()["processing"] == "failed"

    set_job(connection, entry["id"], status="succeeded", indexed_at=now)
    finished = client.post(f"{path}/retry", headers=owner)
    assert finished.status_code == 409
    assert finished.json()["detail"] == "processing already finished"


def test_new_revision_reports_its_own_processing(
    client: TestClient, connection: Connection
) -> None:
    owner = headers(register(client, "process_c", invitation(connection)))
    entry = client.post(
        "/v1/entries",
        json={"client_submission_id": str(uuid4()), "text": "第一版"},
        headers=owner,
    ).json()
    set_job(connection, entry["id"], status="succeeded")
    edited = client.patch(
        f"/v1/entries/{entry['id']}",
        json={"base_revision": 1, "text": "第二版"},
        headers=owner,
    ).json()
    assert edited["processing"] == "queued"
    state = client.get(f"/v1/entries/{entry['id']}/processing", headers=owner).json()
    assert state["status"] == "queued"


def test_delete_cancels_running_job_and_removes_index(
    client: TestClient, connection: Connection
) -> None:
    owner = headers(register(client, "process_d", invitation(connection)))
    entry = client.post(
        "/v1/entries",
        json={"client_submission_id": str(uuid4()), "text": "将被删除的想法"},
        headers=owner,
    ).json()
    entry_id = UUID(entry["id"])
    owner_id = connection.scalar(select(Job.owner_id).where(Job.entry_id == entry_id))
    set_job(connection, entry["id"], status="running", lease_owner="worker-1")
    connection.execute(
        insert(Chunk).values(
            owner_id=owner_id,
            entry_id=entry_id,
            revision=1,
            unit="chunk",
            ordinal=0,
            source="text",
            start_offset=0,
            end_offset=7,
            display_text="将被删除的想法",
            index_text="将被删除的想法",
            tokens=func.to_tsvector("simple", "将 被 删除 的 想法"),
            context_header_version=1,
            tokenizer_version=1,
            index_version=1,
        )
    )

    assert client.delete(f"/v1/entries/{entry['id']}", headers=owner).status_code == 204
    job = connection.execute(
        select(Job.status, Job.lease_owner).where(Job.entry_id == entry_id)
    ).one()
    assert job == ("cancelled", None)
    assert (
        connection.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.entry_id == entry_id)
        )
        == 0
    )
