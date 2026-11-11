"""Relation suggestions: generation in maintenance, and the API."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fake_models import FakeAPI, chat_reply
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import Connection, insert, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_answering import ingest_all, material, project
from test_conversations_api import setup_account
from test_maintenance import run_maintenance, summary_reply
from test_worker import Clock, account

from app import events
from app.models import Entry, ProjectQuestion, Relation


def relation_prompts(api: FakeAPI) -> list[str]:
    return [
        body["messages"][1]["content"]
        for path, body in api.requests
        if path == "completions" and "帮用户整理" in body["messages"][0]["content"]
    ]


def stored(sessions: sessionmaker[Session], project_id: UUID) -> list[Relation]:
    with sessions() as db:
        return list(
            db.scalars(select(Relation).where(Relation.project_id == project_id))
        )


def picks(*items: tuple[str, str]) -> Any:
    return chat_reply(
        {
            "relations": [
                {"target": target, "reason": reason} for target, reason in items
            ]
        }
    )


def test_suggestions_stay_in_the_project_and_are_made_once(
    committed: sessionmaker[Session], saver: PostgresSaver
) -> None:
    owner = account(committed)
    app_project = project(committed, owner, "知识库 App")
    coffee = project(committed, owner, "咖啡烘焙")
    with committed.begin() as db:
        question = ProjectQuestion(
            owner_id=owner, project_id=app_project, text="状态管理选哪个方案？"
        )
        db.add(question)
        db.flush()
        question_id = question.id
    first = material(committed, owner, app_project, "SwiftUI 改用 @Observable 宏。")
    material(committed, owner, app_project, "ObservableObject 写法太啰嗦。")
    material(committed, owner, coffee, "咖啡豆一爆后发展九十秒。")
    api, clock = FakeAPI(), Clock()
    ingest_all(committed, saver, api, clock)

    # Summary first (heavy), then one light call per changed entry with candidates.
    api.chat += [
        summary_reply(),
        picks(("E1", "都在讨论 SwiftUI 状态写法"), ("Q1", "回答了状态方案的问题")),
        picks(("E9", "不存在")),  # invalid target: repaired once
        picks(("E1", "同一主题")),
    ]
    assert run_maintenance(committed, saver, api, clock) == "succeeded"
    prompts = relation_prompts(api)
    assert len(prompts) == 3  # two entries, one repair
    assert all("咖啡" not in prompt for prompt in prompts)
    assert all("状态管理选哪个方案" in prompt for prompt in prompts)
    rows = stored(committed, app_project)
    assert len(rows) == 3
    assert {r.target_question_id for r in rows if r.target_question_id} == {question_id}
    with committed() as db:
        in_app = {
            entry.id
            for entry in db.scalars(select(Entry).where(Entry.owner_id == owner))
        }
    assert all(r.target_entry_id in in_app for r in rows if r.target_entry_id)
    assert all(r.status == "suggested" for r in rows)
    assert stored(committed, coffee) == []

    # Dismiss one; the same entry revision is never suggested again.
    with committed.begin() as db:
        db.execute(
            update(Relation)
            .where(Relation.entry_id == first)
            .values(status="dismissed")
        )
        events.record(db, owner, app_project, events.ENTRY_ADDED, first)
    api.chat.append(summary_reply("新一版 [1]。"))
    assert run_maintenance(committed, saver, api, clock) == "succeeded"
    assert len(relation_prompts(api)) == 3
    assert len(stored(committed, app_project)) == 3


def test_relation_api_accepts_dismisses_and_hides(
    client: TestClient, connection: Connection
) -> None:
    a = setup_account(client, connection, "rel_a")
    b = setup_account(client, connection, "rel_b")

    def entry(text: str) -> str:
        return str(
            client.post(
                "/v1/entries",
                json={
                    "client_submission_id": str(uuid4()),
                    "text": text,
                    "project_id": a["project"],
                },
                headers=a["headers"],
            ).json()["id"]
        )

    source, target = entry("新资料"), entry("旧资料")
    question = client.post(
        f"/v1/projects/{a['project']}/questions",
        json={"text": "要不要加 reranker？"},
        headers=a["headers"],
    ).json()
    ids = [uuid4(), uuid4()]
    for relation_id, values in zip(
        ids,
        [
            {"target_entry_id": UUID(target)},
            {"target_question_id": UUID(question["id"])},
        ],
        strict=True,
    ):
        connection.execute(
            insert(Relation).values(
                id=relation_id,
                owner_id=a["id"],
                project_id=UUID(a["project"]),
                entry_id=UUID(source),
                revision=1,
                reason="理由",
                **values,
            )
        )

    listed = client.get(f"/v1/entries/{source}/relations", headers=a["headers"]).json()
    assert {r["id"] for r in listed} == {str(i) for i in ids}
    entry_link = next(r for r in listed if r["target_entry"])
    assert entry_link["target_entry"]["title"] == "旧资料"
    # The target entry sees the link too.
    assert [
        r["id"]
        for r in client.get(
            f"/v1/entries/{target}/relations", headers=a["headers"]
        ).json()
    ] == [str(ids[0])]
    by_question = client.get(
        f"/v1/projects/{a['project']}/relations",
        params={"question_id": question["id"]},
        headers=a["headers"],
    ).json()
    assert [r["target_question"]["text"] for r in by_question] == [
        "要不要加 reranker？"
    ]

    accepted = client.patch(
        f"/v1/relations/{ids[1]}", json={"status": "accepted"}, headers=a["headers"]
    )
    assert accepted.json()["status"] == "accepted"
    client.patch(
        f"/v1/relations/{ids[0]}", json={"status": "dismissed"}, headers=a["headers"]
    )
    listed = client.get(f"/v1/entries/{source}/relations", headers=a["headers"]).json()
    assert [r["id"] for r in listed] == [str(ids[1])]

    # [T2] Other accounts cannot read or change them.
    assert (
        client.patch(
            f"/v1/relations/{ids[1]}",
            json={"status": "dismissed"},
            headers=b["headers"],
        ).status_code
        == 404
    )
    assert (
        client.get(f"/v1/entries/{source}/relations", headers=b["headers"]).status_code
        == 404
    )
    # An edited source hides suggestions made for the old revision.
    client.patch(
        f"/v1/entries/{source}",
        json={"base_revision": 1, "text": "新资料第二版"},
        headers=a["headers"],
    )
    assert (
        client.get(f"/v1/entries/{source}/relations", headers=a["headers"]).json() == []
    )
    connection.execute(
        update(Entry)
        .where(Entry.id == UUID(target))
        .values(deleted_at=datetime.now(UTC))
    )
