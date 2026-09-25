"""Qdrant 存储适配（tech.md §3.1/§3.3/§5；roadmap R-14、R-18）。

职责：

- **建库**：collection 名 ``recall__<model>@<ver>__<chunker>``，metadata 写死建库参数
  （换 embedding = 建新 collection 并排重灌，旧库不删，tech.md §3.1）；
- **双向量**：dense(1024, cosine) + sparse(learned lexical weights) 命名向量；
- **payload index 第一天就建**：``doc_id``/``owner``/``visibility``/``groups`` (keyword)、
  ``updated_at_ts`` (integer，range 过滤用)（tech.md §3.3、§7）；
- **批量写入**：``upload_points`` 流式灌入，禁止逐 point ``await upsert``（code_standards §4.3）；
- **幂等与清理**：内容寻址 point id ⇒ upsert 原地覆盖；按 ``doc_id`` 做孤儿清理与整篇删除。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, TypeVar, cast
from uuid import UUID

import httpx
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from recall.embedder import DENSE_DIM, Embedding
from recall.models import ChunkPayload

logger = logging.getLogger(__name__)

COLLECTION_TEMPLATE = "recall__{model}@{version}__{chunker}"
"""collection 命名契约（tech.md §3.1，与 code_standards §2 一致）。"""

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

KEYWORD_INDEX_FIELDS: tuple[str, ...] = ("doc_id", "owner", "visibility", "groups")
INTEGER_INDEX_FIELDS: tuple[str, ...] = ("updated_at_ts",)

UPLOAD_BATCH_SIZE = 64
UPLOAD_MAX_RETRIES = 3

RECALL_TOP_K = 50
"""每路召回的候选数（tech.md §4：dense 检索 top_k=50 + sparse 检索 top_k=50）。"""

_T = TypeVar("_T")


class CollectionMismatchError(RuntimeError):
    """目标 collection 的建库参数与本次写入请求不一致（禁止混合写入）。"""


class StoreUnavailableError(RuntimeError):
    """Qdrant 不可达或请求超时（重试已耗尽）。

    单列出来是为了让 API 层能把它映射成语义化的 **503**，而不是一个带堆栈的 500
    （code_standards §6.1）。真实场景：用户没启动 Qdrant 就调 ``/kb/search``。
    """


def collection_name(model: str, version: str, chunker: str) -> str:
    """按契约拼出 collection 名。

    Args:
        model: embedding 模型标识，如 ``bge-m3``。
        version: embedding 版本号，如 ``v1``。
        chunker: 切分器版本号，如 ``md-heading-v1``。

    Returns:
        形如 ``recall__bge-m3@v1__md`` 的 collection 名。
    """
    suffix = chunker.split("-")[0] if chunker else "md"
    return COLLECTION_TEMPLATE.format(model=model, version=version, chunker=suffix)


async def with_retry(
    operation: str,
    func: Callable[[], Awaitable[_T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
) -> _T:
    """指数退避重试（code_standards §10：统一超时 + 退避重试，幂等操作可安全重试）。

    Args:
        operation: 日志用的操作名。
        func: 无参协程工厂，每次重试重新调用。
        attempts: 最大尝试次数（含首次）。
        base_delay: 首次退避秒数，之后按 2 的幂增长。

    Returns:
        ``func`` 的返回值。

    Raises:
        StoreUnavailableError: 重试耗尽且失败原因是**连不上 Qdrant**（超时/连接被拒）——
            调用方应转成 503 而不是 500（code_standards §6.1 语义化状态码）。
        Exception: 其它情况下抛出最后一次异常。
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except Exception as exc:  # noqa: BLE001 - 需要按 attempt 统一退避重试
            last_error = exc
            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "store.retry",
                extra={
                    "operation": operation,
                    "attempt": attempt,
                    "delay_s": delay,
                    "error": str(exc)[:300],
                },
            )
            await asyncio.sleep(delay)
    assert last_error is not None
    if _is_connectivity_error(last_error) or _is_unavailable_response(last_error):
        raise StoreUnavailableError(
            f"{operation} 失败：Qdrant 不可达或超时（已重试 {attempts} 次）"
        ) from last_error
    raise last_error


_CONNECTIVITY_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

