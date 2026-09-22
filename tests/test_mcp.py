"""MCP 工具与挂载测试（tech.md §8/§9；code_standards §6.2/§6.3；roadmap R-24、R-25）。

覆盖：

- 工具注册与「召唤词」docstring（何时调用 + [n] 引用规则 + 忠实度）；
- 返回 Pydantic ⇒ 自动 outputSchema + structuredContent；
- 工具内异常转**可读错误文本**而非裸抛（code_standards §6.2）；
- ``/mcp`` 挂载在真实 ASGI 生命周期下可握手（code_standards §6.3 的 lifespan 要点）。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from ingest import run_ingest
from recall.api import app, close_service, mcp
from tests.helpers import IngestEnv, ingest_args, write_note

EXPECTED_TOOLS = {"kb_search", "kb_answer", "kb_ingest", "kb_stats"}

_RAG_NOTE = """\
# RAG 检索

## 混合检索

混合检索把 dense 与 sparse 两路召回结果用 RRF 融合，再交给 bge-reranker-v2-m3 精排。
检索质量主要取决于切分粒度，必须用 golden QA 评测校准。
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


async def test_tools_are_registered_with_summoning_docstrings() -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == EXPECTED_TOOLS
    description = tools["kb_search"].description or ""
    assert "何时调用" in description  # 「召唤词」：写清何时调用
    assert "[n]" in description  # 引用规则
    assert "只依据证据回答" in description  # 忠实度要求

    # 返回 Pydantic 模型 ⇒ 自动 outputSchema（code_standards §6.2）
    schema = tools["kb_search"].outputSchema or {}
    assert set(schema.get("properties", {})) == {"evidence", "references"}
    stats_schema = tools["kb_stats"].outputSchema or {}
    assert "points_count" in stats_schema.get("properties", {})


async def test_kb_search_tool_returns_structured_evidence(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)

    async with Client(mcp) as client:
        result = await client.call_tool("kb_search", {"query": "检索质量取决于什么？", "top_k": 3})

    data = result.structured_content
    assert data is not None
    assert data["evidence"]
    assert data["evidence"][0]["source_uri"] == "RAG检索.md"
    assert [item["ref_id"] for item in data["evidence"]] == [
        str(index) for index in range(1, len(data["evidence"]) + 1)
    ]
    assert [ref["ref_id"] for ref in data["references"]] == [
        item["ref_id"] for item in data["evidence"]
    ]


async def test_kb_stats_tool_reports_collection_state(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)

    async with Client(mcp) as client:
        result = await client.call_tool("kb_stats", {})

    data = result.structured_content
    assert data is not None
    assert data["collection"] == ingest_env.collection
    assert data["collection_ready"] is True
    assert data["points_count"] > 0
    assert data["documents"] == 2
    assert data["failed_documents"] == 0
    assert data["embedding_model"] == "bge-m3"
    assert data["chunker"] == "md-heading-v1"
    # A/B 时能看到新旧库并排（tech.md §3.1）
    assert ingest_env.collection in data["collections"]


async def test_tool_errors_are_readable_text_not_tracebacks(
    ingest_env: IngestEnv, api_service: object
) -> None:
    await _prepare_corpus(ingest_env)

    async with Client(mcp) as client:
        result = await client.call_tool("kb_search", {"query": ""}, raise_on_error=False)

    assert result.is_error is True
    text = result.content[0].text if result.content else ""
    assert "参数不合法" in text
    assert "Traceback" not in text


async def test_mcp_endpoint_handshakes_under_app_lifespan(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ code_standards §6.3：挂载必须传 lifespan，否则 Streamable HTTP 会话不初始化。"""
    await _prepare_corpus(ingest_env)
    monkeypatch.setenv("RECALL_COLLECTION", ingest_env.collection)
    await close_service()

    def _client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        **kwargs: Any,
    ) -> httpx.AsyncClient:
        """把 MCP 客户端的 httpx 请求接进进程内 ASGI（不起真实网络）。"""
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://recall.test",
            headers=headers,
            timeout=timeout,
            auth=auth,
            **kwargs,
        )

    transport = StreamableHttpTransport(
        "http://recall.test/mcp", httpx_client_factory=_client_factory
    )
    try:
        async with app.router.lifespan_context(app), Client(transport) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert tools == EXPECTED_TOOLS
            result = await client.call_tool("kb_search", {"query": "红烧肉怎么做"})
            data = result.structured_content
            assert data is not None and data["evidence"]
    finally:
        await close_service()
