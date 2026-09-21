"""切分器必测项（code_standards §13：切分确定性）。

覆盖：标题主切、深层标题不切分、导航小节不合并不重切、递归兜底 + overlap、
内容零丢失、同输入同输出。
"""

from __future__ import annotations

from itertools import pairwise

from recall.chunker import (
    MAX_CHUNK_TOKENS,
    OVERLAP_CHARS,
    SEPARATORS,
    _split_oversized,  # noqa: PLC2701 - 内容零丢失是关键不变量，直接验证切分内核
    chunk_markdown,
    count_tokens,
)

_PARAGRAPH = (
    "混合检索把 dense 与 sparse 两路召回结果用 RRF 融合，再交给重排模型精排；"
    "这套三件套是当前生产环境的基线做法，重排只吃召回候选的前二十条。"
)


def _oversized_note(paragraphs: int = 40) -> str:
    body = "\n\n".join(_PARAGRAPH for _ in range(paragraphs))
    return f"# 大模型速成开发MOC\n\n## 超长小节\n\n{body}\n"


def test_chunk_markdown_is_deterministic() -> None:
    text = _oversized_note()
    assert chunk_markdown(text) == chunk_markdown(text)


def test_heading_split_uses_h1_and_h2_as_boundaries() -> None:
    text = "# 标题\n\n引言\n\n## 甲\n\n甲正文\n\n### 甲一\n\n甲一正文\n\n## 乙\n\n乙正文\n"
    chunks = chunk_markdown(text)
    assert [chunk.heading_path for chunk in chunks] == ["标题", "标题 > 甲", "标题 > 乙"]


def test_deep_heading_stays_inside_its_section() -> None:
    text = "# 标题\n\n## 甲\n\n甲正文\n\n### 甲一\n\n甲一正文\n"
    chunks = chunk_markdown(text)
    assert len(chunks) == 1
    assert "### 甲一" in chunks[0].text
    assert "甲一正文" in chunks[0].text


def test_navigation_section_is_neither_merged_nor_resplit() -> None:
    text = "# MOC\n\n这是索引页。\n\n## 导航\n\n- [[甲]]\n- [[乙]]\n"
    chunks = chunk_markdown(text)
    assert [chunk.heading_path for chunk in chunks] == ["MOC", "MOC > 导航"]
    nav = chunks[1]
    assert nav.sub_index == 0
    assert nav.token_count < 100  # 导航类小节阈值（tech.md §2）


def test_empty_or_heading_only_document_yields_no_chunks() -> None:
    assert chunk_markdown("") == []
    assert chunk_markdown("# 只有标题\n\n## 也只有标题\n") == []


def test_heading_inside_code_fence_is_not_a_boundary() -> None:
    text = "# 标题\n\n## 甲\n\n```python\n# 这不是标题\nprint(1)\n```\n"
    chunks = chunk_markdown(text)
    assert len(chunks) == 1
    assert "# 这不是标题" in chunks[0].text


def test_oversized_section_is_split_with_consecutive_sub_indexes() -> None:
    chunks = chunk_markdown(_oversized_note())
    assert len(chunks) > 1
    assert {chunk.heading_path for chunk in chunks} == {"大模型速成开发MOC > 超长小节"}
    assert [chunk.sub_index for chunk in chunks] == list(range(len(chunks)))


def test_oversized_section_chunks_respect_token_budget() -> None:
    chunks = chunk_markdown(_oversized_note())
    # overlap（≈100 字符）会让块略超 MAX_CHUNK_TOKENS，留出 200 token 余量
    assert all(chunk.token_count <= MAX_CHUNK_TOKENS + 200 for chunk in chunks)
    # 且块确实被切小了：单块不应接近整节体量
    assert all(chunk.token_count < MAX_CHUNK_TOKENS + 200 for chunk in chunks)


def test_recursive_split_adds_overlap_between_neighbours() -> None:
    chunks = chunk_markdown(_oversized_note())
    for previous, current in pairwise(chunks):
        visible_tail = previous.text.rsplit("\n", 1)[-1][-40:]
        assert visible_tail
        assert visible_tail in current.text


def test_recursive_split_loses_no_content() -> None:
    body = "\n\n".join(_PARAGRAPH for _ in range(40))
    pieces = _split_oversized(body, MAX_CHUNK_TOKENS, SEPARATORS)
    assert "".join(pieces) == body


def test_count_tokens_is_stable_and_monotonic() -> None:
    assert count_tokens("") == 0
    assert count_tokens(_PARAGRAPH) == count_tokens(_PARAGRAPH)
    assert count_tokens(_PARAGRAPH * 2) > count_tokens(_PARAGRAPH)
    assert OVERLAP_CHARS == 100
