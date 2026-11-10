from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Connection, select, update
from sqlalchemy.orm import Session
from test_accounts_projects import headers, invitation, register

from app import storage
from app.admin import cleanup_attachments
from app.models import Attachment, Job

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def upload(
    client: TestClient,
    auth: dict[str, str],
    submission: str,
    data: bytes = PNG,
    digest: str | None = None,
) -> Any:
    return client.post(
        "/v1/attachments",
        data={
            "client_submission_id": submission,
            "sha256": digest or sha256(data).hexdigest(),
        },
        files={"file": ("shot.png", data, "image/png")},
        headers=auth,
    )


def test_link_entries_keep_extract_and_flag_same_page(
    client: TestClient, connection: Connection
) -> None:
    auth = headers(register(client, "links_a", invitation(connection)))
    request = {
        "client_submission_id": str(uuid4()),
        "type": "url",
        "url": "HTTPS://Example.com/post#section",
        "note": "读一下",
        "client_extract": {"title": "一篇文章", "text": "正文内容"},
    }
    created = client.post("/v1/entries", json=request, headers=auth)
    assert created.status_code == 201, created.text
    entry = created.json()
    assert (entry["type"], entry["title"], entry["text"]) == ("url", "一篇文章", "")
    assert entry["source_url"] == "HTTPS://Example.com/post#section"
    assert entry["processing"] == "queued"
    detail = client.get(f"/v1/entries/{entry['id']}", headers=auth).json()
    assert detail["extract_text"] == "正文内容"

    again = client.post(
        "/v1/entries",
        json={
            "client_submission_id": str(uuid4()),
            "type": "url",
            "url": "https://example.com/post",
        },
        headers=auth,
    )
    assert again.json()["possible_duplicate"] is True

    invalid: list[dict[str, Any]] = [
        {"type": "url"},
        {"type": "url", "url": "ftp://example.com/file"},
        {"type": "url", "url": "https://user:pw@example.com/"},
        {"type": "thought", "text": "想法", "url": "https://example.com/"},
        {"type": "thought", "text": "想法", "client_extract": {"title": "x"}},
        {"type": "image"},
        {"type": "thought", "text": ""},
    ]
    for body in invalid:
        response = client.post(
            "/v1/entries",
            json={"client_submission_id": str(uuid4()), **body},
            headers=auth,
        )
        assert response.status_code == 422, body


def test_image_upload_is_idempotent_checked_and_private(
    client: TestClient, connection: Connection, storage_dir: Path
) -> None:
    owner = headers(register(client, "images_a", invitation(connection)))
    other = headers(register(client, "images_b", invitation(connection)))
    submission = str(uuid4())

    first = upload(client, owner, submission)
    assert first.status_code == 201, first.text
    attachment = first.json()
    assert (attachment["content_type"], attachment["size"]) == ("image/png", len(PNG))
    assert upload(client, owner, submission).json() == attachment
    assert upload(client, owner, submission).status_code == 200
    assert upload(client, owner, submission, PNG + b"x").status_code == 409
    assert upload(client, owner, str(uuid4()), digest="0" * 64).status_code == 422
    assert upload(client, owner, str(uuid4()), b"plain text").status_code == 415
    too_large = b"\xff\xd8\xff" + b"\x00" * storage.MAX_ATTACHMENT_BYTES
    assert upload(client, owner, str(uuid4()), too_large).status_code == 413
    assert list((storage_dir / "tmp").iterdir()) == []

    ocr = {
        "text": "截图文字",
        "lines": [{"text": "截图文字", "box": [0.1, 0.1, 0.5, 0.05]}],
    }
    request = {
        "client_submission_id": submission,
        "type": "image",
        "attachment_id": attachment["id"],
        "client_ocr": ocr,
    }
    assert client.post("/v1/entries", json=request, headers=other).status_code == 404
    mismatched = {**request, "client_submission_id": str(uuid4())}
    assert client.post("/v1/entries", json=mismatched, headers=owner).status_code == 404
    created = client.post("/v1/entries", json=request, headers=owner)
    assert created.status_code == 201, created.text
    entry = created.json()
    assert entry["attachment"] == attachment
    detail = client.get(f"/v1/entries/{entry['id']}", headers=owner).json()
    assert detail["ocr_text"] == "截图文字"

    path = f"/v1/attachments/{attachment['id']}"
    image = client.get(path, headers=owner)
    assert image.status_code == 200
    assert image.content == PNG
    assert image.headers["content-type"] == "image/png"
    assert client.get(path, headers=other).status_code == 404

    assert client.delete(f"/v1/entries/{entry['id']}", headers=owner).status_code == 204
    assert client.get(path, headers=owner).status_code == 404