_UNAVAILABLE_STATUS_CODES = frozenset({502, 503, 504})
"""把"网关/服务不可用"也当成 Qdrant 不可达（roadmap R-46）。

**为什么需要**：本机若开着系统代理，httpx 的 ``trust_env`` 会把连 Qdrant 的请求也发给
代理，而代理对不可达端口返回的是 **HTTP 502 空体**（不是"连接被拒"）。
qdrant-client 于是抛 ``UnexpectedResponse`` 而不是连接错误 —— 只认 httpx 连接异常的
:func:`_is_connectivity_error` 认不出它，`/kb/search` 就退化成**带堆栈的 500**，
而用户本该看到 503 "请启动 qdrant.exe"（R-27i 的设计被绕过）。

根因侧已在 :func:`~recall.config.Settings.from_env` 里把回环地址并入 ``NO_PROXY``；
这里是**兜底**：无论 502 从代理、反代还是别处来，都归到语义化 503。
"""


def _is_connectivity_error(exc: BaseException) -> bool:
    """异常链里是否含"连不上 / 超时"。

    qdrant-client 会把底层 ``httpx.ConnectError`` 包一层 ``ResponseHandlingException``，
    所以必须沿 ``__cause__`` / ``__context__`` 往下找，不能只看最外层类型。
    """
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, _CONNECTIVITY_ERRORS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_unavailable_response(exc: BaseException) -> bool:
    """异常链里是否含"网关/服务不可用"类响应（502 / 503 / 504）。

    与 :func:`_is_connectivity_error` 同样沿异常链下找：qdrant-client 可能把它再包一层。
    """
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, UnexpectedResponse) and current.status_code in (
            _UNAVAILABLE_STATUS_CODES
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass(frozen=True, slots=True)
class ChunkPoint:
    """一个待写入的 Qdrant point：内容寻址 id + 双向量 + payload。"""

    point_id: str
    payload: ChunkPayload
    embedding: Embedding


class QdrantStore:
    """Qdrant 单机服务的异步适配层。"""

    def __init__(self, url: str, *, timeout: int = 60) -> None:
        """建立客户端（不发起连接）。

        Args:
            url: Qdrant 服务地址，如 ``http://127.0.0.1:6333``。
            timeout: 单次请求超时秒数。
        """
        self._url = url
        self._client = AsyncQdrantClient(url=url, timeout=timeout)

    @property
    def url(self) -> str:
        """Qdrant 服务地址。"""
        return self._url

    @property
    def client(self) -> AsyncQdrantClient:
        """底层客户端（Phase 2 混合检索直接使用）。"""
        return self._client

    async def close(self) -> None:
        """关闭底层连接。"""
        await self._client.close()

    async def ping(self) -> bool:
        """探测服务可用性（``/health`` 端点使用）。

        Returns:
            服务可达时为 ``True``。
        """
        try:
            await self._client.get_collections()
        except Exception as exc:  # noqa: BLE001 - 健康检查不应抛错
            logger.warning("store.ping_failed", extra={"url": self._url, "error": str(exc)[:300]})
            return False
        return True

    async def collection_exists(self, name: str) -> bool:
        """判断 collection 是否存在。

        Args:
            name: collection 名。

        Returns:
            存在返回 ``True``。
        """
        return await with_retry("collection_exists", lambda: self._client.collection_exists(name))

    async def ensure_collection(
        self,
        name: str,
        *,
        embedding_model: str,
        embedding_version: str,
        chunker: str,
        created_at: str | None = None,
        dense_dim: int = DENSE_DIM,
    ) -> bool:
        """确保 collection 存在，并建齐 payload index。

        已存在时只校验并补齐 index，**不删库不重写 metadata**——换 embedding 版本必须
        建新 collection 并排重灌，旧库保留供评测与回滚（tech.md §3.1）。

        Args:
            name: collection 名（见 :func:`collection_name`）。
            embedding_model: 写入 metadata 的模型标识。
            embedding_version: 写入 metadata 的版本号。
            chunker: 写入 metadata 的切分器版本号。
            created_at: 建库日期（ISO，默认今天）。
            dense_dim: dense 向量维度。

        Returns:
            本次调用是否新建了 collection。

        Raises:
            CollectionMismatchError: 同名 collection 的建库参数与本次不一致。
        """
        if await self.collection_exists(name):
            await self.assert_compatible(
                name, embedding_model=embedding_model, embedding_version=embedding_version
            )
            await self._ensure_payload_indexes(name)
            return False

        metadata: dict[str, Any] = {
            "embedding_model": embedding_model,
            "embedding_version": embedding_version,
            "chunker": chunker,
            "created_at": created_at or date.today().isoformat(),
        }
        await with_retry(
            "create_collection",
            lambda: self._client.create_collection(
                collection_name=name,
                vectors_config={
                    DENSE_VECTOR_NAME: models.VectorParams(
                        size=dense_dim, distance=models.Distance.COSINE
                    )
                },
                sparse_vectors_config={SPARSE_VECTOR_NAME: models.SparseVectorParams()},
                metadata=metadata,
            ),
        )
        logger.info("store.collection_created", extra={"collection": name, "metadata": metadata})
        await self._ensure_payload_indexes(name)
        return True

    async def assert_compatible(
        self, name: str, *, embedding_model: str, embedding_version: str
    ) -> None:
        """校验目标 collection 的建库参数与本次一致（禁止混合写入，code_standards §10）。

        Args:
            name: collection 名。
            embedding_model: 本次使用的模型标识。
            embedding_version: 本次使用的版本号。

        Raises:
            CollectionMismatchError: metadata 缺失或与本次请求不一致。
        """
        metadata = await self.collection_metadata(name)
        actual_model = metadata.get("embedding_model")
        actual_version = metadata.get("embedding_version")
        if actual_model != embedding_model or actual_version != embedding_version:
            raise CollectionMismatchError(
                f"collection {name!r} 由 {actual_model}@{actual_version} 建立，"
                f"与本次 {embedding_model}@{embedding_version} 不一致；"
                "换 embedding 版本必须建新 collection（tech.md §3.1）"
            )

    async def _ensure_payload_indexes(self, name: str) -> None:
        """建 payload index（幂等；权限字段与 range 字段第一天就位，tech.md §7）。"""
        for field in KEYWORD_INDEX_FIELDS:
            await self._create_index(name, field, models.PayloadSchemaType.KEYWORD)
        for field in INTEGER_INDEX_FIELDS:
            await self._create_index(name, field, models.PayloadSchemaType.INTEGER)

    async def _create_index(self, name: str, field: str, schema: models.PayloadSchemaType) -> None:
        """建单个字段索引（``create_payload_index`` 本身幂等，可重复调用）。"""
        await with_retry(
            f"index:{field}",
            lambda: self._client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=schema,
                wait=True,
            ),
        )

    async def collection_metadata(self, name: str) -> dict[str, Any]:
        """读回 collection metadata（tech.md §3.1：建库参数可读回）。

        Args:
            name: collection 名。

        Returns:
            metadata 字典；未设置时返回空字典。
        """
        info = await with_retry("collection_info", lambda: self._client.get_collection(name))
        return dict(info.config.metadata or {})

    async def upsert_chunks(self, name: str, points: Sequence[ChunkPoint]) -> None:
        """流式批量写入块（内容寻址 ⇒ 同 id 原地覆盖，幂等机制 2）。

        Args:
            name: collection 名。
            points: 待写入的块；空序列直接返回。
        """
        if not points:
            return
        structs = [_to_point_struct(point) for point in points]
        await with_retry("upload_points", lambda: self._upload_points(name, structs))

    async def _upload_points(self, name: str, structs: list[models.PointStruct]) -> None:
        """流式批量写入（⚠️ qdrant-client 1.19 的 ``upload_points`` 是**同步**方法）。

        实测（2026-09-04，qdrant-client 1.19.1）：``AsyncQdrantClient.upload_points``
        返回 ``None`` 且内部同步完成上传，``await`` 它会抛
        ``TypeError: object NoneType can't be used in 'await' expression``。
        因为是阻塞调用，按 code_standards §0.4 用 :func:`asyncio.to_thread` 隔离。
        """
        uploader = cast("Callable[..., None]", self._client.upload_points)
        await asyncio.to_thread(
            uploader,
            collection_name=name,
            points=structs,
            batch_size=UPLOAD_BATCH_SIZE,
            max_retries=UPLOAD_MAX_RETRIES,
            wait=False,
        )

    async def hybrid_search(
        self,
        name: str,
        *,
        dense: Sequence[float],
        sparse_indices: Sequence[int],
        sparse_values: Sequence[float],
        top_k: int = RECALL_TOP_K,
        query_filter: models.Filter | None = None,
        limit: int | None = None,
    ) -> list[models.ScoredPoint]:
        """dense + sparse 双路检索 → **RRF 融合**（tech.md §4，顺序固定）。

        Args:
            name: collection 名。
            dense: query 的 1024 维 dense 向量。
            sparse_indices: query 的 learned sparse token id（升序）。
            sparse_values: 与 ``sparse_indices`` 一一对应的权重。
            top_k: 每路召回的候选数（tech.md §4：各 ``top_k=50``）。
            query_filter: 服务端收敛后的过滤条件（见 :mod:`recall.auth`），S1 为空。
            limit: 融合后返回条数，默认与 ``top_k`` 相同。

        Returns:
            按 RRF 融合分数降序排列的候选点（payload 已回填，向量不回传）。
        """
        prefetch = [
            models.Prefetch(
                query=list(dense), using=DENSE_VECTOR_NAME, limit=top_k, filter=query_filter
            ),
            models.Prefetch(
                query=models.SparseVector(indices=list(sparse_indices), values=list(sparse_values)),
                using=SPARSE_VECTOR_NAME,
                limit=top_k,
                filter=query_filter,
            ),
        ]
        response = await with_retry(
            "hybrid_search",
            lambda: self._client.query_points(
                collection_name=name,
                prefetch=prefetch,
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                query_filter=query_filter,
                limit=limit or top_k,
                with_payload=True,
                with_vectors=False,
            ),
        )
        return list(response.points)

    async def scroll_doc_ids(self, name: str, doc_id: str) -> set[str]:
        """列出某文档名下全部 point id（孤儿清理的比对基准）。

        Args:
            name: collection 名。
            doc_id: 文档主键。

        Returns:
            该文档名下现存 point id 集合。
        """
        return await with_retry("scroll_doc_ids", lambda: self._scroll_doc_ids(name, doc_id))

    async def _scroll_doc_ids(self, name: str, doc_id: str) -> set[str]:
        ids: set[str] = set()
        offset: Any = None
        while True:
            records, offset = await self._client.scroll(
                collection_name=name,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))
                    ]
                ),
                limit=256,
                with_payload=False,
                with_vectors=False,
                offset=offset,
            )
            ids.update(str(record.id) for record in records)
            if offset is None:
                break
        return ids

    async def delete_orphans(self, name: str, doc_id: str, keep_ids: set[str]) -> int:
        """删除该文档名下不在 ``keep_ids`` 中的旧块（幂等机制 3）。

        Args:
            name: collection 名。
            doc_id: 文档主键。
            keep_ids: 本次重灌产生的全部新 point id。

        Returns:
            被删除的孤儿 point 数。
        """
        existing = await self.scroll_doc_ids(name, doc_id)
        stale: list[int | str | UUID] = sorted(existing - keep_ids)
        if not stale:
            return 0
        await with_retry(
            "delete_orphans",
            lambda: self._client.delete(
                collection_name=name,
                points_selector=models.PointIdsList(points=stale),
                wait=True,
            ),
        )
        logger.info(
            "store.orphans_deleted",
            extra={"collection": name, "doc_id": doc_id, "count": len(stale)},
        )
        return len(stale)

    async def delete_document(self, name: str, doc_id: str) -> int:
        """按 ``doc_id`` 删除整篇文档的全部块（源文件已删除时对账使用）。

        Args:
            name: collection 名。
            doc_id: 文档主键。

        Returns:
            删除前该文档的 point 数。
        """
        existing = await self.scroll_doc_ids(name, doc_id)
        if not existing:
            return 0
        await with_retry(
            "delete_document",
            lambda: self._client.delete(
                collection_name=name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="doc_id", match=models.MatchValue(value=doc_id)
                            )
                        ]
                    )
                ),
                wait=True,
            ),
        )
        return len(existing)

    async def count_points(self, name: str, doc_id: str | None = None) -> int:
        """统计 point 数（可选按 ``doc_id`` 过滤）。

        Args:
            name: collection 名。
            doc_id: 只统计该文档时传入。

        Returns:
            point 数量。
        """
        count_filter = (
            models.Filter(
                must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
            )
            if doc_id is not None
            else None
        )
        result = await with_retry(
            "count_points",
            lambda: self._client.count(collection_name=name, count_filter=count_filter, exact=True),
        )
        return int(result.count)

    async def list_collections(self) -> list[str]:
        """列出全部 collection 名（``kb_stats`` 使用）。"""
        result = await with_retry("list_collections", lambda: self._client.get_collections())
        return sorted(collection.name for collection in result.collections)


def _to_point_struct(point: ChunkPoint) -> models.PointStruct:
    """把 :class:`ChunkPoint` 转成 Qdrant ``PointStruct``（payload 即 tech.md §3.3 契约）。"""
    return models.PointStruct(
        id=point.point_id,
        vector={
            DENSE_VECTOR_NAME: point.embedding.dense,
            SPARSE_VECTOR_NAME: models.SparseVector(
                indices=point.embedding.sparse_indices,
                values=point.embedding.sparse_values,
            ),
        },
        payload=point.payload.model_dump(),
    )
