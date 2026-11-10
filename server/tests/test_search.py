"""Hybrid search: ranking, card mapping, scope and isolation ([T2], [T3])."""

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, insert, update
from test_accounts_projects import headers, invitation, register

from app import search, tokenizer
from app.jobs import Retryable
from app.main import app
from app.models import EMBEDDING_DIMENSION, Chunk, Entry
from app.providers import Call, Recorder
from app.search_api import get_embedder


def basis(index: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[index] = 1.0
    return vector


class FakeEmbedder:
    """Maps known texts to fixed vectors; anything else to an unrelated axis."""

    def __init__(self, vectors: dict[str, list[float]], *, fail: bool = False) -> None:
        self.vectors = vectors
        self.fail = fail
        self.calls = 0

    def embed(
        self, texts: Sequence[str], task: str, record: Recorder
    ) -> list[list[float]]:
        self.calls += 1
        record(
            Call(
                task,
                "qwen",
                "fake-embedding",
                10,
                0,
                1,
                "network" if self.fail else None,
            )
        )
        if self.fail:
            raise Retryable("network")
        return [
            self.vectors.get(text, basis(EMBEDDING_DIMENSION - 1)) for text in texts
        ]


@pytest.fixture
def embedder() -> Iterator[FakeEmbedder]:
    fake = FakeEmbedder({})
    app.dependency_overrides[get_embedder] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_embedder)


class Account:
    def __init__(self, client: TestClient, connection: Connection, name: str) -> None:
        credentials = register(client, name, invitation(connection))
        self.id = credentials.account.id
        self.headers = headers(credentials)
        self.client = client
        self.connection = connection

    def project(self, name: str) -> str:
        response = self.client.post(
            "/v1/projects", json={"name": name}, headers=self.headers
        )
        return str(response.json()["id"])

    def entry(self, text: str, project: str | None = None, **extra: Any) -> UUID:
        body = {"client_submission_id": str(uuid4()), "text": text, **extra}
        if project:
            body["project_id"] = project
        response = self.client.post("/v1/entries", json=body, headers=self.headers)
        assert response.status_code == 201, response.text
        return UUID(response.json()["id"])

    def index(
        self,
        entry_id: UUID,
        text: str,
        vector: list[float] | None,
        *,
        unit: str = "chunk",
        revision: int = 1,
        ordinal: int = 0,
    ) -> UUID:
        chunk_id = uuid4()
        self.connection.execute(
            insert(Chunk).values(
                id=chunk_id,
                owner_id=self.id,
                entry_id=entry_id,
                revision=revision,
                unit=unit,
                ordinal=ordinal,
                source="card" if unit == "card" else "text",
                start_offset=0,
                end_offset=len(text),
                display_text=text,
                index_text=text,
                tokens=func_tsvector(text),
                embedding=vector,
                context_header_version=1,
                tokenizer_version=1,
                index_version=1,
            )
        )
        return chunk_id


def func_tsvector(text: str) -> Any:
    from sqlalchemy import func

    return func.to_tsvector("simple", tokenizer.index_text(text))


