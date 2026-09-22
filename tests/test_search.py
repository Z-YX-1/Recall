"""kb_search 端到端与 REST 契约（tech.md §4 链路、§8 端点；roadmap R-22、R-23）。

真跑「query → 编码 → 双路检索 RRF → 精排 → 预算截断 → 证据包」，并验证错误信封。
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest

from ingest import run_ingest
from recall.api import ApiError, Service, app, kb_answer_core, kb_search_core
from recall.models import AnswerRequest, SearchRequest
from recall.rerank import DEFAULT_TOP_N, Reranker, RerankHit
from tests.helpers import IngestEnv, ingest_args, write_note

_RAG_NOTE = """\
# RAG 检索

## 混合检索

混合检索把 dense 与 sparse 两路召回结果用 RRF 融合，再交给 bge-reranker-v2-m3 精排；
这套三件套是当前生产环境的基线做法。

## 预算控制

证据包按分数贪心取块，累计 token 不超过 max_tokens，默认三千。
"""

_COOKING_NOTE = """\
# 家常菜

## 红烧肉

五花肉切块冷水下锅焯水，加冰糖炒糖色，小火慢炖四十分钟收汁。
"""


async def _prepare_corpus(ingest_env: IngestEnv) -> None:
    write_note(ingest_env.vault, "RAG检索.md", _RAG_NOTE)
    write_note(ingest_env.vault, "家常菜.md", _COOKING_NOTE)
    report = await run_ingest(ingest_args(ingest_env))
    assert report.indexed_docs == 2
    assert report.failed == []


async def test_kb_search_returns_evidence_traced_to_the_right_document(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)

    result = await kb_search_core(SearchRequest(query="混合检索怎么融合两路召回结果？", top_k=3))

    assert result.evidence
    assert result.evidence[0].source_uri == "RAG检索.md"
    assert "RRF" in result.evidence[0].text
    assert result.evidence[0].score > 0.0


async def test_kb_search_ref_ids_and_references_are_one_to_one(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)

    result = await kb_search_core(SearchRequest(query="红烧肉怎么做", top_k=5))

    assert [item.ref_id for item in result.evidence] == [
        str(index) for index in range(1, len(result.evidence) + 1)
    ]
    assert [ref["ref_id"] for ref in result.references] == [item.ref_id for item in result.evidence]
    assert all(ref["source_uri"] for ref in result.references)


async def test_kb_search_respects_max_tokens_budget(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)

    tiny = await kb_search_core(SearchRequest(query="检索与预算", max_tokens=1, top_k=5))
    wide = await kb_search_core(SearchRequest(query="检索与预算", max_tokens=3000, top_k=5))

    assert len(tiny.evidence) <= len(wide.evidence)
    assert tiny.evidence  # 单块即超预算时仍返回第一条，不静默失败


async def test_kb_search_returns_empty_result_instead_of_fabricating(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)

    result = await kb_search_core(
        SearchRequest(
            query="任何问题",
            filter={"must": [{"key": "doc_id", "match": {"value": "不存在的文档"}}]},
        )
    )
    assert result.evidence == []
    assert result.references == []


async def test_kb_search_rejects_malformed_client_filter(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)

    with pytest.raises(ApiError) as excinfo:
        await kb_search_core(SearchRequest(query="任何问题", filter={"must": [{"key": "doc_id"}]}))
    assert excinfo.value.code == "invalid_filter"
    assert excinfo.value.status_code == 400


async def test_kb_search_feeds_heading_context_to_the_reranker(
    ingest_env: IngestEnv, api_service: object
) -> None:
    """⚠️ 回归守门：检索链路必须把「标题 + 正文」交给精排。

    只喂正文会让精排丢掉最强的话题信号（实测 MRR 0.859 → 0.591，见
    :func:`recall.rerank.build_rerank_document`），这里用探针把喂进去的文本抓下来。
    """
    await _prepare_corpus(ingest_env)
    assert isinstance(api_service, Service)
    captured: list[list[str]] = []

    class _SpyReranker(Reranker):
        async def rerank(
            self, query: str, documents: Sequence[str], *, top_n: int = DEFAULT_TOP_N
        ) -> list[RerankHit]:
            captured.append(list(documents))
            return await service_reranker.rerank(query, documents, top_n=top_n)

    service_reranker = api_service.reranker
    api_service.reranker = _SpyReranker()

    await kb_search_core(SearchRequest(query="混合检索怎么融合两路召回结果？", top_k=3))

    assert captured, "精排没有被调用"
    fed = captured[0]
    assert any("\n" in document for document in fed), "喂给精排的文本里没有标题行"
    assert any(doc.startswith("🧪") or "混合检索" in doc.split("\n")[0] for doc in fed)


async def test_kb_search_reports_missing_collection_as_503(
    ingest_env: IngestEnv, api_service: object
) -> None:
    """忘了先跑 ingest 时的真实报错路径：503 + 可操作提示，而不是 500 堆栈。"""
    del ingest_env
    assert isinstance(api_service, Service)
    api_service.collection = "recall__never-built"

    with pytest.raises(ApiError) as excinfo:
        await kb_search_core(SearchRequest(query="任何问题"))

    assert excinfo.value.code == "collection_not_found"
    assert excinfo.value.status_code == 503
    assert "ingest.py" in excinfo.value.message  # 提示怎么修


async def test_kb_answer_reports_missing_collection_too(
    ingest_env: IngestEnv, api_service: object
) -> None:
    del ingest_env
    assert isinstance(api_service, Service)
    api_service.collection = "recall__never-built"

    with pytest.raises(ApiError) as excinfo:
        await kb_answer_core(AnswerRequest(query="任何问题"))

    assert excinfo.value.code == "collection_not_found"


async def test_rest_endpoints_and_error_envelope(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://recall.test") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        payload = health.json()
        assert payload["status"] == "ok"
        assert payload["collection"] == ingest_env.collection
        assert payload["points_count"] > 0
        assert payload["documents"] == 2

        ok = await client.post("/kb/search", json={"query": "混合检索", "top_k": 3})
        assert ok.status_code == 200
        body = ok.json()
        assert body["evidence"]
        assert body["references"]
        assert set(body["evidence"][0]) == {"ref_id", "source_uri", "heading_path", "text", "score"}

        empty_query = await client.post("/kb/search", json={"query": ""})
        assert empty_query.status_code == 422
        assert empty_query.json()["error"]["code"] == "invalid_request"

        bad_filter = await client.post(
            "/kb/search", json={"query": "x", "filter": {"must": [{"key": "doc_id"}]}}
        )
        assert bad_filter.status_code == 400
        assert bad_filter.json()["error"]["code"] == "invalid_filter"

        missing = await client.get("/no-such-route")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "http_error"