def test_edit_creates_revisions_and_jobs(
    client: TestClient, connection: Connection
) -> None:
    owner = register(client, "edits_a", invitation(connection))
    auth = headers(owner)
    other = headers(register(client, "edits_b", invitation(connection)))
    entry = client.post(
        "/v1/entries",
        json={"client_submission_id": str(uuid4()), "text": "初稿", "note": "备注"},
        headers=auth,
    ).json()
    path = f"/v1/entries/{entry['id']}"

    edited = client.patch(
        path, json={"base_revision": 1, "text": "修改后"}, headers=auth
    )
    assert edited.status_code == 200, edited.text
    assert (
        edited.json()["revision"],
        edited.json()["text"],
        edited.json()["note"],
    ) == (
        2,
        "修改后",
        "备注",
    )
    assert (
        client.patch(
            path, json={"base_revision": 1, "note": "x"}, headers=auth
        ).status_code
        == 409
    )
    assert (
        client.patch(path, json={"base_revision": 2}, headers=auth).status_code == 422
    )
    assert (
        client.patch(
            path, json={"base_revision": 2, "text": ""}, headers=auth
        ).status_code
        == 422
    )
    assert (
        client.patch(
            path, json={"base_revision": 2, "note": "x"}, headers=other
        ).status_code
        == 404
    )

    revisions = client.get(f"{path}/revisions", headers=auth).json()
    assert [(r["revision"], r["text"]) for r in revisions] == [
        (2, "修改后"),
        (1, "初稿"),
    ]
    assert client.get(f"{path}/revisions", headers=other).status_code == 404
    jobs = connection.execute(
        select(Job.revision, Job.status)
        .where(Job.owner_id == owner.account.id)
        .order_by(Job.revision)
    ).all()
    assert jobs == [(1, "queued"), (2, "queued")]


def test_delete_hides_entry_and_cancels_jobs(
    client: TestClient, connection: Connection
) -> None:
    owner = register(client, "deletes_a", invitation(connection))
    auth = headers(owner)
    request = {"client_submission_id": str(uuid4()), "text": "要删除的想法"}
    entry = client.post("/v1/entries", json=request, headers=auth).json()
    path = f"/v1/entries/{entry['id']}"

    assert (
        client.delete(
            path, headers=headers(register(client, "deletes_b", invitation(connection)))
        ).status_code
        == 404
    )
    assert client.delete(path, headers=auth).status_code == 204
    assert client.delete(path, headers=auth).status_code == 404
    assert client.get(path, headers=auth).status_code == 404
    assert client.get("/v1/entries?inbox=true", headers=auth).json()["items"] == []
    assert client.post("/v1/entries", json=request, headers=auth).status_code == 409
    assert (
        connection.scalar(select(Job.status).where(Job.owner_id == owner.account.id))
        == "cancelled"
    )
    fresh = client.post(
        "/v1/entries",
        json={**request, "client_submission_id": str(uuid4())},
        headers=auth,
    )
    assert fresh.json()["possible_duplicate"] is False


def test_project_membership_is_owner_scoped(
    client: TestClient, connection: Connection
) -> None:
    owner = headers(register(client, "members_a", invitation(connection)))
    other = headers(register(client, "members_b", invitation(connection)))
    project = client.post("/v1/projects", json={"name": "项目"}, headers=owner).json()
    foreign_project = client.post(
        "/v1/projects", json={"name": "别人的"}, headers=other
    ).json()
    entry = client.post(
        "/v1/entries",
        json={"client_submission_id": str(uuid4()), "text": "想法"},
        headers=owner,
    ).json()
    foreign_entry = client.post(
        "/v1/entries",
        json={"client_submission_id": str(uuid4()), "text": "想法"},
        headers=other,
    ).json()
    link = f"/v1/projects/{project['id']}/entries/{entry['id']}"

    for method in ("put", "delete"):
        for path, auth in (
            (link, other),
            (f"/v1/projects/{foreign_project['id']}/entries/{entry['id']}", owner),
            (f"/v1/projects/{project['id']}/entries/{foreign_entry['id']}", owner),
        ):
            assert client.request(method, path, headers=auth).status_code == 404, (
                method,
                path,
            )

    assert client.put(link, headers=owner).status_code == 204
    assert client.put(link, headers=owner).status_code == 204
    assert client.get("/v1/entries?inbox=true", headers=owner).json()["items"] == []
    scoped = client.get(
        "/v1/entries", params={"project_id": project["id"]}, headers=owner
    ).json()
    assert [item["id"] for item in scoped["items"]] == [entry["id"]]
    assert scoped["items"][0]["project_ids"] == [project["id"]]

    assert client.delete(link, headers=owner).status_code == 204
    inbox = client.get("/v1/entries?inbox=true", headers=owner).json()["items"]
    assert [item["id"] for item in inbox] == [entry["id"]]


