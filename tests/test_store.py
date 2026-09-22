"""Qdrant 适配层集成测试（需要本地 Qdrant；孤儿清理为 code_standards §13 必测项）。"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest

from recall.chunker import count_tokens
from recall.embedder import Embedder
from recall.models import ChunkPayload, chunk_content_hash, chunk_point_id
from recall.store import (
    INTEGER_INDEX_FIELDS,
    KEYWORD_INDEX_FIELDS,
    ChunkPoint,
    CollectionMismatchError,
    QdrantStore,
    collection_name,
    with_retry,
)


def test_collection_name_follows_contract_and_carries_version() -> None:
    assert collection_name("bge-m3", "v1", "md-heading-v1") == "recall__bge-m3@v1__md"
    assert collection_name("bge-m3", "v2", "md-heading-v1") == "recall__bge-m3@v2__md"


@pytest.fixture
async def collection(store: QdrantStore, unique_collection: str) -> AsyncIterator[str]:
    """用例结束后删除临时 collection。"""
    try:
        yield unique_collection
    finally:
        with contextlib.suppress(Exception):  # 清理失败不应掩盖用例结论
            await store.client.delete_collection(unique_collection)


async def _points(embedder: Embedder, doc_id: str, texts: list[str]) -> list[ChunkPoint]:
    """用真实 bge-m3 编码构造待写入的块。"""
    embeddings = await embedder.encode(texts)
    points: list[ChunkPoint] = []
    for index, (text, embedding) in enumerate(zip(texts, embeddings, strict=True)):
        payload = ChunkPayload(
            doc_id=doc_id,
            chunk_index=index,
            heading_path=f"标题 > 小节{index}",
            text=text,
            content_hash=chunk_content_hash(text),
            token_count=count_tokens(text),
            embedding_model=embedder.embedding_model,
            embedding_version=embedder.embedding_version,
            updated_at_ts=1756800000,
        )
        points.append(
            ChunkPoint(
                point_id=chunk_point_id(doc_id, index, payload.content_hash),
                payload=payload,
                embedding=embedding,
            )
        )
    return points


async def test_ensure_collection_writes_contract_metadata_and_indexes(
    store: QdrantStore, collection: str
) -> None:
    created = await store.ensure_collection(
        collection, embedding_model="bge-m3", embedding_version="v1", chunker="md-heading-v1"
    )
    assert created is True

    metadata = await store.collection_metadata(collection)
    assert metadata["embedding_model"] == "bge-m3"
    assert metadata["embedding_version"] == "v1"
    assert metadata["chunker"] == "md-heading-v1"
    assert metadata["created_at"]

    info = await store.client.get_collection(collection)
    vectors = info.config.params.vectors
    sparse_vectors = info.config.params.sparse_vectors
    assert isinstance(vectors, dict) and set(vectors) == {"dense"}
    assert isinstance(sparse_vectors, dict) and set(sparse_vectors) == {"sparse"}
    assert set(info.payload_schema) >= set(KEYWORD_INDEX_FIELDS) | set(INTEGER_INDEX_FIELDS)


async def test_ensure_collection_is_idempotent(store: QdrantStore, collection: str) -> None:
    assert await store.ensure_collection(
        collection, embedding_model="bge-m3", embedding_version="v1", chunker="md-heading-v1"
    )
    assert not await store.ensure_collection(
        collection, embedding_model="bge-m3", embedding_version="v1", chunker="md-heading-v1"
    )


async def test_ensure_collection_rejects_mixed_embedding_version(
    store: QdrantStore, collection: str
) -> None:
    await store.ensure_collection(
        collection, embedding_model="bge-m3", embedding_version="v1", chunker="md-heading-v1"
    )
    with pytest.raises(CollectionMismatchError):
        await store.ensure_collection(
            collection, embedding_model="bge-m3", embedding_version="v2", chunker="md-heading-v1"
        )


async def test_upsert_is_idempotent_and_orphan_cleanup_removes_stale_points(
    store: QdrantStore, collection: str, embedder: Embedder
) -> None:
    await store.ensure_collection(
        collection, embedding_model="bge-m3", embedding_version="v1", chunker="md-heading-v1"
    )
    points = await _points(embedder, "doc-a", ["第一块内容", "第二块内容", "第三块内容"])

    await store.upsert_chunks(collection, points)
    assert await store.count_points(collection, "doc-a") == 3

    # 同输入重跑：内容寻址 id 不变 ⇒ 原地覆盖，不产生新点
    await store.upsert_chunks(collection, points)
    assert await store.count_points(collection, "doc-a") == 3

    keep = {point.point_id for point in points[:2]}
    assert await store.delete_orphans(collection, "doc-a", keep) == 1
    assert await store.count_points(collection, "doc-a") == 2
    assert await store.delete_orphans(collection, "doc-a", keep) == 0  # 再跑一次无孤儿


async def test_delete_document_removes_every_point(
    store: QdrantStore, collection: str, embedder: Embedder
) -> None:
    await store.ensure_collection(
        collection, embedding_model="bge-m3", embedding_version="v1", chunker="md-heading-v1"
    )
    points = await _points(embedder, "doc-b", ["内容一", "内容二"])
    await store.upsert_chunks(collection, points)

    assert await store.delete_document(collection, "doc-b") == 2
    assert await store.count_points(collection, "doc-b") == 0


async def test_ping_reports_unreachable_service() -> None:
    """Qdrant 不可达时 ``ping()`` 返回 False 而不是抛错（``/health`` 降级路径）。"""
    store = QdrantStore("http://127.0.0.1:1", timeout=2)
    try:
        assert await store.ping() is False
    finally:
        await store.close()


async def test_with_retry_succeeds_after_transient_failures() -> None:
    """所有 Qdrant 调用都走的重试原语：瞬时失败要能重试成功（code_standards §10）。"""
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("瞬时故障")
        return "ok"

    assert await with_retry("probe", flaky, attempts=3, base_delay=0.0) == "ok"
    assert attempts == 3


async def test_with_retry_raises_last_error_after_exhausting_attempts() -> None:
    attempts = 0

    async def always_fail() -> None:
        nonlocal attempts
        attempts += 1
        raise ValueError("一直失败")

    with pytest.raises(ValueError, match="一直失败"):
        await with_retry("probe", always_fail, attempts=2, base_delay=0.0)
    assert attempts == 2
