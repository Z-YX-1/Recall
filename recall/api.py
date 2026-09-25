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
import hmac
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import ValidationError
from qdrant_client import models as qmodels
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from recall.assemble import Candidate, assemble, assemble_evidence
from recall.audit import (
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_UNAUTHORIZED,
    AuditLog,
    AuditRecord,
    utc_now_iso,
)
from recall.auth import (
    InvalidFilterError,
    current_identity,
    effective_filter,
    get_identity,
    reset_current_identity,
    set_current_identity,
)
from recall.chunker import CHUNKER_NAME
from recall.config import Settings, configure_logging
from recall.embedder import DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_VERSION, Embedder
from recall.llm import DeepSeekClient, LlmNotConfiguredError
from recall.models import (
    AnswerRequest,
    AnswerResult,
    ChunkPayload,
    HealthResult,
    Identity,
    IngestRequest,
    IngestSummary,
    SearchRequest,
    SearchResult,
    StatsResult,
)
from recall.registry import Registry
from recall.rerank import Reranker, build_rerank_document
from recall.store import (
    RECALL_TOP_K,
    QdrantStore,
    StoreUnavailableError,
    collection_name,
)

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = collection_name(
    DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_VERSION, CHUNKER_NAME
)
"""默认检索 collection：``recall__bge-m3@v1__md``（tech.md §3.1 命名契约）。"""

PUBLIC_PATHS = frozenset({"/health"})
"""**免鉴权**路径（roadmap R-40）。

只放探活端点：``/health`` 要被监控/脚本无凭据调用，且它不返回任何笔记内容。
其余一切（含 ``/kb/stats``、``/mcp`` 与**回环请求**）在启用鉴权后都要求 API key
—— 项目工程师 2026-09-25 定案「不豁免回环」，理由是避免"本机就免检"这条隐性规则
在将来（容器 / 反向代理 / 隧道）突然失效时变成静默的鉴权缺口。
"""

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
"""回环监听地址；不在其中即视为"对外暴露"。"""