def test_cleanup_removes_only_stale_unlinked_uploads(
    client: TestClient, connection: Connection, storage_dir: Path
) -> None:
    owner = register(client, "cleanup_a", invitation(connection))
    auth = headers(owner)
    stale = upload(client, auth, str(uuid4())).json()
    linked_submission = str(uuid4())
    linked = upload(client, auth, linked_submission).json()
    client.post(
        "/v1/entries",
        json={
            "client_submission_id": linked_submission,
            "type": "image",
            "attachment_id": linked["id"],
        },
        headers=auth,
    )
    fresh = upload(client, auth, str(uuid4())).json()
    connection.execute(
        update(Attachment)
        .where(Attachment.id.in_([UUID(stale["id"]), UUID(linked["id"])]))
        .values(created_at=datetime.now(UTC) - timedelta(days=2))
    )
    leftover = storage_dir / "tmp" / "leftover"
    leftover.write_bytes(b"partial")

    with Session(bind=connection, join_transaction_mode="create_savepoint") as db:
        cleanup_attachments(db, datetime.now(UTC) + timedelta(hours=2))

    remaining = set(
        connection.scalars(
            select(Attachment.id).where(Attachment.owner_id == owner.account.id)
        )
    )
    assert remaining == {UUID(linked["id"]), UUID(fresh["id"])}
    assert not storage.attachment_path(owner.account.id, UUID(stale["id"])).exists()
    assert storage.attachment_path(owner.account.id, UUID(linked["id"])).exists()
    assert not leftover.exists()


def test_oversized_upload_is_rejected_before_reading(
    client: TestClient, connection: Connection, storage_dir: Path
) -> None:
    auth = headers(register(client, "oversize_a", invitation(connection)))
    body = b"\xff\xd8\xff" + b"\x00" * (storage.MAX_ATTACHMENT_BYTES + 128 * 1024)
    response = upload(client, auth, str(uuid4()), body)
    assert response.status_code == 413
    assert response.json() == {"detail": "attachment exceeds 10 MiB"}
    assert not (storage_dir / "tmp").exists()


def test_delete_project_keeps_entries_and_is_owner_scoped(
    client: TestClient, connection: Connection
) -> None:
    owner = headers(register(client, "projdel_a", invitation(connection)))
    other = headers(register(client, "projdel_b", invitation(connection)))
    project = client.post("/v1/projects", json={"name": "要删的"}, headers=owner).json()
    keep = client.post("/v1/projects", json={"name": "保留"}, headers=owner).json()
    client.post(
        f"/v1/projects/{project['id']}/questions", json={"text": "问题"}, headers=owner
    )
    only_here = client.post(
        "/v1/entries",
        json={
            "client_submission_id": str(uuid4()),
            "text": "只在这里",
            "project_id": project["id"],
        },
        headers=owner,
    ).json()
    both = client.post(
        "/v1/entries",
        json={
            "client_submission_id": str(uuid4()),
            "text": "两个项目",
            "project_id": keep["id"],
        },
        headers=owner,
    ).json()
    client.put(f"/v1/projects/{project['id']}/entries/{both['id']}", headers=owner)
    path = f"/v1/projects/{project['id']}"

    assert client.delete(path, headers=other).status_code == 404
    assert client.delete(path, headers=owner).status_code == 204
    assert client.delete(path, headers=owner).status_code == 404
    assert client.get(path, headers=owner).status_code == 404
    assert [
        p["id"] for p in client.get("/v1/projects", headers=owner).json()["items"]
    ] == [keep["id"]]
    inbox = client.get("/v1/entries?inbox=true", headers=owner).json()["items"]
    assert [e["id"] for e in inbox] == [only_here["id"]]
    detail = client.get(f"/v1/entries/{both['id']}", headers=owner).json()
    assert detail["project_ids"] == [keep["id"]]