def search_api(account: Account, **params: Any) -> dict[str, Any]:
    response = account.client.get("/v1/search", params=params, headers=account.headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def ids(page: dict[str, Any]) -> list[str]:
    return [item["entry"]["id"] for item in page["items"]]


def test_hybrid_search_ranks_maps_cards_and_isolates_accounts(
    client: TestClient, connection: Connection, embedder: FakeEmbedder
) -> None:
    a = Account(client, connection, "search_a")
    b = Account(client, connection, "search_b")
    app_project = a.project("知识库 App")
    coffee = a.project("咖啡烘焙")

    swift = a.entry("SwiftUI 状态管理用 @Observable 宏", app_project)
    swift_chunk = a.index(swift, "SwiftUI 状态管理用 @Observable 宏", basis(0))
    sync = a.entry("离线优先需要幂等键", app_project)
    sync_chunk = a.index(sync, "离线优先需要幂等键", basis(1))
    a.index(sync, "离线同步设计 冲突处理 幂等", basis(2), unit="card")
    roast = a.entry("一爆之后再烘两分钟", coffee)
    a.index(roast, "一爆之后再烘两分钟", basis(3))
    other = b.entry("SwiftUI 状态管理用 @Observable 宏")
    b.index(other, "SwiftUI 状态管理用 @Observable 宏", basis(0))

    embedder.vectors = {"SwiftUI 状态管理": basis(0), "冲突怎么处理": basis(2)}
    page = search_api(a, q="SwiftUI 状态管理")
    assert ids(page)[0] == str(swift)
    first = page["items"][0]
    assert first["chunk_id"] == str(swift_chunk)
    assert first["matched_by"] == ["keyword", "semantic"]
    assert str(other) not in ids(page)  # [T2]
    assert page["degraded"] is False
    snippet = first["snippet"]
    marked = {snippet[start:end] for start, end in first["highlights"]}
    assert {"SwiftUI", "状态", "管理"} <= marked

    # A card hit is reported, but the evidence is the entry's source chunk.
    card_hit = search_api(a, q="冲突怎么处理")["items"][0]
    assert card_hit["entry"]["id"] == str(sync)
    assert "card" in card_hit["matched_by"]
    assert card_hit["chunk_id"] == str(sync_chunk)
    assert card_hit["source"] == "text"

    # [T3] A project scope excludes other projects; global search finds all.
    embedder.vectors = {"一爆": basis(3)}
    assert str(roast) not in ids(search_api(a, q="一爆", project_id=app_project))
    assert ids(search_api(a, q="一爆"))[0] == str(roast)
    assert ids(search_api(a, q="一爆", project_id=coffee)) == [str(roast)]
    other_project = b.project("别人的项目")
    response = client.get(
        "/v1/search", params={"q": "x", "project_id": other_project}, headers=a.headers
    )
    assert response.status_code == 404


def test_keyword_only_when_embedding_fails_and_stale_rows_are_excluded(
    client: TestClient, connection: Connection, embedder: FakeEmbedder
) -> None:
    a = Account(client, connection, "search_c")
    edited = a.entry("第一版")
    a.index(edited, "旧版本独有词汇", None, revision=1)
    a.client.patch(
        f"/v1/entries/{edited}",
        json={"base_revision": 1, "text": "第二版"},
        headers=a.headers,
    )
    a.index(edited, "第二版的新内容", None, revision=2)
    deleted = a.entry("将删除")
    a.index(deleted, "已删除资料里的独有词汇", None)
    connection.execute(
        update(Entry).where(Entry.id == deleted).values(deleted_at=datetime.now(UTC))
    )

    embedder.fail = True
    page = search_api(a, q="独有词汇")
    assert page["degraded"] is True
    assert page["items"] == []
    page = search_api(a, q="新内容")
    assert ids(page) == [str(edited)]
    assert page["items"][0]["matched_by"] == ["keyword"]


def test_card_weight_zero_ignores_cards_and_filters_apply(
    client: TestClient, connection: Connection, embedder: FakeEmbedder
) -> None:
    from sqlalchemy.orm import Session

    a = Account(client, connection, "search_d")
    note = a.entry("一条普通想法")
    a.index(note, "一条普通想法", basis(5))
    a.index(note, "主题词 向量数据库", basis(6), unit="card")
    link = a.entry("", type="url", url="https://example.com/pgvector")
    a.index(link, "pgvector 支持向量数据库与全文检索混合", basis(7))

    with Session(bind=connection) as db:

        def run(**options: Any) -> search.SearchResult:
            return search.retrieve(
                db,
                a.id,
                "向量数据库",
                embedder=None,
                record=lambda call: None,
                paths=("keyword",),
                **options,
            )

        assert {hit.entry_id for hit in run().hits} == {note, link}
        assert {hit.entry_id for hit in run(card_weight=0).hits} == {link}
        typed = run(filters=search.Filters(types=["url"]))
        assert [hit.entry_id for hit in typed.hits] == [link]
        later = run(filters=search.Filters(since=datetime(2999, 1, 1, tzinfo=UTC)))
        assert later.hits == []

        embedder.vectors = {"向量数据库": basis(7)}
        near = search.retrieve(
            db,
            a.id,
            "向量数据库",
            embedder=embedder,  # type: ignore[arg-type]
            record=lambda call: None,
            paths=("semantic",),
            max_distance=0.5,
        )
        assert [hit.entry_id for hit in near.hits] == [link]


def test_pagination_cursor_and_validation(
    client: TestClient, connection: Connection, embedder: FakeEmbedder
) -> None:
    a = Account(client, connection, "search_e")
    for number in range(5):
        entry_id = a.entry(f"分页测试资料 {number}")
        a.index(entry_id, f"分页测试资料 {number}", basis(10 + number))
    first = search_api(a, q="分页测试", limit=2)
    second = search_api(a, q="分页测试", limit=2, cursor=first["next_cursor"])
    third = search_api(a, q="分页测试", limit=2, cursor=second["next_cursor"])
    seen = ids(first) + ids(second) + ids(third)
    assert len(seen) == len(set(seen)) == 5
    assert third["next_cursor"] is None

    bad = client.get(
        "/v1/search", params={"q": "x", "cursor": "nope"}, headers=a.headers
    )
    assert bad.status_code == 400
    assert (
        client.get("/v1/search", params={"q": ""}, headers=a.headers).status_code == 422
    )
    assert client.get("/v1/search", params={"q": "x"}).status_code == 401


def test_query_terms_drop_stopwords_and_punctuation() -> None:
    terms = search.query_terms("我收藏的那篇讲 SwiftUI 的文章是什么？")
    assert {"收藏", "swiftui", "文章"} <= set(terms)
    assert not {"我", "的", "是", "什么", "那"} & set(terms)
    assert len(terms) == len(set(terms))
    assert search.query_terms("c++ & ' |") == ["c"]
    assert search.query_terms("的 是 什么") == []
    text, marks = search.snippet(
        "前文" * 50 + "关键结论在这里" + "后文" * 50, ["关键", "结论"]
    )
    assert text.startswith("前文") and "关键结论" in text
    assert [text[a:b] for a, b in marks] == ["关键", "结论"]
