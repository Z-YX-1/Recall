"""身份与权限必测项（code_standards §7 与 §13：``effective_filter`` 只收窄）。

结构断言保证「合取而非并集」，集成断言用真实 Qdrant 证明**放宽的过滤条件拿不到
别人私有的块**——这是权限红线，必须端到端验证。

本文件同时覆盖 **S2 身份中间件与审计**（tech.md §7；roadmap R-40）：

1. **唯一鉴权点**：REST 与 ``/mcp`` 共用一份 key 表、一段解析、同一个 401 信封；
2. **写端点必须挡住**：``POST /kb/ingest`` 无 key 一律 401（code_standards §6.1）；
3. **身份真正流进检索**：key 映射成谁，就只看得见谁的笔记——这是"通了"与
   "只是返回 401"的区别所在；
4. **审计不泄密**：审计行含身份与状态，但**绝不包含密钥本身**。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from qdrant_client import models

from ingest import run_ingest
from recall.api import IdentityMiddleware, app, close_service, get_service, mcp
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
from tests.helpers import IngestEnv, ingest_args, write_note


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


# ======================================================================================
# S2 身份中间件与审计（roadmap R-40）
# ======================================================================================

ME_TOKEN = "tok-me-0123456789abcdef"
ALICE_TOKEN = "tok-alice-0123456789abcdef"
"""测试用 token。刻意**不像**真实密钥，避免被误当成生产凭据。"""

_RAG_NOTE = """\
# RAG 检索

## 混合检索

