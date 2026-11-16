"""Page snapshots through entry APIs, versioning and deletion."""

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Connection, select, update
from sqlalchemy.orm import Session
from test_accounts_projects import headers, invitation, register

from app import deletion
from app.models import EntryRevision, Job

SNAPSHOT: dict[str, Any] = {
    "title": "服务端标题",
    "text": "保存的网页正文 🌿。",
    "provider": "firecrawl",
    "source_url": "https://example.com/page",
    "captured_at": "2026-11-16T02:00:00+00:00",
}


def test_snapshot_detail_history_edits_and_owner_isolation(
    client: TestClient, connection: Connection
) -> None:
    owner = headers(register(client, "web_view_a", invitation(connection)))
    other = headers(register(client, "web_view_b", invitation(connection)))
    created = client.post(
        "/v1/entries",
        headers=owner,
        json={
            "client_submission_id": str(uuid4()),
            "type": "url",
            "url": SNAPSHOT["source_url"],
            "client_extract": {"title": "旧标题", "text": ""},
        },
    ).json()
    saved = UUID(created["id"])
    connection.execute(
        update(EntryRevision)
        .where(EntryRevision.entry_id == saved)
        .values(server_extract=SNAPSHOT)
    )
    detail = client.get(f"/v1/entries/{saved}", headers=owner).json()
    assert (detail["title"], detail["extract_text"]) == (
        SNAPSHOT["title"],
        SNAPSHOT["text"],
    )
    assert client.get(f"/v1/entries/{saved}", headers=other).status_code == 404
    assert (
        client.get(f"/v1/entries/{saved}/revisions/1", headers=other).status_code == 404
    )
    assert client.get("/v1/entries", headers=other).json()["items"] == []
    edited = client.patch(
        f"/v1/entries/{saved}",
        headers=owner,
        json={"base_revision": 1, "note": "补充备注"},
    ).json()
    assert edited["revision"] == 2
    for version in (1, 2):
        view = client.get(
            f"/v1/entries/{saved}/revisions/{version}", headers=owner
        ).json()
        assert (
            view["extract_text"] == SNAPSHOT["text"]
            and view["title"] == SNAPSHOT["title"]
        )
    with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
        rows = db.scalars(
            select(EntryRevision).where(EntryRevision.entry_id == saved)
        ).all()
        assert len(rows) == 2 and all(row.server_extract == SNAPSHOT for row in rows)
    # A deleted snapshot must be scrubbed from every revision, not just hidden.
    assert client.delete(f"/v1/entries/{saved}", headers=owner).status_code == 204
    owner_id = connection.scalar(select(Job.owner_id).where(Job.entry_id == saved))
    assert owner_id is not None
    with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
        deletion.purge_entry(db, owner_id, saved)
        db.commit()
    with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
        assert all(
            row.server_extract is None
            for row in db.scalars(
                select(EntryRevision).where(EntryRevision.entry_id == saved)
            )
        )


def test_extract_failure_visible_and_retry_preserves_backfill_marker(
    client: TestClient, connection: Connection
) -> None:
    owner = headers(register(client, "web_view_c", invitation(connection)))
    created = client.post(
        "/v1/entries",
        headers=owner,
        json={
            "client_submission_id": str(uuid4()),
            "type": "url",
            "url": "https://example.com/",
        },
    ).json()
    saved = UUID(created["id"])
    connection.execute(
        update(Job)
        .where(Job.entry_id == saved)
        .values(
            status="failed",
            stage="extract",
            error_step="extract",
            error_code="web_quota",
            payload={"web_backfill": 1},
        )
    )
    state = client.get(f"/v1/entries/{saved}/processing", headers=owner).json()
    assert (state["failed_step"], state["error_code"], state["can_retry"]) == (
        "extract",
        "web_quota",
        True,
    )
    assert (
        client.post(f"/v1/entries/{saved}/retry", headers=owner).json()["status"]
        == "queued"
    )
    assert connection.scalar(select(Job.payload).where(Job.entry_id == saved)) == {
        "web_backfill": 1
    }
