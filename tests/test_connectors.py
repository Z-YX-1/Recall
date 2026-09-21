"""Connector 必测项（code_standards §4.1：协议、归一化、错误隔离）。"""

from __future__ import annotations

from pathlib import Path

from recall.connectors.base import document_content_hash, normalize_text, slugify
from recall.connectors.obsidian import ObsidianConnector
from tests.helpers import write_note


def test_normalize_text_is_idempotent_and_canonical() -> None:
    raw = "\ufeff标题\r\n\r\n\r\n\r\n正文   \r\n"
    once = normalize_text(raw)
    assert once == "标题\n\n正文"
    assert normalize_text(once) == once


def test_document_content_hash_ignores_line_ending_style() -> None:
    assert document_content_hash("a\r\nb") == document_content_hash("a\nb")


def test_slugify_keeps_cjk_and_kebab_cases_ascii() -> None:
    assert slugify("大模型速成开发MOC") == "大模型速成开发moc"
    assert slugify("RAG 检索质量（笔记）") == "rag-检索质量笔记"
    assert slugify("a__b  c") == "a-b-c"
    assert slugify("   ") != ""  # 空结果回落到短哈希，doc_id 恒非空


def test_list_yields_markdown_with_frontmatter_and_relative_uri(vault: Path) -> None:
    write_note(
        vault,
        "子目录/笔记A.md",
        "---\ntitle: 笔记A\ntags: [rag]\n---\n\n# 笔记A\n\n正文内容\n",
    )
    docs = list(ObsidianConnector(vault).list())

    assert len(docs) == 1
    assert docs[0].doc_id == "笔记a"
    assert docs[0].source_uri == "子目录/笔记A.md"
    assert docs[0].frontmatter == {"title": "笔记A", "tags": ["rag"]}
    assert docs[0].text == "# 笔记A\n\n正文内容"
    assert docs[0].updated_at != ""


def test_list_skips_obsidian_internal_directories(vault: Path) -> None:
    write_note(vault, "笔记.md", "# 笔记\n")
    write_note(vault, ".obsidian/workspace.md", "# 内部\n")
    write_note(vault, ".trash/删掉的.md", "# 垃圾\n")

    assert [doc.source_uri for doc in ObsidianConnector(vault).list()] == ["笔记.md"]


def test_single_bad_document_is_isolated_and_run_continues(vault: Path) -> None:
    write_note(vault, "好的.md", "# 好的\n\n正文\n")
    (vault / "坏的.md").write_bytes(b"\xff\xfe\x00\x00not utf-8")

    connector = ObsidianConnector(vault)
    docs = list(connector.list())

    assert [doc.source_uri for doc in docs] == ["好的.md"]
    errors = connector.drain_errors()
    assert len(errors) == 1
    assert errors[0].source_uri == "坏的.md"
    assert "UnicodeDecodeError" in errors[0].message
    assert connector.drain_errors() == []  # drain 语义：取走即清空


def test_duplicate_stems_get_distinct_stable_ids(vault: Path) -> None:
    write_note(vault, "甲/note.md", "# 甲\n")
    write_note(vault, "乙/note.md", "# 乙\n")
    write_note(vault, "唯一.md", "# 唯一\n")

    first = {doc.source_uri: doc.doc_id for doc in ObsidianConnector(vault).list()}
    second = {doc.source_uri: doc.doc_id for doc in ObsidianConnector(vault).list()}

    assert first == second  # 与枚举顺序无关的确定性
    assert first["唯一.md"] == "唯一"
    assert len({first["甲/note.md"], first["乙/note.md"]}) == 2
    assert first["甲/note.md"].startswith("note-")
    assert first["乙/note.md"].startswith("note-")


def test_missing_vault_records_error_instead_of_raising(tmp_path: Path) -> None:
    connector = ObsidianConnector(tmp_path / "不存在")
    assert list(connector.list()) == []
    errors = connector.drain_errors()
    assert len(errors) == 1
    assert "不存在" in errors[0].message


def test_hash_of_tracks_normalized_content(vault: Path) -> None:
    write_note(vault, "笔记.md", "# 笔记\n\n正文\n")
    connector = ObsidianConnector(vault)
    doc = next(iter(connector.list()))
    assert connector.hash_of(doc) == document_content_hash(doc.text)
