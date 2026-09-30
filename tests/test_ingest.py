"""摄取管道端到端 smoke test（code_standards §13：幂等、孤儿清理、端到端）。

用固定小 corpus 真跑一遍「扫描 → 切分 → bge-m3 编码 → Qdrant upsert → registry 记账」，
不使用 mock 冒充端到端。需要本地 Qdrant + GPU 模型缓存。
"""

from __future__ import annotations

import pytest
from qdrant_client import models

from ingest import run_ingest
from recall.models import DocRecord
from recall.registry import Registry
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


async def test_rebuild_into_new_collection_keeps_the_old_one(ingest_env: IngestEnv) -> None:
    """换 embedding 版本并排重灌：新库全量写入，**旧库不删**（tech.md §3.1）。"""
    write_note(ingest_env.vault, "甲.md", "# 甲\n\n正文\n")
    await run_ingest(ingest_args(ingest_env))
    assert await ingest_env.store.count_points(ingest_env.collection, "甲") == 1

    fresh = f"{ingest_env.collection}-v2"
    try:
        report = await run_ingest(
            ingest_args(ingest_env, mode="rebuild", extra=["--collection", fresh])
        )
        assert report.skipped == 0  # 目标库为空 ⇒ 全量重灌
        assert report.indexed_docs == 1
        assert await ingest_env.store.count_points(fresh, "甲") == 1
        # 旧库原样保留，供评测与回滚
        assert await ingest_env.store.count_points(ingest_env.collection, "甲") == 1
    finally:
        await ingest_env.store.client.delete_collection(fresh)


async def test_rebuild_resumes_without_reembedding(ingest_env: IngestEnv) -> None:
    """断点续传：目标库已与本次切分一致 ⇒ 跳过；缺块时只补该文档（tech.md §5）。"""
    write_note(ingest_env.vault, "甲.md", "# 甲\n\n正文\n")
    write_note(ingest_env.vault, "乙.md", "# 乙\n\n正文\n")
    await run_ingest(ingest_args(ingest_env))

    resumed = await run_ingest(ingest_args(ingest_env, mode="rebuild"))
    assert resumed.skipped == 2
    assert resumed.indexed_docs == 0
    assert resumed.indexed_chunks == 0

    # 模拟"上次写到一半就中断"：把甲的点全删掉
    ids = await ingest_env.store.scroll_doc_ids(ingest_env.collection, "甲")
    await ingest_env.store.client.delete(
        ingest_env.collection,
        points_selector=models.PointIdsList(points=sorted(ids)),
        wait=True,
    )

    repaired = await run_ingest(ingest_args(ingest_env, mode="rebuild"))
    assert repaired.indexed_docs == 1  # 只补甲
    assert repaired.skipped == 1  # 乙原样跳过
    assert await ingest_env.store.count_points(ingest_env.collection, "甲") == 1


async def test_force_reembeds_even_when_target_matches(ingest_env: IngestEnv) -> None:
    write_note(ingest_env.vault, "甲.md", "# 甲\n\n正文\n")
    await run_ingest(ingest_args(ingest_env))

    forced = await run_ingest(ingest_args(ingest_env, extra=["--force"]))
    assert forced.skipped == 0
    assert forced.indexed_docs == 1
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


# ---------------------------------------------------------------------------------------
# 多来源账本隔离（roadmap R-41c，2026-09-30）
# ---------------------------------------------------------------------------------------


def _foreign_record() -> DocRecord:
    """构造一条"另一个来源"的账本记录（无需真有那个 Connector）。"""
    return DocRecord(
        doc_id="feishu-foreign-token",
        source_type="feishu",
        source_uri="https://example.feishu.cn/docx/foreign",
        title="另一来源的文档",
        content_hash="0" * 64,
        updated_at="2026-09-30T00:00:00+08:00",
        indexed_at="2026-09-30T00:00:00+08:00",
        chunk_count=3,
    )


async def test_run_does_not_delete_documents_of_other_sources(ingest_env: IngestEnv) -> None:
    """🔴 R-41c 回归守门：跑单一来源的 run **不得**删除其它来源的文档。

    背景（2026-09-30 复查发现的数据事故级缺陷）：``_reconcile_deleted`` 原先对
    ``registry.list_all()`` **全量**对账，凡未在本次 run 枚举到的 ``doc_id`` 一律当
    "源侧已删"删掉（点 + 账本行）。一旦接入第二个来源（飞书），跑一次
    ``--source feishu`` 就会把 Obsidian 的 65 篇笔记**全部删除**。
    修法：对账范围按 ``source_type`` 收窄。

    本用例往账本里塞一条"另一个来源"的记录，再跑一次 Obsidian 摄取，断言它完好无损。
    """
    registry = Registry(ingest_env.registry_db)
    await registry.initialize()
    await registry.upsert(_foreign_record())

    write_note(ingest_env.vault, "甲.md", "# 甲\n\n正文\n")
    report = await run_ingest(ingest_args(ingest_env))

    assert report.indexed_docs == 1
    assert report.deleted_docs == 0, "其它来源的文档被误删了（R-41c 回归）"
    assert await registry.get("feishu-foreign-token") is not None
    assert await registry.count() == 2  # 甲 + 那条外来记录


async def test_reconcile_still_removes_deleted_docs_of_the_same_source(
    ingest_env: IngestEnv,
) -> None:
    """同一来源内，源侧已删的文档**仍要**清理。

    这条是上一条的对照组：防止"修 R-41c 时顺手把对账整个关掉"（那会留下永不过期的幽灵文档）。
    """
    registry = Registry(ingest_env.registry_db)
    await registry.initialize()
    await registry.upsert(_foreign_record())

    write_note(ingest_env.vault, "甲.md", "# 甲\n\n正文\n")
    write_note(ingest_env.vault, "乙.md", "# 乙\n\n正文\n")
    await run_ingest(ingest_args(ingest_env))

    (ingest_env.vault / "乙.md").unlink()
    report = await run_ingest(ingest_args(ingest_env))

    assert report.scanned == 1
    assert report.deleted_docs == 1  # 只删同来源的"乙"，不碰外来记录
    assert await registry.get("乙") is None
    assert await registry.get("feishu-foreign-token") is not None
    assert await ingest_env.store.count_points(ingest_env.collection, "乙") == 0
    assert await ingest_env.store.count_points(ingest_env.collection, "甲") == 1


async def test_unknown_source_fails_loudly(ingest_env: IngestEnv) -> None:
    """未知来源必须**大声失败**：静默跳过会让人以为"这一来源已经跑过了"。"""
    write_note(ingest_env.vault, "甲.md", "# 甲\n\n正文\n")

    with pytest.raises(SystemExit, match="未支持的 source_type"):
        await run_ingest(ingest_args(ingest_env, extra=["--source", "no-such-source"]))
