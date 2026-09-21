"""契约与 id 规则的必测项（code_standards §3.1、§13）。

覆盖：``chunk_point_id`` 确定性、payload 值类型约束、默认值不共享、必填字段校验。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from recall.models import (
    AnswerResult,
    ChunkPayload,
    DocRecord,
    Evidence,
    Identity,
    RawDoc,
    SearchResult,
    chunk_content_hash,
    chunk_point_id,
)

CONTENT_HASH = "a" * 64


def _payload(**overrides: Any) -> ChunkPayload:
    data: dict[str, Any] = {
        "doc_id": "doc-a",
        "chunk_index": 0,
        "heading_path": "标题 > 小节",
        "text": "块原文",
        "content_hash": CONTENT_HASH,
        "token_count": 12,
        "embedding_model": "bge-m3",
        "embedding_version": "v1",
        "updated_at_ts": 1756800000,
    }
    data.update(overrides)
    return ChunkPayload.model_validate(data)


def test_chunk_content_hash_matches_sha256() -> None:
    text = "大模型速成开发MOC"
    assert chunk_content_hash(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_chunk_point_id_is_deterministic_and_valid_uuid() -> None:
    first = chunk_point_id("doc-a", 3, CONTENT_HASH)
    second = chunk_point_id("doc-a", 3, CONTENT_HASH)
    assert first == second
    parsed = uuid.UUID(first)
    assert parsed.version == 5  # uuid5 ⇒ 合法 Qdrant PointId（code_standards §3.1）


def test_chunk_point_id_changes_with_position_or_content() -> None:
    base = chunk_point_id("doc-a", 3, CONTENT_HASH)
    assert base != chunk_point_id("doc-a", 4, CONTENT_HASH)
    assert base != chunk_point_id("doc-a", 3, "b" * 64)
    assert base != chunk_point_id("doc-b", 3, CONTENT_HASH)


def test_chunk_payload_defaults_are_permission_placeholders() -> None:
    payload = _payload()
    assert payload.owner == "me"
    assert payload.visibility == "private"
    assert payload.groups == []


def test_chunk_payload_groups_default_is_not_shared_between_instances() -> None:
    first = _payload()
    second = _payload()
    first.groups.append("admin")
    assert second.groups == []


def test_chunk_payload_requires_every_contract_field() -> None:
    with pytest.raises(ValidationError):
        ChunkPayload.model_validate({"doc_id": "doc-a", "chunk_index": 0})


def test_chunk_payload_dump_matches_qdrant_value_type_constraints() -> None:
    dumped = _payload().model_dump()
    assert set(dumped) == {
        "doc_id",
        "chunk_index",
        "heading_path",
        "text",
        "content_hash",
        "token_count",
        "embedding_model",
        "embedding_version",
        "owner",
        "visibility",
        "groups",
        "updated_at_ts",
    }
    assert isinstance(dumped["updated_at_ts"], int)  # range 索引必须是 integer
    assert isinstance(dumped["groups"], list)
    assert all(isinstance(item, str) for item in dumped["groups"])


def test_chunk_payload_rejects_non_integer_timestamp() -> None:
    with pytest.raises(ValidationError):
        _payload(updated_at_ts="2026-09-04T00:00:00")


def test_identity_defaults_follow_s1_hardcoding() -> None:
    identity = Identity()
    assert identity.user == "me"
    assert identity.groups == ["owner"]


def test_raw_doc_updated_at_is_optional() -> None:
    doc = RawDoc(doc_id="d", source_uri="a.md", text="正文")
    assert doc.updated_at == ""
    assert doc.frontmatter == {}


def test_search_result_and_evidence_roundtrip() -> None:
    result = SearchResult(
        evidence=[
            Evidence(
                ref_id="1",
                source_uri="a.md",
                heading_path="标题",
                text="片段",
                score=0.5,
            )
        ],
        references=[{"ref_id": "1", "source_uri": "a.md"}],
    )
    restored = SearchResult.model_validate_json(result.model_dump_json())
    assert restored == result


def test_answer_result_citations_are_ints() -> None:
    answer = AnswerResult(answer="结论[1]", citations=[1], references=[])
    assert answer.citations == [1]


def test_doc_record_error_defaults_to_none() -> None:
    record = DocRecord(
        doc_id="d",
        source_type="obsidian",
        source_uri="a.md",
        title="A",
        content_hash=CONTENT_HASH,
        updated_at="2026-09-04T00:00:00+08:00",
        indexed_at="2026-09-04T00:00:00+08:00",
    )
    assert record.error is None
    assert record.chunk_count == 0
    assert record.owner == "me"
    assert record.visibility == "private"
