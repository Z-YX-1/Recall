"""FastAPI 服务：REST 检索口（tech.md §8；roadmap R-22；MCP 挂载见 R-24）。

三条硬约束：

1. **单点强制权限**：所有检索流量只经 :func:`kb_search_core` 一个入口，
   身份与过滤在服务端收敛（code_standards §0.5、§7）；
2. **错误统一信封**：``{"error": {"code", "message"}}`` + 语义化状态码（code_standards §6.1）；
3. **可观测**：每条查询一个 ``trace_id``，记录 query → 双路召回数 → rerank 后数 →
   预算截断后 token → 耗时（code_standards §9）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from qdrant_client import models as qmodels
from starlette.exceptions import HTTPException as StarletteHTTPException

from recall.assemble import Candidate, assemble_evidence
from recall.auth import InvalidFilterError, effective_filter, get_identity
from recall.chunker import CHUNKER_NAME
from recall.config import Settings
from recall.embedder import DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_VERSION, Embedder
from recall.models import ChunkPayload, HealthResult, Identity, SearchRequest, SearchResult
from recall.registry import Registry
from recall.rerank import Reranker
from recall.store import RECALL_TOP_K, QdrantStore, collection_name

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = collection_name(
    DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_VERSION, CHUNKER_NAME
)
"""默认检索 collection：``recall__bge-m3@v1__md``（tech.md §3.1 命名契约）。"""


class ApiError(Exception):
    """带语义化错误码与 HTTP 状态的业务异常。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        """构造业务异常。

        Args:
            code: 机器可读错误码，如 ``invalid_filter``。
            message: 人类可读错误说明。
            status_code: HTTP 状态码。
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(slots=True)
class Service:
    """进程级共享依赖（模型常驻显存，只加载一次）。"""

    settings: Settings
    store: QdrantStore
    embedder: Embedder
    reranker: Reranker
    registry: Registry
    collection: str

    @classmethod
    async def create(cls, settings: Settings, *, collection: str | None = None) -> Service:
        """按配置装配服务依赖。

        Args:
            settings: 运行配置。
            collection: 目标 collection 名；``None`` 时依次回落到
                ``RECALL_COLLECTION`` 与契约默认名（A/B 切库用，tech.md §3.1）。

        Returns:
            装配完成（registry 已建表）的 :class:`Service`。
        """
        registry = Registry(settings.registry_db)
        await registry.initialize()
        return cls(
            settings=settings,
            store=QdrantStore(settings.qdrant_url),
            embedder=Embedder(),
            reranker=Reranker(),
            registry=registry,
            collection=collection or settings.collection or DEFAULT_COLLECTION,
        )

    async def aclose(self) -> None:
        """释放连接（显存由 GC 回收）。"""
        await self.store.close()


_service: Service | None = None
_service_lock = asyncio.Lock()


async def get_service() -> Service:
    """取进程级服务单例（首次调用时装配，REST 与 MCP 共用同一实例）。"""
    global _service  # noqa: PLW0603 - 进程级单例，保证模型只加载一次
    if _service is None:
        async with _service_lock:
            if _service is None:
                _service = await Service.create(Settings.from_env())
                logger.info(
                    "api.service_ready",
                    extra={
                        "collection": _service.collection,
                        "qdrant_url": _service.settings.qdrant_url,
                    },
                )
    return _service


async def close_service() -> None:
    """关闭并丢弃服务单例（应用退出 / 测试收尾）。"""
    global _service  # noqa: PLW0603 - 与 get_service 成对
    if _service is not None:
        await _service.aclose()
        _service = None


async def kb_search_core(request: SearchRequest, identity: Identity | None = None) -> SearchResult:
    """kb_search 瘦核心：检索止步于证据包，**不调 LLM**（tech.md §4/§6）。

    步骤顺序固定：同模型编码 query(dense+sparse) → Qdrant 双路检索(RRF) →
    权限过滤（服务端强制）→ 精排 Top-K → 预算贪心截断 → 同文档合并 → 证据包。

    Args:
        request: 检索请求（query / top_k / max_tokens / filter）。
        identity: 调用者身份；``None`` 时用 S1 默认身份。

    Returns:
        证据包；无命中时返回 ``SearchResult(evidence=[], references=[])``，绝不硬答。

    Raises:
        ApiError: client filter 非法，或目标 collection 不存在。
    """
    trace_id = uuid.uuid4().hex[:12]
    actor = identity if identity is not None else Identity()
    service = await get_service()
    started = time.perf_counter()

    try:
        query_filter = effective_filter(actor, request.filter)
    except InvalidFilterError as exc:
        raise ApiError("invalid_filter", str(exc), 400) from exc

    if not await service.store.collection_exists(service.collection):
        raise ApiError(
            "collection_not_found",
            f"collection {service.collection!r} 不存在，请先运行 python ingest.py --update",
            503,
        )

    embeddings = await service.embedder.encode([request.query])
    embedding = embeddings[0]
    hits = await service.store.hybrid_search(
        service.collection,
        dense=embedding.dense,
        sparse_indices=embedding.sparse_indices,
        sparse_values=embedding.sparse_values,
        top_k=RECALL_TOP_K,
        query_filter=query_filter,
        limit=RECALL_TOP_K,
    )

    if not hits:
        logger.info(
            "kb_search.done",
            extra={
                "trace_id": trace_id,
                "stage": "recall",
                "collection": service.collection,
                "query": request.query,
                "hit_count": 0,
                "reranked_count": 0,
                "dropped_by_budget": 0,
                "selected_tokens": 0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return SearchResult(evidence=[], references=[])

    candidates = [_to_candidate(hit) for hit in hits]
    reranked = await service.reranker.rerank(
        request.query, [candidate.text for candidate in candidates], top_n=request.top_k
    )
    rescored = [replace(candidates[hit.index], score=hit.score) for hit in reranked]

    source_uris = await _resolve_source_uris(service.registry, [item.doc_id for item in rescored])
    rescored = [replace(item, source_uri=source_uris.get(item.doc_id, "")) for item in rescored]

    assembly = assemble_evidence(rescored, max_tokens=request.max_tokens, top_k=request.top_k)
    logger.info(
        "kb_search.done",
        extra={
            "trace_id": trace_id,
            "stage": "full",
            "collection": service.collection,
            "query": request.query,
            "hit_count": len(hits),
            "reranked_count": len(rescored),
            "dropped_by_budget": assembly.dropped_by_budget,
            "selected_tokens": assembly.selected_tokens,
            "evidence_count": len(assembly.result.evidence),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return assembly.result


def _to_candidate(hit: qmodels.ScoredPoint) -> Candidate:
    """把 Qdrant 命中点还原成组装层候选（payload 即 tech.md §3.3 契约）。"""
    try:
        payload = ChunkPayload.model_validate(hit.payload)
    except Exception as exc:  # noqa: BLE001 - payload 损坏归类为服务端错误
        raise ApiError("payload_corrupt", f"point {hit.id} payload 不符合契约：{exc}", 500) from exc
    return Candidate(
        doc_id=payload.doc_id,
        chunk_index=payload.chunk_index,
        source_uri="",  # 由 registry 回填（payload 不含 source_uri，tech.md §3.3）
        heading_path=payload.heading_path,
        text=payload.text,
        token_count=payload.token_count,
        score=float(hit.score),
    )


async def _resolve_source_uris(registry: Registry, doc_ids: Sequence[str]) -> dict[str, str]:
    """把 ``doc_id`` 映射回 ``source_uri``（溯源展示用，tech.md §3.2/§3.3）。"""
    resolved: dict[str, str] = {}
    for doc_id in dict.fromkeys(doc_ids):
        record = await registry.get(doc_id)
        resolved[doc_id] = record.source_uri if record is not None else ""
    return resolved


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动时装配服务（fail fast），退出时释放连接。"""
    del app
    await get_service()
    try:
        yield
    finally:
        await close_service()


