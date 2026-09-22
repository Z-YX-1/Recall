"""公共数据契约与 id 规则。

字段名与 ``spec/tech.md`` §3 / §8 完全一致，集中定义以避免各模块自造同名不同义
的字段（``spec/code_standards.md`` §3）。PointId 规则见 ``spec/code_standards.md`` §3.1。

数据流：
    RawDoc（Connector 产出）→ Chunk（切分器产出）→ ChunkPayload（Qdrant point payload）
    → Evidence / SearchResult（检索证据包）→ AnswerResult（胖端点回答）
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------------------
# id 规则（code_standards §3.1）
# --------------------------------------------------------------------------------------

NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # uuid.NAMESPACE_URL


def chunk_content_hash(text: str) -> str:
    """256-bit sha256 十六进制——用于变更检测与注册表，存入 payload.content_hash。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_point_id(doc_id: str, chunk_index: int, content_hash: str) -> str:
    """确定性 PointId：位置感知的内容寻址。

    Qdrant PointId 只接受 uint64 或 UUID 字符串（``⚠️`` 64 位 hex sha256 不是合法
    UUID），因此这里用 uuid5 生成 128-bit 主键。同 doc + 同位置 + 同文本 ⇒ 同 id
    ⇒ upsert 原地覆盖（幂等）；换文本 ⇒ 新 id，旧 id 成为孤儿交由清理删除。

    Args:
        doc_id: 文档稳定 slug 主键。
        chunk_index: 块在文档内的全局序号（从 0 起）。
        content_hash: :func:`chunk_content_hash` 的结果。

    Returns:
        标准 UUID 字符串形式的 point id。
    """
    return str(uuid.uuid5(NAMESPACE, f"recall://{doc_id}/{chunk_index}/{content_hash}"))


# --------------------------------------------------------------------------------------
# 核心模型（tech.md §3.2 / §3.3 / §8；code_standards §3.2）
# --------------------------------------------------------------------------------------


class Identity(BaseModel):
    """调用者身份（S1 硬编码，S2+ 由 API key 中间件填充，tech.md §7）。"""

    user: str = "me"
    groups: list[str] = ["owner"]


class RawDoc(BaseModel):
    """Connector 枚举出的原始文档（code_standards §4.1），text 为归一化全文。

    Attributes:
        updated_at: 来源侧最后修改时间（ISO 8601）。填充 registry 的 ``updated_at``
            与 payload 的 ``updated_at_ts``（tech.md §3.2/§3.3）；不支持该语义的
            Connector 可留空，属向后兼容的可选字段（见 roadmap §七 变更日志）。
    """

    doc_id: str
    source_uri: str
    text: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = ""


class Chunk(BaseModel):
    """切分器产出的块（chunker 内部契约，对应 tech.md §3 的 heading_path + sub_index）。

    不属于 Qdrant payload 契约：入库时由 :class:`ChunkPayload` 承载。
    """

    text: str
    heading_path: str
    sub_index: int
    token_count: int


class ChunkPayload(BaseModel):
    """Qdrant point payload（tech.md §3.3）——必须存完整块原文。"""

    doc_id: str
    chunk_index: int
    heading_path: str
    text: str
    content_hash: str
    token_count: int
    embedding_model: str
    embedding_version: str
    owner: str = "me"
    visibility: str = "private"
    groups: list[str] = Field(default_factory=list)
    updated_at_ts: int  # Unix 秒整数（range 索引用；ISO 串不能建 range 索引）


class Evidence(BaseModel):
    """返回给消费端的证据片段（tech.md §8 /kb/search）。"""

    ref_id: str
    source_uri: str
    heading_path: str
    text: str
    score: float


class SearchResult(BaseModel):
    """kb_search 证据包：evidence 与 references 的 ``[n]`` 一一对应（索引 = n-1）。"""

    evidence: list[Evidence]
    references: list[dict[str, str]]  # [{ref_id, source_uri}]


class AnswerResult(BaseModel):
    """kb_answer 胖端点回答（tech.md §8，DeepSeek JSON 模式）。"""

    answer: str
    citations: list[int]
    references: list[dict[str, str]]


class DocRecord(BaseModel):
    """文档注册表记录（tech.md §3.2，SQLite，不进向量库）。"""

    doc_id: str
    source_type: str
    source_uri: str
    title: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    content_hash: str
    updated_at: str
    indexed_at: str
    owner: str = "me"
    visibility: str = "private"
    chunk_count: int = 0
    error: str | None = None


# --------------------------------------------------------------------------------------
# API 请求 / 响应模型（tech.md §8；字段与端点契约逐字一致）
# --------------------------------------------------------------------------------------


class SearchRequest(BaseModel):
    """``POST /kb/search`` 请求体（tech.md §8）。"""

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=20, ge=1, le=100)
    max_tokens: int = Field(default=3000, ge=1, le=32000)
    filter: dict[str, Any] = Field(default_factory=dict)
    """客户端过滤条件：只能收窄身份可见范围，服务端强制与 scope 取交集（tech.md §7）。"""


class HealthResult(BaseModel):
    """``GET /health`` 响应体。"""

    status: str
    qdrant: bool
    collection: str
    collection_ready: bool
    points_count: int
    documents: int
    """文档注册表中的文档数（与 points_count 对账用）。"""


class StatsResult(BaseModel):
    """``kb_stats`` MCP 工具返回体（只读，无副作用）。"""

    collection: str
    qdrant: bool
    collection_ready: bool
    points_count: int
    documents: int
    failed_documents: int
    embedding_model: str
    embedding_version: str
    chunker: str
    created_at: str


class AnswerRequest(BaseModel):
    """``POST /kb/answer`` 请求体（tech.md §8，胖端点）。"""

    query: str = Field(min_length=1, max_length=2000)
    max_tokens: int = Field(default=3000, ge=1, le=32000)


class IngestRequest(BaseModel):
    """``POST /kb/ingest`` 请求体（tech.md §8，**有副作用**的写操作）。"""

    mode: Literal["update", "rebuild"] = "update"
    collection: str | None = None
    """目标 collection；``None`` 时用契约默认名。"""


class IngestSummary(BaseModel):
    """``POST /kb/ingest`` 与 MCP ``kb_ingest`` 的返回体。"""

    mode: str
    collection: str
    scanned: int
    skipped: int
    indexed_docs: int
    indexed_chunks: int
    orphans_deleted: int
    deleted_docs: int
    failed: list[str] = Field(default_factory=list)
    elapsed_s: float
