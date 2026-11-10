from itertools import pairwise

from app.chunking import MAX, Material, chunk_material, spans
from app.tokenizer import index_text, tokenize


def long_article() -> str:
    paragraphs = []
    for number in range(12):
        sentences = [
            f"第{number}段第{index}句讲的是 SwiftUI 状态管理的细节。"
            for index in range(8)
        ]
        paragraphs.append("".join(sentences))
    return "\n\n".join(paragraphs)


def test_short_thought_is_one_chunk_with_context_header() -> None:
    material = Material(
        type="thought", text="  一条想法。\n第二句  ", note="备注里有分类词"
    )
    [chunk] = chunk_material(material)
    assert chunk.display_text == "一条想法。\n第二句"
    assert material.text[chunk.start : chunk.end] == chunk.display_text
    assert chunk.index_text == "备注里有分类词\n一条想法。\n第二句"
    assert chunk.source == "text"


def test_long_text_splits_with_overlap_and_exact_offsets() -> None:
    body = long_article()
    material = Material(
        type="url",
        text="",
        note="",
        source_url="https://example.com/a",
        client_extract={"title": "状态管理指南", "text": body},
    )
    chunks = chunk_material(material)
    assert len(chunks) > 3
    for chunk in chunks:
        assert chunk.source == "extract"
        assert body[chunk.start : chunk.end] == chunk.display_text
        assert len(chunk.display_text) <= MAX
        assert chunk.index_text == f"状态管理指南\n{chunk.display_text}"
        assert "状态管理指南" not in chunk.display_text
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    # Consecutive chunks overlap by at most one sentence and cover everything.
    for previous, current in pairwise(chunks):
        assert current.start <= previous.end
        assert previous.end - current.start <= 200
    assert chunks[0].start == 0 and chunks[-1].end == len(body)


def test_unbroken_text_is_hard_split() -> None:
    body = "字" * 2000
    assert all(end - start <= MAX for start, end in spans(body))
    assert spans(body)[-1][1] == 2000


def test_image_uses_ocr_and_link_without_body_uses_metadata() -> None:
    image = Material(
        type="image", text="", note="会议截图", client_ocr={"text": "白板上的结论"}
    )
    [chunk] = chunk_material(image)
    assert (chunk.source, chunk.display_text) == ("ocr", "白板上的结论")
    assert chunk.index_text == "会议截图\n白板上的结论"

    link = Material(
        type="url",
        text="",
        note="稍后读",
        source_url="https://example.com/post",
        client_extract={"title": "一篇文章"},
    )
    [meta] = chunk_material(link)
    assert meta.source == "meta"
    assert meta.display_text == "一篇文章\n稍后读\nhttps://example.com/post"

    assert chunk_material(Material(type="image", text="", note="")) == []


def test_tokenizer_handles_mixed_chinese_and_english() -> None:
    tokens = tokenize("SwiftUI 状态管理，用 @Observable 宏！")
    assert {"swiftui", "状态", "管理", "observable", "宏"} <= set(tokens)
    assert "，" not in tokens and "@" not in tokens
    assert index_text("中华人民共和国").startswith("中华")