def _warn_if_exposed_without_keys(settings: Settings) -> None:
    """监听非回环地址却**没有** key 表时大声告警。

    ``RECALL_API_KEYS`` 为空时鉴权整体不启用（见
    :data:`~recall.config.DEFAULT_API_KEYS` 的 fail-open 说明）。那条默认值是为了
    不让升级打断本机使用，但"监听 0.0.0.0 且无 key"就是真的敞开了
    （``POST /kb/ingest`` 是写端点）——所以这里补一条启动告警。

    Args:
        settings: 运行配置。
    """
    if settings.host not in LOCAL_HOSTS and not settings.auth_enabled:
        logger.warning(
            "api.exposed_without_auth 监听 %s 但未配置 RECALL_API_KEYS："
            "所有端点（含写端点 /kb/ingest）当前无需凭据即可访问。",
            settings.host,
            extra={"host": settings.host},
        )


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
    llm: DeepSeekClient
    collection: str
    audit: AuditLog
    """审计写入器（roadmap R-40）；随服务单例重建，测试可拿到干净状态。"""

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
        configure_logging(settings, component="api")
        _warn_if_exposed_without_keys(settings)
        return cls(
            settings=settings,
            store=QdrantStore(settings.qdrant_url),
            embedder=Embedder(),
            reranker=Reranker(),
            registry=registry,
            llm=DeepSeekClient.from_settings(settings),
            collection=collection or settings.collection or DEFAULT_COLLECTION,
            audit=AuditLog(settings.audit_log_path if settings.log_to_file else None),
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

    try:
        collection_ready = await service.store.collection_exists(service.collection)
    except StoreUnavailableError as exc:
        raise _qdrant_unavailable(exc) from exc
    if not collection_ready:
        raise ApiError(
            "collection_not_found",
            f"collection {service.collection!r} 不存在，请先运行 python ingest.py --update",
            503,
        )

    embeddings = await service.embedder.encode([request.query])
    embedding = embeddings[0]
    try:
        hits = await service.store.hybrid_search(
            service.collection,
            dense=embedding.dense,
            sparse_indices=embedding.sparse_indices,
            sparse_values=embedding.sparse_values,
            top_k=RECALL_TOP_K,
            query_filter=query_filter,
            limit=RECALL_TOP_K,
        )
    except StoreUnavailableError as exc:
        raise _qdrant_unavailable(exc) from exc

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
        request.query,
        [build_rerank_document(candidate.heading_path, candidate.text) for candidate in candidates],
        top_n=request.top_k,
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


async def kb_answer_core(request: AnswerRequest, identity: Identity | None = None) -> AnswerResult:
    """kb_answer 胖端点：kb_search + 组装 + DeepSeek 生成（tech.md §6/§8）。

    瘦/胖判据 = **服务是否自己调 LLM 出最终答案**（tech.md §15 决策 2）——本函数就是胖的那一半。

    ⚠️ 副作用：会把证据文本发送到 DeepSeek API（数据出域仅限这一次生成调用）。
    本地 embedding 与知识库内容仍然不出域（tech.md §15 决策 7）。

    Args:
        request: 回答请求（query / max_tokens）。
        identity: 调用者身份；``None`` 时用 S1 默认身份。

    Returns:
        :class:`~recall.models.AnswerResult`；无证据时直接返回"没找到"，**不调 LLM 硬答**。

    Raises:
        ApiError: 未配置 DeepSeek key，或 LLM 调用失败。
    """
    trace_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    search = await kb_search_core(
        SearchRequest(query=request.query, max_tokens=request.max_tokens), identity
    )

    if not search.evidence:
        logger.info(
            "kb_answer.done",
            extra={
                "trace_id": trace_id,
                "stage": "no_evidence",
                "query": request.query,
                "evidence_count": 0,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return AnswerResult(
            answer="笔记里没有检索到与该问题相关的内容，建议换一个说法或关键词再问。",
            citations=[],
            references=[],
        )

    prompt, ref_map = assemble(search.evidence, request.max_tokens)
    service = await get_service()
    try:
        payload = await service.llm.complete_json(prompt)
    except LlmNotConfiguredError as exc:
        raise ApiError("llm_not_configured", str(exc), 503) from exc
    except Exception as exc:  # noqa: BLE001 - LLM 失败统一转语义化错误
        logger.exception("kb_answer.llm_failed", extra={"trace_id": trace_id})
        raise ApiError("llm_failed", f"生成失败：{type(exc).__name__}: {exc}", 502) from exc

    raw_answer = payload.get("answer")
    answer = str(raw_answer).strip() if raw_answer is not None else ""
    citations = _normalize_citations(payload.get("citations"), len(search.evidence), trace_id)

    logger.info(
        "kb_answer.done",
        extra={
            "trace_id": trace_id,
            "stage": "generated",
            "query": request.query,
            "evidence_count": len(search.evidence),
            "citation_count": len(citations),
            "prompt_tokens": len(ref_map),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return AnswerResult(answer=answer, citations=citations, references=search.references)


def _normalize_citations(raw: object, evidence_count: int, trace_id: str) -> list[int]:
    """清洗 LLM 返回的 ``citations``：去重保序、剔除越界编号（防引用幻觉）。

    Args:
        raw: LLM 返回的 ``citations`` 字段原文。
        evidence_count: 本次证据条数，合法编号为 ``1..evidence_count``。
        trace_id: 日志用查询 id。

    Returns:
        合法且去重的编号列表。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    cleaned: list[int] = []
    dropped: list[object] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            dropped.append(item)
            continue
        try:
            number = int(item)
        except ValueError:
            dropped.append(item)
            continue
        if not 1 <= number <= evidence_count or number in cleaned:
            dropped.append(item)
            continue
        cleaned.append(number)
    if dropped:
        logger.warning(
            "kb_answer.citations_dropped",
            extra={"trace_id": trace_id, "dropped": [str(item) for item in dropped]},
        )
    return cleaned


async def kb_ingest_core(request: IngestRequest) -> IngestSummary:
    """摄取入口：参数化调用同一管道（幂等三机制见 :mod:`ingest`，tech.md §5）。

    ⚠️ **有副作用的写操作**：会写入 Qdrant 与 registry。

    Args:
        request: 摄取请求（mode / collection）。

    Returns:
        本次 run 的汇总。

    Raises:
        ApiError: vault 未配置或不存在等参数问题。
    """
    from ingest import build_parser, run_ingest  # 延迟导入：CLI 模块只在真正摄取时加载

    argv = [f"--{request.mode}", "--log-level", "WARNING"]
    if request.collection:
        argv += ["--collection", request.collection]
    args = build_parser().parse_args(argv)
    try:
        report = await run_ingest(args)
    except SystemExit as exc:  # run_ingest 用 SystemExit 报告参数问题
        raise ApiError("ingest_rejected", str(exc), 400) from exc
    return IngestSummary(
        mode=report.mode,
        collection=report.collection,
        scanned=report.scanned,
        skipped=report.skipped,
        indexed_docs=report.indexed_docs,
        indexed_chunks=report.indexed_chunks,
        orphans_deleted=report.orphans_deleted,
        deleted_docs=report.deleted_docs,
        failed=list(report.failed),
        elapsed_s=round(report.elapsed_s, 2),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """组合生命周期：装配 Recall 服务（fail fast）+ 初始化 FastMCP 会话管理。

    ⚠️ code_standards §6.3：必须把 ``mcp_app.lifespan`` 纳入 FastAPI 的 lifespan，
    否则 Streamable HTTP 的会话管理不初始化，``/mcp`` 请求会失败。
    """
    await get_service()
    try:
        async with mcp_app.lifespan(app):
            yield
    finally:
        await close_service()


app = FastAPI(title="Recall", version="0.1.0", lifespan=lifespan)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """统一错误信封 ``{"error": {"code", "message"}}``（code_standards §6.1）。"""
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


def _qdrant_unavailable(exc: Exception) -> ApiError:
    """把"连不上 Qdrant"转成语义化的 503（而不是带堆栈的 500）。"""
    return ApiError(
        "qdrant_unavailable",
        f"知识库服务不可用（Qdrant 未响应）：{exc}。请确认 tools/qdrant/qdrant.exe 已启动。",
        503,
    )


@app.exception_handler(StoreUnavailableError)
async def _handle_store_unavailable(request: Request, exc: StoreUnavailableError) -> JSONResponse:
    """兜住所有未在 core 层转换的 Qdrant 不可达（例如 ``/kb/ingest``）。"""
    del request
    error = _qdrant_unavailable(exc)
    return _error_response(error.status_code, error.code, error.message)


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


def _format_validation_error(exc: RequestValidationError | ValidationError) -> str:
    """把 Pydantic 校验错误压成一行可读文本（不回显输入值，避免泄漏）。"""
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts) or "请求体校验失败"


# --------------------------------------------------------------------------------------
# 身份中间件（roadmap R-40；tech.md §7 的 S2）—— 全项目**唯一**的鉴权点
# --------------------------------------------------------------------------------------


def _extract_api_token(request: Request) -> str:
    """从请求头取 API key：``X-API-Key`` 优先，其次 ``Authorization: Bearer``。

    Args:
        request: 当前请求。

    Returns:
        token 原文；两种头都没带时返回空串。
    """
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        return api_key
    authorization = request.headers.get("authorization", "").strip()
    prefix = "bearer "
    if authorization.lower().startswith(prefix):
        return authorization[len(prefix) :].strip()
    return ""


def _resolve_identity(request: Request, api_keys: Mapping[str, str]) -> Identity | None:
    """用 key 表解析身份；未携带或匹配不上时返回 ``None``。

    ⚠️ 用 :func:`hmac.compare_digest` **逐条常量时间比较**，不用 ``dict.get``：
    后者会因命中位置不同产生可测量的时间差，等于给暴力枚举留了旁路。

    Args:
        request: 当前请求。
        api_keys: ``{token: user}`` 映射（来自 ``RECALL_API_KEYS``）。

    Returns:
        匹配成功时的身份（``groups`` 暂留空，S3 再加组）；否则 ``None``。
    """
    token = _extract_api_token(request)
    if not token:
        return None
    candidate = token.encode("utf-8")
    for expected, user in api_keys.items():
        if hmac.compare_digest(expected.encode("utf-8"), candidate):
            return Identity(user=user, groups=[])
    return None


def _record_audit(
    service: Service,
    request: Request,
    status: int,
    started: float,
    identity: Identity | None,
    outcome: str,
) -> None:
    """写一行审计（内容与失败处理见 :mod:`recall.audit`）。"""
    service.audit.record(
        AuditRecord(
            ts=utc_now_iso(),
            user=identity.user if identity is not None else "-",
            groups=list(identity.groups) if identity is not None else [],
            method=request.method,
            path=request.url.path,
            status=status,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            outcome=outcome,
            trace_id=uuid.uuid4().hex[:12],
            client=request.client.host if request.client is not None else "-",
        )
    )


class IdentityMiddleware:
    """**唯一的鉴权点**：校验 API key → 写身份 contextvar → 审计（roadmap R-40）。

    为什么是"一个中间件"而不是"MCP 用 FastMCP 原生 auth + REST 另写一套"：
    两条鉴权路径必然分叉（R-45 的教训）。这里 REST 与 ``/mcp`` 共用**同一份**
    key 表、同一段解析、同一个 401 信封；``/mcp`` 是挂载的子应用，而 ASGI
    中间件包住整个 app（含挂载），因此必然在它之前生效。

    为什么用**纯 ASGI 中间件**而不是 ``@app.middleware("http")``：

    1. ``BaseHTTPMiddleware`` 会把下游放进子任务并对响应体加一层包装，而 ``/mcp``
       走的是 ``text/event-stream`` **长连接**——纯 ASGI 只代理 ``send``，对流式响应零干预；
    2. contextvar 在同一任务内可见是身份传递的前提，纯 ASGI 下无歧义。

    未配置 key 表时**整段跳过**（S1 语义，见 :data:`~recall.config.DEFAULT_API_KEYS`）。
    """

    def __init__(self, app: ASGIApp) -> None:
        """包装下游 ASGI 应用。

        Args:
            app: 下游 ASGI 应用（Starlette 处理链）。
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI 入口：非 HTTP 作用域（lifespan 等）直接透传。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        started = time.perf_counter()
        service = await get_service()

        identity: Identity | None = None
        if service.settings.auth_enabled and request.url.path not in PUBLIC_PATHS:
            identity = _resolve_identity(request, service.settings.api_keys)
            if identity is None:
                _record_audit(service, request, 401, started, None, OUTCOME_UNAUTHORIZED)
                await _error_response(
                    401,
                    "unauthorized",
                    "缺少或无效的 API key：请携带 X-API-Key 或 Authorization: Bearer <key>。",
                )(scope, receive, send)
                return

        status = 500

        async def _capture_status(message: Message) -> None:
            """记录下游响应状态码供审计用，其余原样透传。"""
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        token = set_current_identity(identity) if identity is not None else None
        try:
            await self.app(scope, receive, _capture_status)
        except Exception:  # noqa: BLE001 - 记录审计后原样重抛，不吞异常
            _record_audit(service, request, status, started, identity, OUTCOME_ERROR)
            raise
        finally:
            if token is not None:
                reset_current_identity(token)

        _record_audit(
            service,
            request,
            status,
            started,
            identity,
            OUTCOME_OK if status < 400 else OUTCOME_ERROR,
        )


app.add_middleware(IdentityMiddleware)


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


async def kb_stats_core() -> StatsResult:
    """采集知识库状态（**只读**，无副作用）。

    REST ``GET /kb/stats`` 与 MCP 工具 ``kb_stats`` **共用这一份实现**，
    避免同一份口径在两条接入路径上各自演化。

    Returns:
        目标 collection、现存 collection 列表、点数、文档数、失败文档数与建库参数。
    """
    service = await get_service()
    reachable = await service.store.ping()
    ready = reachable and await service.store.collection_exists(service.collection)
    metadata = await service.store.collection_metadata(service.collection) if ready else {}
    records = await service.registry.list_all()
    return StatsResult(
        collection=service.collection,
        collections=await service.store.list_collections() if reachable else [],
        qdrant=reachable,
        collection_ready=ready,
        points_count=(await service.store.count_points(service.collection) if ready else 0),
        documents=len(records),
        failed_documents=sum(1 for record in records if record.error),
        embedding_model=str(metadata.get("embedding_model", "")),
        embedding_version=str(metadata.get("embedding_version", "")),
        chunker=str(metadata.get("chunker", "")),
        created_at=str(metadata.get("created_at", "")),
    )


@app.get("/kb/stats")
async def kb_stats_endpoint() -> StatsResult:
    """知识库状态（只读）：collection / 点数 / 文档数 / 失败文档数 / 建库参数。

    与 MCP 工具 ``kb_stats`` 同源同形（tech.md §8，2026-09-24 由项目工程师确认新增）；
    不依赖 MCP 也能查状态，便于脚本与运维。
    """
    return await kb_stats_core()


@app.post("/kb/search")
async def kb_search_endpoint(payload: SearchRequest, request: Request) -> SearchResult:
    """检索个人知识库，返回带出处的证据片段（tech.md §8）。

    函数名与 MCP 工具 ``kb_search`` 区分：本函数只服务 REST 路由。
    """
    return await kb_search_core(payload, get_identity(request))


@app.post("/kb/answer")
async def kb_answer_endpoint(payload: AnswerRequest, request: Request) -> AnswerResult:
    """胖端点：检索 → 组装 → DeepSeek 生成带引用的回答（tech.md §8）。"""
    return await kb_answer_core(payload, get_identity(request))


@app.post("/kb/ingest")
async def kb_ingest_endpoint(payload: IngestRequest) -> IngestSummary:
    """摄取知识库（**有副作用的写操作**，tech.md §8）。

    ⚠️ code_standards §6.1：本端点自 S2 起必须挂鉴权 + 限流；S1 仅监听 127.0.0.1。
    """
    return await kb_ingest_core(payload)


# --------------------------------------------------------------------------------------
# MCP 工具与挂载（tech.md §8/§9；code_standards §6.2/§6.3；roadmap R-24、R-25）
# --------------------------------------------------------------------------------------

mcp = FastMCP(
    name="recall",
    instructions=(
        "Recall（拾忆）个人知识库。检索笔记内容一律用 kb_search 取回带出处的证据，"
        "再按 recall-assembly 规范用 [n] 标注引用作答；不要凭记忆回答笔记里的内容。"
    ),
)


@mcp.tool
async def kb_search(query: str, top_k: int = 20, max_tokens: int = 3000) -> SearchResult:
    """搜索个人知识库，返回带出处的证据片段（只读，无副作用）。

    何时调用：用户问题涉及"我的笔记/知识库里说过什么"时。
    调用方须按返回的 references 用 [n] 标注引用，且只依据证据回答。

    Args:
        query: 检索问题（用中文原问，勿自行改写）
        top_k: 召回候选数
        max_tokens: 调用方可接受的证据 token 预算
    """
    try:
        return await kb_search_core(
            SearchRequest(query=query, top_k=top_k, max_tokens=max_tokens), current_identity()
        )
    except ValidationError as exc:
        raise ToolError(f"参数不合法：{_format_validation_error(exc)}") from exc
    except ApiError as exc:
        raise ToolError(f"[{exc.code}] {exc.message}") from exc
    except Exception as exc:  # noqa: BLE001 - MCP 工具不裸抛，异常转可读文本（§6.2）
        logger.exception("mcp.kb_search_failed")
        raise ToolError(f"检索失败：{type(exc).__name__}: {exc}") from exc


@mcp.tool
async def kb_stats() -> StatsResult:
    """查看个人知识库的当前状态（只读，无副作用）。

    何时调用：需要确认"知识库里有多少内容/建库参数是什么/是否就绪"时。

    Returns:
        目标 collection、点数、文档数、失败文档数与建库参数。
    """
    try:
        return await kb_stats_core()
    except Exception as exc:  # noqa: BLE001 - MCP 工具不裸抛，异常转可读文本（§6.2）
        logger.exception("mcp.kb_stats_failed")
        raise ToolError(f"读取知识库状态失败：{type(exc).__name__}: {exc}") from exc


@mcp.tool
async def kb_answer(query: str, max_tokens: int = 3000) -> AnswerResult:
    """检索笔记并**由服务端自己调 LLM** 生成带引用的回答（胖端点）。

    何时调用：调用方希望直接拿到成稿回答、而不是自己组装证据时（例如客服 / 业务系统）。

    ⚠️ 副作用：会把检索到的证据文本发送到 DeepSeek API 完成这一次生成；
    如果调用方要自己组装作答（DSH agent 的默认姿势），请改用只读的 ``kb_search``。

    Args:
        query: 用户问题（用中文原问，勿自行改写）
        max_tokens: 证据 token 预算

    Returns:
        answer 正文（引用处为 [n]）、citations（用到的编号）、references（与 [n] 一一对应）。
    """
    try:
        return await kb_answer_core(
            AnswerRequest(query=query, max_tokens=max_tokens), current_identity()
        )
    except ValidationError as exc:
        raise ToolError(f"参数不合法：{_format_validation_error(exc)}") from exc
    except ApiError as exc:
        raise ToolError(f"[{exc.code}] {exc.message}") from exc
    except Exception as exc:  # noqa: BLE001 - MCP 工具不裸抛，异常转可读文本（§6.2）
        logger.exception("mcp.kb_answer_failed")
        raise ToolError(f"生成失败：{type(exc).__name__}: {exc}") from exc


@mcp.tool
async def kb_ingest(mode: str = "update", collection: str | None = None) -> IngestSummary:
    """把 Obsidian 笔记增量同步进知识库（**写操作，有副作用**）。

    何时调用：用户明确要求"更新/重建知识库"时；**只读提问不要调用本工具**。

    ⚠️ 副作用：会写入 / 覆盖 Qdrant 中的向量点与 SQLite 注册表记录；
    ``mode="rebuild"`` 会忽略内容哈希整篇重灌（仍不删除旧 collection）。

    Args:
        mode: "update" 增量（哈希未变即跳过）或 "rebuild" 整篇重灌
        collection: 目标 collection 名；留空用契约默认名
    """
    try:
        return await kb_ingest_core(
            IngestRequest.model_validate({"mode": mode, "collection": collection})
        )
    except ValidationError as exc:
        raise ToolError(f"参数不合法：{_format_validation_error(exc)}") from exc
    except ApiError as exc:
        raise ToolError(f"[{exc.code}] {exc.message}") from exc
    except Exception as exc:  # noqa: BLE001 - MCP 工具不裸抛，异常转可读文本（§6.2）
        logger.exception("mcp.kb_ingest_failed")
        raise ToolError(f"摄取失败：{type(exc).__name__}: {exc}") from exc


mcp_app = mcp.http_app(path="/", stateless_http=Settings.from_env().mcp_stateless)
"""code_standards §6.3：``http_app(path="/")`` + ``mount("/mcp")`` ⇒ 端点 ``/mcp``。

⚠️ ``stateless_http`` 来自配置（默认 **True**，见
:data:`~recall.config.DEFAULT_MCP_STATELESS` / roadmap R-44）：状态化模式下会话
存在服务进程内存里，空闲 30 分钟或被重启进程都会让客户端手里那个 id 变成
HTTP 404 ``Session not found``，而 MCP 客户端不会据此重新握手 ⇒ 默认无会话更稳。
"""

app.mount("/mcp", mcp_app)


def main() -> None:
    """``python -m recall.api`` 启动服务：监听地址与端口从配置读（tech.md §12 进程 2）。

    等价于 ``uvicorn recall.api:app --host <RECALL_HOST> --port <RECALL_PORT>``，
    但走 :class:`~recall.config.Settings`，部署时不必再手抄一遍 host/port。
    """
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(
        "recall.api:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
