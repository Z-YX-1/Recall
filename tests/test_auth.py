"""身份与权限必测项（code_standards §7 与 §13：``effective_filter`` 只收窄）。

结构断言保证「合取而非并集」，集成断言用真实 Qdrant 证明**放宽的过滤条件拿不到
别人私有的块**——这是权限红线，必须端到端验证。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from qdrant_client import models

from recall.auth import (
    InvalidFilterError,
    build_scope_filter,
    effective_filter,
    get_identity,
    intersect,
)
from recall.chunker import count_tokens
from recall.embedder import Embedder
from recall.models import ChunkPayload, Identity, chunk_content_hash, chunk_point_id
from recall.store import ChunkPoint, QdrantStore


def _field_keys(conditions: object) -> list[str]:
    """取出过滤条件里的字段名（``Filter.should`` 是条件联合类型，需先窄化）。"""
    if conditions is None or isinstance(conditions, (models.FieldCondition, models.Filter)):
        return []
    if not isinstance(conditions, Sequence):
        return []
    return [item.key for item in conditions if isinstance(item, models.FieldCondition)]


def _payload(doc_id: str, *, owner: str, visibility: str, text: str) -> ChunkPayload:
    return ChunkPayload(
        doc_id=doc_id,
        chunk_index=0,
        heading_path="标题",
        text=text,
        content_hash=chunk_content_hash(text),
        token_count=count_tokens(text),
        embedding_model="bge-m3",
        embedding_version="v1",
        owner=owner,
        visibility=visibility,
        groups=[],
        updated_at_ts=1756800000,
    )


def test_get_identity_is_s1_hardcoded() -> None:
    assert get_identity() == Identity(user="me", groups=["owner"])


def test_scope_filter_is_a_disjunction_of_owner_public_and_groups() -> None:
    scope = build_scope_filter(Identity())
    assert _field_keys(scope.should) == ["owner", "visibility", "groups"]
    assert not scope.must and not scope.must_not


def test_scope_filter_omits_groups_when_identity_has_none() -> None:
    scope = build_scope_filter(Identity(user="me", groups=[]))
    assert _field_keys(scope.should) == ["owner", "visibility"]


def test_effective_filter_without_client_filter_returns_scope_only() -> None:
    identity = Identity()
    assert effective_filter(identity, None) == build_scope_filter(identity)
    assert effective_filter(identity, {}) == build_scope_filter(identity)


def test_effective_filter_is_conjunction_never_union() -> None:
    """关键语义：客户端 filter 只能收窄，绝不能把可见范围并大。"""
    identity = Identity()
    scope = build_scope_filter(identity)
    client = models.Filter(
        must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value="doc-a"))]
    )
    merged = effective_filter(identity, client.model_dump())
    assert merged == intersect(scope, client)
    must = merged.must
    assert isinstance(must, list) and len(must) == 2  # scope AND client，不是并集
    assert not merged.should


def test_effective_filter_rejects_malformed_client_filter() -> None:
    with pytest.raises(InvalidFilterError):
        effective_filter(Identity(), {"must": [{"key": "doc_id"}]})  # 缺 match


def test_effective_filter_rejects_unknown_filter_field() -> None:
    with pytest.raises(InvalidFilterError):
        effective_filter(Identity(), {"filtre": []})  # 拼错的字段名


def test_effective_filter_accepts_nested_and_typed_conditions() -> None:
    merged = effective_filter(
        Identity(),
        {
            "must": [
                {"key": "doc_id", "match": {"value": "doc-a"}},
                {"key": "updated_at_ts", "range": {"gte": 1756800000}},
            ],
            "must_not": [{"key": "visibility", "match": {"value": "public"}}],
        },
    )
    must = merged.must
    assert isinstance(must, list) and len(must) == 2


async def test_client_filter_cannot_widen_visibility(
    store: QdrantStore, embedder: Embedder, unique_collection: str
) -> None:
    """端到端权限红线：客户端显式索要别人的私有块，必须一条都拿不到。"""
    await store.ensure_collection(
        unique_collection, embedding_model="bge-m3", embedding_version="v1", chunker="md-heading-v1"
    )
    specs = [
        ("mine", "me", "private", "我的私有笔记内容"),
        ("other-private", "someone-else", "private", "别人的私有笔记内容"),
        ("other-public", "someone-else", "public", "别人的公开笔记内容"),
    ]
    points: list[ChunkPoint] = []
    for doc_id, owner, visibility, text in specs:
        payload = _payload(doc_id, owner=owner, visibility=visibility, text=text)
        embedding = (await embedder.encode([text]))[0]
        points.append(
            ChunkPoint(
                point_id=chunk_point_id(doc_id, 0, payload.content_hash),
                payload=payload,
                embedding=embedding,
            )
        )
    await store.upsert_chunks(unique_collection, points)
    try:
        embedding = (await embedder.encode(["笔记内容"]))[0]
        client_filter = {"must": [{"key": "owner", "match": {"value": "someone-else"}}]}
        hits = await store.hybrid_search(
            unique_collection,
            dense=embedding.dense,
            sparse_indices=embedding.sparse_indices,
            sparse_values=embedding.sparse_values,
            top_k=10,
            query_filter=effective_filter(Identity(), client_filter),
        )
        found = {hit.payload["doc_id"] for hit in hits if hit.payload is not None}
        assert found == {"other-public"}  # 自己的 + 别人的公开可见；别人的私有一条不给
    finally:
        await store.client.delete_collection(unique_collection)
