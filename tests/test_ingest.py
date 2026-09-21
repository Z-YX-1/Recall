"""摄取管道端到端 smoke test（code_standards §13：幂等、孤儿清理、端到端）。

用固定小 corpus 真跑一遍「扫描 → 切分 → bge-m3 编码 → Qdrant upsert → registry 记账」，
不使用 mock 冒充端到端。需要本地 Qdrant + GPU 模型缓存。
"""

from __future__ import annotations

from ingest import run_ingest
from tests.helpers import IngestEnv, ingest_args, write_note


async def test_ingest_run_is_idempotent(ingest_env: IngestEnv) -> None:
    write_note(ingest_env.vault, "甲.md", "# 甲\n\n## 一\n\n甲一正文\n\n## 二\n\n甲二正文\n")
    write_note(ingest_env.vault, "乙.md", "# 乙\n\n乙正文\n")

    first = await run_ingest(ingest_args(ingest_env))
    assert first.scanned == 2
    assert first.indexed_docs == 2
    assert first.indexed_chunks == 3
    assert first.failed == []

    ids_before = await ingest_env.store.scroll_doc_ids(ingest_env.collection, "甲")

    second = await run_ingest(ingest_args(ingest_env))
    assert second.skipped == 2
    assert second.indexed_docs == 0
    assert second.indexed_chunks == 0

    assert await ingest_env.store.scroll_doc_ids(ingest_env.collection, "甲") == ids_before
    assert await ingest_env.store.count_points(ingest_env.collection) == first.indexed_chunks


async def test_rebuild_reprocesses_every_document(ingest_env: IngestEnv) -> None:
    write_note(ingest_env.vault, "甲.md", "# 甲\n\n正文\n")
    await run_ingest(ingest_args(ingest_env))

    report = await run_ingest(ingest_args(ingest_env, mode="rebuild"))
    assert report.skipped == 0
    assert report.indexed_docs == 1
    assert await ingest_env.store.count_points(ingest_env.collection, "甲") == 1


async def test_edit_only_replaces_changed_chunks(ingest_env: IngestEnv) -> None:
    """标题主切让编辑局部化（tech.md §5）：只改一节 ⇒ 只有该节的块换 id。"""
    write_note(ingest_env.vault, "甲.md", "# 甲\n\n## 一\n\n甲一正文\n\n## 二\n\n甲二正文\n")
    first = await run_ingest(ingest_args(ingest_env))
    assert first.indexed_chunks == 2
    ids_before = await ingest_env.store.scroll_doc_ids(ingest_env.collection, "甲")

    write_note(
        ingest_env.vault, "甲.md", "# 甲\n\n## 一\n\n甲一正文\n\n## 二\n\n甲二正文（改过）\n"
    )
    second = await run_ingest(ingest_args(ingest_env))

    assert second.indexed_docs == 1
    assert second.orphans_deleted == 1  # 第一节块文本未变 ⇒ id 不变，只有第二节块成孤儿
    ids_after = await ingest_env.store.scroll_doc_ids(ingest_env.collection, "甲")
    assert len(ids_before & ids_after) == 1
    assert await ingest_env.store.count_points(ingest_env.collection, "甲") == 2


async def test_shrinking_document_deletes_every_stale_chunk(ingest_env: IngestEnv) -> None:
    write_note(ingest_env.vault, "甲.md", "# 甲\n\n## 一\n\n甲一正文\n\n## 二\n\n甲二正文\n")
    first = await run_ingest(ingest_args(ingest_env))
    assert first.indexed_chunks == 2

    write_note(ingest_env.vault, "甲.md", "# 甲\n\n只剩一节了\n")
    second = await run_ingest(ingest_args(ingest_env))

    assert second.indexed_docs == 1
    assert second.orphans_deleted == 2  # 剩下的一节换了文本 ⇒ 两个旧块全部成为孤儿
    assert await ingest_env.store.count_points(ingest_env.collection, "甲") == 1


async def test_deleted_source_is_reconciled(ingest_env: IngestEnv) -> None:
    write_note(ingest_env.vault, "甲.md", "# 甲\n\n正文\n")
    write_note(ingest_env.vault, "乙.md", "# 乙\n\n正文\n")
    await run_ingest(ingest_args(ingest_env))

    (ingest_env.vault / "乙.md").unlink()
    report = await run_ingest(ingest_args(ingest_env))

    assert report.deleted_docs == 1
    assert await ingest_env.store.count_points(ingest_env.collection, "乙") == 0
    assert await ingest_env.store.count_points(ingest_env.collection, "甲") == 1


async def test_broken_document_does_not_abort_run(ingest_env: IngestEnv) -> None:
    write_note(ingest_env.vault, "好的.md", "# 好的\n\n正文\n")
    (ingest_env.vault / "坏的.md").write_bytes(b"\xff\xfe\x00bad")

    report = await run_ingest(ingest_args(ingest_env))

    assert report.indexed_docs == 1
    assert len(report.failed) == 1
    assert "坏的.md" in report.failed[0]
    assert await ingest_env.store.count_points(ingest_env.collection, "好的") == 1


async def test_broken_document_keeps_previously_indexed_chunks(ingest_env: IngestEnv) -> None:
    write_note(ingest_env.vault, "甲.md", "# 甲\n\n正文\n")
    await run_ingest(ingest_args(ingest_env))

    (ingest_env.vault / "甲.md").write_bytes(b"\xff\xfe\x00bad")
    report = await run_ingest(ingest_args(ingest_env))

    assert len(report.failed) == 1
    assert report.deleted_docs == 0  # 解析失败 ≠ 源已删除，旧块必须留着
    assert await ingest_env.store.count_points(ingest_env.collection, "甲") == 1