混合检索把 dense 与 sparse 两路召回结果用 RRF 融合，再交给 bge-reranker-v2-m3 精排。
"""


async def _prepare_corpus(ingest_env: IngestEnv) -> None:
    """灌入一篇归属于 ``me`` 的笔记（``ChunkPayload.owner`` 默认即 ``me``）。"""
    write_note(ingest_env.vault, "RAG检索.md", _RAG_NOTE)
    report = await run_ingest(ingest_args(ingest_env))
    assert report.indexed_docs == 1
    assert report.failed == []


@pytest.fixture
async def unsecured(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[IngestEnv]:
    """**不启用**鉴权的服务实例（S1 语义）：显式把 key 表置空。

    显式置空而不是 ``delenv``：``load_dotenv(override=False)`` 会把 ``.env`` 里有、
    真实环境里没有的变量读回来，不置空的话一旦 ``.env`` 配了 key，用例就会漂。
    """
    monkeypatch.setenv("RECALL_API_KEYS", "")
    monkeypatch.setenv("RECALL_COLLECTION", ingest_env.collection)
    await close_service()
    await get_service()
    try:
        yield ingest_env
    finally:
        await close_service()


@pytest.fixture
async def secured(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, str]]:
    """**启用**鉴权的服务实例，产出 ``{user: token}``。"""
    monkeypatch.setenv("RECALL_API_KEYS", f"{ME_TOKEN}:me,{ALICE_TOKEN}:alice")
    monkeypatch.setenv("RECALL_COLLECTION", ingest_env.collection)
    await close_service()
    await get_service()
    try:
        yield {"me": ME_TOKEN, "alice": ALICE_TOKEN}
    finally:
        await close_service()


def _client() -> httpx.AsyncClient:
    """接进进程内 ASGI 的 REST 客户端（不起真实网络）。"""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://recall.test")


async def test_no_key_table_keeps_s1_semantics(unsecured: IngestEnv) -> None:
    """未配置 ``RECALL_API_KEYS`` ⇒ 退回 S1（不鉴权），确保升级不打断既有使用。"""
    async with _client() as client:
        response = await client.get("/kb/stats")

    assert response.status_code == 200


async def test_requests_without_key_are_rejected(secured: dict[str, str]) -> None:
    """启用鉴权后，无 key 的请求被 401 拒绝，且走**统一错误信封**（§6.1）。"""
    async with _client() as client:
        response = await client.get("/kb/stats")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert "X-API-Key" in response.json()["error"]["message"]


async def test_wrong_key_is_rejected(secured: dict[str, str]) -> None:
    """错误的 key 与"没有 key"一样被拒（不能因为带了个头就放行）。"""
    async with _client() as client:
        response = await client.get("/kb/stats", headers={"X-API-Key": "not-a-real-token"})

    assert response.status_code == 401


async def test_x_api_key_and_bearer_are_both_accepted(secured: dict[str, str]) -> None:
    """两种携带方式都支持：``X-API-Key`` 与 ``Authorization: Bearer``。"""
    async with _client() as client:
        by_header = await client.get("/kb/stats", headers={"X-API-Key": secured["me"]})
        by_bearer = await client.get(
            "/kb/stats", headers={"Authorization": f"Bearer {secured['me']}"}
        )

    assert by_header.status_code == 200
    assert by_bearer.status_code == 200
    assert by_header.json() == by_bearer.json()


async def test_health_stays_public(secured: dict[str, str]) -> None:
    """``/health`` 免鉴权：监控/脚本要能无凭据探活，且它不返回任何笔记内容。"""
    async with _client() as client:
        response = await client.get("/health")

    assert response.status_code == 200


async def test_ingest_write_endpoint_requires_key(
    secured: dict[str, str], ingest_env: IngestEnv
) -> None:
    """⚠️ code_standards §6.1：写端点自 S2 起**必须**挂鉴权。

    这是 R-40 存在的首要理由——``POST /kb/ingest`` 能重灌整个知识库，
    无鉴权地暴露到公网等于把写入口交出去。
    """
    async with _client() as client:
        response = await client.post("/kb/ingest", json={"mode": "update"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_mcp_path_is_guarded_by_the_same_middleware(secured: dict[str, str]) -> None:
    """``/mcp`` 与 REST 走**同一个**中间件：无 key / 错 key 都在下游之前被拒。

    这里刻意不带 lifespan：鉴权失败时中间件**短路**，请求根本到不了 MCP 应用，
    因此无需初始化它的会话管理（也避开了 ``mcp_app.lifespan`` 只能跑一次的隔离坑）。
    """
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "auth-probe", "version": "0"},
        },
    }
    accept = {"Accept": "application/json, text/event-stream"}

    async with _client() as client:
        no_key = await client.post("/mcp/", json=payload, headers=accept)
        bad_key = await client.post("/mcp/", json=payload, headers={**accept, "X-API-Key": "wrong"})

    assert no_key.status_code == 401
    assert no_key.json()["error"]["code"] == "unauthorized"
    assert bad_key.status_code == 401


async def test_identity_from_key_scopes_the_search(
    secured: dict[str, str], ingest_env: IngestEnv
) -> None:
    """**身份必须真正流进检索**：主人看得见，旁人看不见。

    笔记的 ``owner`` 是 ``me``（``ChunkPayload`` 默认值）。用 ``me`` 的 key 检索应命中；
    换成 ``alice`` 的 key，服务端强制注入的可见范围（``owner==alice`` 或 ``public``）
    会把它全部过滤掉 ⇒ 证据为空。若中间件只是"校验通过就放行"而不传身份，
    这条断言就会失败——这正是本用例存在的意义。
    """
    await _prepare_corpus(ingest_env)

    async with _client() as client:
        as_owner = await client.post(
            "/kb/search",
            json={"query": "混合检索", "top_k": 3},
            headers={"X-API-Key": secured["me"]},
        )
        as_other = await client.post(
            "/kb/search",
            json={"query": "混合检索", "top_k": 3},
            headers={"X-API-Key": secured["alice"]},
        )

    assert as_owner.status_code == 200
    assert as_owner.json()["evidence"], "主人应当检索到自己的笔记"

    assert as_other.status_code == 200
    assert as_other.json()["evidence"] == [], "旁人不得看到他人笔记"


def _mcp_factory(token: str, target: FastAPI) -> Any:
    """构造把 MCP 客户端接进指定 ASGI app、并固定携带某个 key 的 httpx 工厂。"""

    def _factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        **kwargs: Any,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target),
            base_url="http://recall.test",
            headers={**(headers or {}), "X-API-Key": token},
            timeout=timeout,
            auth=auth,
            **kwargs,
        )

    return _factory


async def test_mcp_tool_identity_flows_from_the_request_header(
    secured: dict[str, str], ingest_env: IngestEnv
) -> None:
    """MCP 工具侧也能拿到身份（``api.py`` 的 ``kb_search`` 曾**不传** identity）。

    ⚠️ 这里自建 ``FastAPI`` + ``mcp.http_app(...)`` 实例，而不是复用模块级 ``app``：
    ``StreamableHTTPSessionManager.run()`` **每个实例只能跑一次**，模块级 ``mcp_app``
    已被"挂载 + lifespan"用例占用。中间件类本身是同一个，测的仍是生产代码。
    """
    await _prepare_corpus(ingest_env)

    mcp_asgi = mcp.http_app(path="/", stateless_http=True)
    test_app = FastAPI()
    test_app.add_middleware(IdentityMiddleware)
    test_app.mount("/mcp", mcp_asgi)

    async with mcp_asgi.lifespan(mcp_asgi):
        owner_transport = StreamableHttpTransport(
            "http://recall.test/mcp", httpx_client_factory=_mcp_factory(ME_TOKEN, test_app)
        )
        other_transport = StreamableHttpTransport(
            "http://recall.test/mcp", httpx_client_factory=_mcp_factory(ALICE_TOKEN, test_app)
        )

        async with Client(owner_transport) as client:
            owner = await client.call_tool("kb_search", {"query": "混合检索", "top_k": 3})
        async with Client(other_transport) as client:
            other = await client.call_tool("kb_search", {"query": "混合检索", "top_k": 3})

    owner_data = owner.structured_content
    other_data = other.structured_content
    assert owner_data is not None and owner_data["evidence"]
    assert other_data is not None and other_data["evidence"] == []


async def test_audit_log_records_both_outcomes_without_leaking_the_key(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """一次放行、一次拒绝都要留痕；且**审计文件里不得出现密钥**。

    审计与结构化日志同目录，因此受 ``RECALL_LOG_TO_FILE`` 控制（测试默认关掉，
    免得污染 ``data/``）；这里显式打开并改到 ``tmp_path``。
    """
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("RECALL_API_KEYS", f"{ME_TOKEN}:me")
    monkeypatch.setenv("RECALL_COLLECTION", ingest_env.collection)
    monkeypatch.setenv("RECALL_LOG_DIR", str(log_dir))
    monkeypatch.setenv("RECALL_LOG_TO_FILE", "1")
    await close_service()
    await get_service()
    try:
        async with _client() as client:
            allowed = await client.get("/kb/stats", headers={"X-API-Key": ME_TOKEN})
            denied = await client.get("/kb/stats")
    finally:
        await close_service()

    assert allowed.status_code == 200
    assert denied.status_code == 401

    raw = (log_dir / "audit.jsonl").read_text(encoding="utf-8")
    assert ME_TOKEN not in raw, "审计文件绝不能包含密钥"

    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    by_outcome = {record["outcome"]: record for record in records}
    assert set(by_outcome) == {"ok", "unauthorized"}

    allowed_record = by_outcome["ok"]
    assert allowed_record["user"] == "me"
    assert allowed_record["path"] == "/kb/stats"
    assert allowed_record["status"] == 200
    assert allowed_record["method"] == "GET"
    assert allowed_record["client"]

    denied_record = by_outcome["unauthorized"]
    assert denied_record["user"] == "-", "被拒时还没有身份"
    assert denied_record["status"] == 401
