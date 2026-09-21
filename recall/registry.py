"""SQLite 文档注册表（tech.md §3.2、§5；roadmap R-11）。

账本职责：

- **文档级 hash 跳过**：``content_hash`` 未变且非 ``--force`` ⇒ 整篇跳过（幂等机制 1）；
- **删除对账**：registry 里有、本次枚举没有的 doc ⇒ 删点 + 删行（孤儿清理的文档侧）；
- **错误隔离**：单文档失败只记 ``error``，绝不中断整个 run（code_standards §4.1）。

SQLite 是阻塞 IO，统一用 :func:`asyncio.to_thread` 隔离（code_standards §0.4）。
连接按操作短开短关，避免跨线程共享连接。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from recall.models import DocRecord

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    source_type  TEXT NOT NULL,
    source_uri   TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    frontmatter  TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT '',
    indexed_at   TEXT NOT NULL DEFAULT '',
    owner        TEXT NOT NULL DEFAULT 'me',
    visibility   TEXT NOT NULL DEFAULT 'private',
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, source_uri);
"""

_COLUMNS = (
    "doc_id",
    "source_type",
    "source_uri",
    "title",
    "frontmatter",
    "content_hash",
    "updated_at",
    "indexed_at",
    "owner",
    "visibility",
    "chunk_count",
    "error",
)

_UPSERT_SQL = f"""
INSERT INTO documents ({", ".join(_COLUMNS)})
VALUES ({", ".join("?" for _ in _COLUMNS)})
ON CONFLICT(doc_id) DO UPDATE SET
    {", ".join(f"{col} = excluded.{col}" for col in _COLUMNS if col != "doc_id")}
"""

_ERROR_SQL = """
INSERT INTO documents (doc_id, source_type, source_uri, error)
VALUES (?, ?, ?, ?)
ON CONFLICT(doc_id) DO UPDATE SET
    error = excluded.error,
    source_uri = excluded.source_uri
"""


class Registry:
    """文档注册表（SQLite 单文件）的异步门面。"""

    def __init__(self, db_path: Path) -> None:
        """初始化注册表门面。

        Args:
            db_path: SQLite 数据库文件路径（父目录会自动创建）。
        """
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        """注册表文件路径。"""
        return self._db_path

    async def initialize(self) -> None:
        """建表建索引（幂等，可重复调用）。"""
        await asyncio.to_thread(self._initialize_sync)

    async def get(self, doc_id: str) -> DocRecord | None:
        """读取单个文档记录。

        Args:
            doc_id: 文档稳定 slug 主键。

        Returns:
            记录存在时返回 :class:`~recall.models.DocRecord`，否则 ``None``。
        """
        return await asyncio.to_thread(self._get_sync, doc_id)

    async def list_all(self) -> list[DocRecord]:
        """列出全部文档记录（按 doc_id 排序，保证确定性）。"""
        return await asyncio.to_thread(self._list_all_sync)

    async def upsert(self, record: DocRecord) -> None:
        """整行写入 / 覆盖一条文档记录。

        Args:
            record: 待落库的注册表记录。
        """
        await asyncio.to_thread(self._upsert_sync, record)

    async def record_error(
        self, doc_id: str, source_type: str, source_uri: str, message: str
    ) -> None:
        """记录单文档失败状态并保留其它字段（错误隔离，不中断 run）。

        Args:
            doc_id: 失败的文档主键（无稳定 slug 时可用来源路径派生）。
            source_type: Connector 类型，如 ``obsidian``。
            source_uri: 来源相对路径。
            message: 失败原因（截断至 2000 字符，避免日志/库膨胀）。
        """
        await asyncio.to_thread(self._record_error_sync, doc_id, source_type, source_uri, message)

    async def delete(self, doc_id: str) -> None:
        """删除一条文档记录（源文件已不存在时对账使用）。

        Args:
            doc_id: 待删除的文档主键。
        """
        await asyncio.to_thread(self._delete_sync, doc_id)

    async def count(self) -> int:
        """返回文档总数。"""
        return await asyncio.to_thread(self._count_sync)

    # ------------------------------------------------------------------ 同步实现

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_sync(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)
        logger.debug("registry.initialized", extra={"db_path": str(self._db_path)})

    def _get_sync(self, doc_id: str) -> DocRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def _list_all_sync(self) -> list[DocRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM documents ORDER BY doc_id").fetchall()
        return [_row_to_record(row) for row in rows]

    def _upsert_sync(self, record: DocRecord) -> None:
        values = (
            record.doc_id,
            record.source_type,
            record.source_uri,
            record.title,
            _dump_frontmatter(record.frontmatter),
            record.content_hash,
            record.updated_at,
            record.indexed_at,
            record.owner,
            record.visibility,
            record.chunk_count,
            record.error,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(_UPSERT_SQL, values)

    def _record_error_sync(
        self, doc_id: str, source_type: str, source_uri: str, message: str
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(_ERROR_SQL, (doc_id, source_type, source_uri, message[:2000]))
        logger.warning(
            "registry.doc_error",
            extra={"doc_id": doc_id, "source_uri": source_uri, "error": message[:500]},
        )

    def _delete_sync(self, doc_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))

    def _count_sync(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
        return int(row["n"])


def _dump_frontmatter(frontmatter: dict[str, Any]) -> str:
    """把 frontmatter 序列化为稳定 JSON（``default=str`` 兜住 YAML 日期等类型）。"""
    return json.dumps(frontmatter, ensure_ascii=False, sort_keys=True, default=str)


def _row_to_record(row: sqlite3.Row) -> DocRecord:
    """把 SQLite 行还原为 :class:`~recall.models.DocRecord`。"""
    try:
        frontmatter: dict[str, Any] = json.loads(row["frontmatter"] or "{}")
    except ValueError:
        logger.warning("registry.frontmatter_unparsable", extra={"doc_id": row["doc_id"]})
        frontmatter = {}
    return DocRecord(
        doc_id=row["doc_id"],
        source_type=row["source_type"],
        source_uri=row["source_uri"],
        title=row["title"],
        frontmatter=frontmatter,
        content_hash=row["content_hash"],
        updated_at=row["updated_at"],
        indexed_at=row["indexed_at"],
        owner=row["owner"],
        visibility=row["visibility"],
        chunk_count=int(row["chunk_count"]),
        error=row["error"],
    )
