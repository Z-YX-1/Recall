"""注册表必测项（code_standards §13：registry 读写、错误隔离）。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from recall.models import DocRecord
from recall.registry import Registry

CONTENT_HASH = "c" * 64


def _registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "nested" / "registry.db")


def _record(**overrides: Any) -> DocRecord:
    data: dict[str, Any] = {
        "doc_id": "doc-a",
        "source_type": "obsidian",
        "source_uri": "笔记A.md",
        "title": "笔记A",
        "frontmatter": {"tags": ["rag"], "title": "笔记A"},
        "content_hash": CONTENT_HASH,
        "updated_at": "2026-09-04T10:00:00+08:00",
        "indexed_at": "2026-09-04T10:00:01+08:00",
        "chunk_count": 3,
    }
    data.update(overrides)
    return DocRecord.model_validate(data)


async def test_initialize_creates_parent_dir_and_is_idempotent(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    await registry.initialize()
    await registry.initialize()
    assert registry.db_path.is_file()
    assert await registry.count() == 0


async def test_upsert_then_get_roundtrip(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    await registry.initialize()
    await registry.upsert(_record())

    restored = await registry.get("doc-a")
    assert restored is not None
    assert restored.doc_id == "doc-a"
    assert restored.frontmatter == {"tags": ["rag"], "title": "笔记A"}
    assert restored.chunk_count == 3
    assert restored.error is None


async def test_get_returns_none_for_unknown_doc(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    await registry.initialize()
    assert await registry.get("missing") is None


async def test_upsert_overwrites_existing_row(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    await registry.initialize()
    await registry.upsert(_record())
    await registry.upsert(_record(content_hash="d" * 64, chunk_count=7))

    assert await registry.count() == 1
    restored = await registry.get("doc-a")
    assert restored is not None
    assert restored.content_hash == "d" * 64
    assert restored.chunk_count == 7


async def test_frontmatter_with_yaml_date_is_serialisable(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    await registry.initialize()
    await registry.upsert(_record(frontmatter={"created": date(2026, 9, 4)}))

    restored = await registry.get("doc-a")
    assert restored is not None
    assert restored.frontmatter == {"created": "2026-09-04"}


async def test_record_error_preserves_indexed_fields(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    await registry.initialize()
    await registry.upsert(_record())
    await registry.record_error("doc-a", "obsidian", "笔记A.md", "YAMLError: bad frontmatter")

    restored = await registry.get("doc-a")
    assert restored is not None
    assert restored.error is not None and "YAMLError" in restored.error
    assert restored.content_hash == CONTENT_HASH  # 旧账本不因失败被清空
    assert restored.chunk_count == 3


async def test_record_error_creates_row_for_new_doc(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    await registry.initialize()
    await registry.record_error("doc-b", "obsidian", "坏的.md", "UnicodeDecodeError")

    restored = await registry.get("doc-b")
    assert restored is not None
    assert restored.source_uri == "坏的.md"
    assert restored.chunk_count == 0
    assert restored.error == "UnicodeDecodeError"


async def test_list_all_is_sorted_and_delete_removes_row(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    await registry.initialize()
    await registry.upsert(_record(doc_id="doc-b"))
    await registry.upsert(_record(doc_id="doc-a"))

    assert [record.doc_id for record in await registry.list_all()] == ["doc-a", "doc-b"]

    await registry.delete("doc-a")
    assert [record.doc_id for record in await registry.list_all()] == ["doc-b"]
    assert await registry.count() == 1
