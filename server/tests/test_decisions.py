"""Decision drafts ([T6]): generation limits and dedupe, and the state machine."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fake_models import FakeAPI, chat_reply
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import Connection, func, insert, select
from sqlalchemy.orm import Session, sessionmaker
from test_conversations_api import setup_account
from test_maintenance import run_maintenance, summary_reply
from test_worker import Clock

from app import decisions, events, jobs
from app.models import Decision, DecisionRevision, Job, ProjectEvent

DRAFTS = "值得做出决定"


def draft(
    title: str, rationale: str = "资料里倾向 @Observable [1]。", **extra: Any
) -> dict[str, Any]:
    return {
        "title": title,
        "choice": f"建议{title}",
        "rationale": rationale,
        "alternatives": ["继续用 ObservableObject"],
        "premises": ["最低支持 iOS 17"],
        "citations": [1],
        **extra,
    }


def drafts_reply(*items: dict[str, Any]) -> Any:
    return chat_reply({"drafts": list(items)})


def stored(sessions: sessionmaker[Session], project_id: UUID) -> list[Decision]:
    with sessions() as db:
        return list(
            db.scalars(
                select(Decision)
                .where(Decision.project_id == project_id)
                .order_by(Decision.created_at, Decision.title)
            )
        )


def draft_calls(api: FakeAPI) -> int:
    return sum(
        1
        for path, body in api.requests
        if path == "completions" and DRAFTS in body["messages"][0]["content"]
    )


def test_drafts_are_proposed_never_confirmed_and_wording_is_checked(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    _owner, project_id, api, clock = ready_project
    api.script("当前认识", summary_reply())
    api.script(
        DRAFTS,
        drafts_reply(draft("状态管理方案", choice="已决定用 @Observable")),
        drafts_reply(draft("状态管理方案"), draft("上传重试策略")),
    )
    assert run_maintenance(committed, saver, api, clock) == "succeeded"
    assert draft_calls(api) == 2  # one repair for forbidden wording
    rows = stored(committed, project_id)
    assert [(d.title, d.status) for d in rows] == [
        ("上传重试策略", "draft"),
        ("状态管理方案", "draft"),
    ]
    assert all(d.confirmed_at is None for d in rows)  # [T6]
    assert rows[1].sources[0]["n"] == 1 and rows[1].topic_key == "状态管理方案"


def test_daily_limit_and_topic_dedupe(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, project_id, api, clock = ready_project
    api.script(
        "当前认识",
        summary_reply(),
        summary_reply("二版 [1]。"),
        summary_reply("三版 [1]。"),
    )
    api.script(DRAFTS, drafts_reply(draft("方案 A"), draft("方案 B")))
    run_maintenance(committed, saver, api, clock)
    with committed.begin() as db:
        events.record(db, owner, project_id, events.ENTRY_ADDED)
    # " 方案A " normalizes to the live draft's topic and is dropped.
    api.script(DRAFTS, drafts_reply(draft(" 方案A "), draft("方案 C")))
    run_maintenance(committed, saver, api, clock)
    assert [d.title for d in stored(committed, project_id)] == [
        "方案 A",
        "方案 B",
        "方案 C",
    ]

    with committed.begin() as db:
        events.record(db, owner, project_id, events.ENTRY_ADDED)
    calls = draft_calls(api)
    run_maintenance(committed, saver, api, clock)
    assert draft_calls(api) == calls  # 3 today already: no model call


def test_rejected_topics_return_only_on_new_evidence(
    committed: sessionmaker[Session],
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, project_id, api, clock = ready_project
    evidence = [
        {
            "n": 1,
            "entry_id": str(uuid4()),
            "revision": 1,
            "chunk_id": str(uuid4()),
            "type": "thought",
            "title": "",
            "created_at": "2026-11-11T00:00:00+00:00",
            "text": "倾向 @Observable",
        }
    ]
    with committed.begin() as db:
        db.add(
            Decision(
                owner_id=owner,
                project_id=project_id,
                status="rejected",
                title="状态管理",
                choice="建议 X",
                topic_key=decisions.topic_key("状态管理"),
                source_fingerprint=decisions.fingerprint(evidence),
            )
        )
    router = api.models().router
    api.script(DRAFTS, drafts_reply(draft("状态管理")))
    with committed() as db:
        same = decisions.propose(
            db, owner, project_id, evidence, router, lambda _call: None, clock()
        )
    assert same == []
    changed = [{**evidence[0], "revision": 2}]
    api.script(DRAFTS, drafts_reply(draft("状态管理")))
    with committed() as db:
        again = decisions.propose(
            db, owner, project_id, changed, router, lambda _call: None, clock()
        )
    assert [d["title"] for d in again] == ["状态管理"]


def test_no_new_material_means_no_drafting(
    committed: sessionmaker[Session],
    saver: PostgresSaver,
    ready_project: tuple[UUID, UUID, FakeAPI, Clock],
) -> None:
    owner, project_id, api, clock = ready_project
    api.script("当前认识", summary_reply(), summary_reply("问题变了 [1]。"))
    run_maintenance(committed, saver, api, clock)
    calls = draft_calls(api)
    with committed.begin() as db:
        events.record(db, owner, project_id, events.QUESTION_ADDED)
    run_maintenance(committed, saver, api, clock)
    assert draft_calls(api) == calls


@pytest.fixture
def decision_setup(client: TestClient, connection: Connection) -> dict[str, Any]:
    a = setup_account(client, connection, "dec_a")
    entry = client.post(
        "/v1/entries",
        json={
            "client_submission_id": str(uuid4()),
            "text": "倾向 @Observable",
            "project_id": a["project"],
        },
        headers=a["headers"],
    ).json()
    ids = [uuid4(), uuid4()]
    for decision_id, title in zip(ids, ["状态管理", "存储方案"], strict=True):
        connection.execute(
            insert(Decision).values(
                id=decision_id,
                owner_id=a["id"],
                project_id=UUID(a["project"]),
                title=title,
                choice=f"建议{title}",
                rationale="理由 [1]",
                sources=[
                    {
                        "n": 1,
                        "entry_id": entry["id"],
                        "revision": 1,
                        "chunk_id": str(uuid4()),
                        "text": "倾向 @Observable",
                    }
                ],
                topic_key=title,
                source_fingerprint="x",
            )
        )
    return {**a, "entry": entry["id"], "ids": ids}


def act(client: TestClient, a: dict[str, Any], decision_id: UUID, **body: Any) -> Any:
    return client.patch(f"/v1/decisions/{decision_id}", json=body, headers=a["headers"])


def test_confirm_is_explicit_idempotent_and_versioned(
    client: TestClient, connection: Connection, decision_setup: dict[str, Any]
) -> None:
    a = decision_setup
    first, second = a["ids"]
    drafts = client.get(
        f"/v1/projects/{a['project']}/decisions",
        params={"status": "draft"},
        headers=a["headers"],
    ).json()
    assert {d["title"] for d in drafts} == {"状态管理", "存储方案"}
    assert drafts[0]["basis_changed"] is False

    confirmed = act(
        client, a, first, action="confirm", version=1, choice="用 @Observable"
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert (body["status"], body["choice"], body["version"]) == (
        "confirmed",
        "用 @Observable",
        2,
    )
    assert body["confirmed_at"] is not None
    # [T6] Confirming again (even with a stale version) changes nothing.
    again = act(client, a, first, action="confirm", version=1)
    assert again.status_code == 200 and again.json()["version"] == 2
    count = connection.scalar(
        select(func.count())
        .select_from(DecisionRevision)
        .where(DecisionRevision.decision_id == first)
    )
    assert count == 1
    assert (
        connection.scalar(
            select(func.count())
            .select_from(ProjectEvent)
            .where(
                ProjectEvent.kind == events.DECISION_CONFIRMED,
                ProjectEvent.subject_id == first,
            )
        )
        == 1
    )
    assert (
        connection.scalar(
            select(Job.status).where(
                Job.project_id == UUID(a["project"]), Job.kind == jobs.MAINTENANCE
            )
        )
        == "queued"
    )

    assert act(client, a, first, action="reject", version=2).status_code == 409
    assert (
        act(client, a, first, action="revise", version=1, choice="x").status_code == 409
    )
    revised = act(client, a, first, action="revise", version=2, rationale="新理由")
    assert revised.json()["version"] == 3
    history = client.get(
        f"/v1/decisions/{first}/revisions", headers=a["headers"]
    ).json()
    assert [h["version"] for h in history] == [2, 1]
    assert history[0]["rationale"] == "新理由"

    assert (
        act(client, a, second, action="reject", version=1).json()["status"]
        == "rejected"
    )
    assert (
        act(
            client, a, first, action="supersede", version=3, superseded_by=str(second)
        ).status_code
        == 409
    )
    third = uuid4()
    connection.execute(
        insert(Decision).values(
            id=third,
            owner_id=a["id"],
            project_id=UUID(a["project"]),
            status="confirmed",
            title="新的状态管理",
            choice="改用 X",
            topic_key="新的状态管理",
            source_fingerprint="y",
        )
    )
    superseded = act(
        client, a, first, action="supersede", version=3, superseded_by=str(third)
    )
    assert superseded.json()["status"] == "superseded"
    assert superseded.json()["superseded_by"] == str(third)


def test_basis_changes_and_isolation(
    client: TestClient, connection: Connection, decision_setup: dict[str, Any]
) -> None:
    a = decision_setup
    first = a["ids"][0]
    client.patch(
        f"/v1/entries/{a['entry']}",
        json={"base_revision": 1, "text": "改主意了"},
        headers=a["headers"],
    )
    view = client.get(f"/v1/decisions/{first}", headers=a["headers"]).json()
    assert view["basis_changed"] is True
    assert view["sources"][0]["stale"] is True

    b = setup_account(client, connection, "dec_b")
    assert client.get(f"/v1/decisions/{first}", headers=b["headers"]).status_code == 404
    assert act(client, b, first, action="confirm", version=1).status_code == 404
    assert (
        client.get(
            f"/v1/projects/{a['project']}/decisions", headers=b["headers"]
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"/v1/decisions/{first}",
            json={"action": "supersede", "version": 1},
            headers=a["headers"],
        ).status_code
        == 422
    )


def test_drafts_state_the_option_and_need_an_alternative() -> None:
    reply = drafts_reply(
        draft("路线", choice="建议选择 A 线"),
        draft("装备", choice="建议带雨衣", alternatives=[]),
    )
    content = reply["choices"][0]["message"]["content"]
    result = decisions.validate_drafts(content, {1})
    assert [(d.title, d.choice) for d in result.drafts] == [("路线", "A 线")]
    assert decisions.plain_choice("推荐：用 X") == "用 X"
    assert decisions.plain_choice("建议") == "建议"