app = FastAPI(title="Recall", version="0.1.0", lifespan=lifespan)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """统一错误信封 ``{"error": {"code", "message"}}``（code_standards §6.1）。"""
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


@app.exception_handler(ApiError)
async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    del request
    return _error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    del request
    return _error_response(422, "invalid_request", _format_validation_error(exc))


@app.exception_handler(StarletteHTTPException)
async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """把 Starlette 自带的 404/405 等也纳入统一错误信封（code_standards §6.1）。"""
    del request
    return _error_response(exc.status_code, "http_error", str(exc.detail))


@app.exception_handler(Exception)
async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("api.unhandled", extra={"path": request.url.path})
    return _error_response(500, "internal_error", f"{type(exc).__name__}: {exc}")


def _format_validation_error(exc: RequestValidationError) -> str:
    """把 Pydantic 校验错误压成一行可读文本（不回显输入值，避免泄漏）。"""
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts) or "请求体校验失败"


@app.get("/health")
async def health() -> HealthResult:
    """健康检查：Qdrant 可达性 + collection 状态 + 注册表文档数。"""
    service = await get_service()
    reachable = await service.store.ping()
    ready = reachable and await service.store.collection_exists(service.collection)
    points = await service.store.count_points(service.collection) if ready else 0
    return HealthResult(
        status="ok" if ready else "degraded",
        qdrant=reachable,
        collection=service.collection,
        collection_ready=ready,
        points_count=points,
        documents=await service.registry.count(),
    )


@app.post("/kb/search")
async def kb_search(payload: SearchRequest, request: Request) -> SearchResult:
    """检索个人知识库，返回带出处的证据片段（tech.md §8）。"""
    return await kb_search_core(payload, get_identity(request))
