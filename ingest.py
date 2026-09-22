"""摄取 CLI（tech.md §5；roadmap R-15）。

用法::

    python ingest.py --update                       # 增量摄取（默认模式）
    python ingest.py --rebuild                      # 重灌同库（可断点续传，见下）
    python ingest.py --rebuild --collection recall__bge-m3@v2__md --model bge-m3@v2
    python ingest.py --update --force               # 无条件重灌（重新嵌入）

三种跳过语义：

- ``--update``：registry 命中同 ``content_hash`` ⇒ 整篇跳过（不切分、不编码）；
- ``--rebuild``：忽略账本快路径，但**目标 collection 已与本次切分结果一致时跳过**
  —— 换 embedding 版本并排重灌时目标库是空的，等于全量重灌；中断后重跑只补没写完的部分；
- ``--force``：无条件重新嵌入 + 原地覆盖。

**幂等三机制**（顺序固定，code_standards §4.2）：

1. 文档级 hash 跳过——``content_hash`` 未变且非 ``--force`` ⇒ 整篇跳过；
2. 块级内容寻址——``point_id = uuid5(doc_id/chunk_index/content_hash)`` ⇒ upsert 原地覆盖；
3. 孤儿清理——重灌后按 ``doc_id`` 扫出旧 point，删除不在新 id 集合中的点。

**断点续传** = 直接重跑：未写完 registry 的文档下次会被重新处理，已写完的走 hash 跳过。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from recall.chunker import CHUNKER_NAME, MAX_CHUNK_TOKENS, chunk_markdown
from recall.config import Settings
from recall.connectors.base import BaseConnector, Connector, SourceError, slugify
from recall.connectors.obsidian import SOURCE_TYPE, ObsidianConnector
from recall.embedder import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_VERSION,
    DEFAULT_MODEL_NAME,
    Embedder,
)
from recall.models import (
    Chunk,
    ChunkPayload,
    DocRecord,
    RawDoc,
    chunk_content_hash,
    chunk_point_id,
)
from recall.registry import Registry
from recall.store import ChunkPoint, QdrantStore, collection_name

logger = logging.getLogger("recall.ingest")


@dataclass(frozen=True, slots=True)
class ModelRef:
    """``<模型标识>@<版本>`` 引用，如 ``bge-m3@v1``。"""

    model: str
    version: str

    @classmethod
    def parse(cls, raw: str) -> ModelRef:
        """解析 ``--model`` 参数。

        Args:
            raw: ``bge-m3@v2`` 形式；省略 ``@版本`` 时用默认版本。

        Returns:
            解析后的引用。
        """
        model, _, version = raw.partition("@")
        return cls(
            model=model.strip() or DEFAULT_EMBEDDING_MODEL,
            version=version.strip() or DEFAULT_EMBEDDING_VERSION,
        )

    def __str__(self) -> str:
        return f"{self.model}@{self.version}"


@dataclass(slots=True)
class IngestReport:
    """一次摄取 run 的汇总（run 结束打印，失败清单同列）。"""

    mode: str
    collection: str
    elapsed_s: float = 0.0
    scanned: int = 0
    skipped: int = 0
    indexed_docs: int = 0
    indexed_chunks: int = 0
    orphans_deleted: int = 0
    deleted_docs: int = 0
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """人类可读的一句话总结。"""
        return (
            f"[{self.mode}] collection={self.collection} "
            f"扫描={self.scanned} 跳过={self.skipped} 入库文档={self.indexed_docs} "
            f"入库块={self.indexed_chunks} 孤儿删除={self.orphans_deleted} "
            f"文档删除={self.deleted_docs} 失败={len(self.failed)} "
            f"耗时={self.elapsed_s:.1f}s"
        )


def build_parser() -> argparse.ArgumentParser:
    """构造摄取命令行解析器。"""
    parser = argparse.ArgumentParser(description="Recall 摄取管道（幂等、可重复执行）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help="增量摄取（默认）")
    mode.add_argument(
        "--rebuild",
        action="store_true",
        help="重灌：忽略 registry 快路径，但目标库已与本次切分一致时跳过（可断点续传）",
    )
    parser.add_argument("--collection", default=None, help="目标 collection 名（默认按契约生成）")
    parser.add_argument(
        "--model",
        default=f"{DEFAULT_EMBEDDING_MODEL}@{DEFAULT_EMBEDDING_VERSION}",
        help="embedding 标识，形如 bge-m3@v1",
    )
    parser.add_argument(
        "--model-path", default=DEFAULT_MODEL_NAME, help="HuggingFace 模型名或本地路径"
    )
    parser.add_argument("--chunker", default=CHUNKER_NAME, help="切分器版本号")
    parser.add_argument(
        "--vault", default=None, help="Obsidian vault 路径（覆盖 RECALL_VAULT_PATH）"
    )
    parser.add_argument(
        "--skip-dirs",
        default=None,
        help="逗号分隔的跳过目录名；默认用 DEFAULT_SKIP_DIRS（含 node_modules 等工程产物）",
    )
    parser.add_argument("--force", action="store_true", help="无条件重灌（重新嵌入 + 原地覆盖）")
    parser.add_argument("--max-tokens", type=int, default=MAX_CHUNK_TOKENS, help="单块 token 上限")
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="嵌入批大小（16~32）"
    )
    parser.add_argument("--device", default=None, help="推理设备，如 cuda:0 / cpu")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    return parser


async def run_ingest(args: argparse.Namespace) -> IngestReport:
    """执行一次完整摄取（幂等、可重复执行）。

    Args:
        args: :func:`build_parser` 解析出的命令行参数。

    Returns:
        本次 run 的汇总报告。
    """
    settings = Settings.from_env()
    model_ref: ModelRef = ModelRef.parse(args.model)
    collection = args.collection or collection_name(
        model_ref.model, model_ref.version, args.chunker
    )
    vault = Path(args.vault) if args.vault else settings.vault_path
    if vault is None:
        raise SystemExit("缺少 vault 路径：请设置 RECALL_VAULT_PATH 或传 --vault")
    if not vault.is_dir():
        raise SystemExit(f"vault 路径不存在或不是目录：{vault}")

    report = IngestReport(mode="rebuild" if args.rebuild else "update", collection=collection)

    registry = Registry(settings.registry_db)
    await registry.initialize()
    store = QdrantStore(settings.qdrant_url)
    embedder = Embedder(
        args.model_path,
        embedding_model=model_ref.model,
        embedding_version=model_ref.version,
        batch_size=args.batch_size,
        device=args.device,
    )
    connector: Connector = _build_connector(SOURCE_TYPE, vault, _parse_skip_dirs(args.skip_dirs))

    started = time.perf_counter()
    try:
        await store.ensure_collection(
            collection,
            embedding_model=model_ref.model,
            embedding_version=model_ref.version,
            chunker=args.chunker,
        )
        docs = await asyncio.to_thread(lambda: list(connector.list()))
        seen: set[str] = set()
        for doc in docs:
            report.scanned += 1
            seen.add(doc.doc_id)
            await _ingest_one(
                doc=doc,
                connector=connector,
                registry=registry,
                store=store,
                embedder=embedder,
                collection=collection,
                max_tokens=args.max_tokens,
                rebuild=bool(args.rebuild),
                force=bool(args.force),
                report=report,
            )

        # 错误隔离：失败文档记 error 状态，并排除在"已删除"对账之外，避免误删旧块
        failed_ids: set[str] = set()
        for source_error in _drain_errors(connector):
            error_doc_id = (
                source_error.doc_id or f"vault-{slugify(source_error.source_uri) or 'root'}"
            )
            failed_ids.add(error_doc_id)
            await registry.record_error(
                error_doc_id, SOURCE_TYPE, source_error.source_uri, source_error.message
            )
            report.failed.append(f"{source_error.source_uri}: {source_error.message}")

        report.deleted_docs = await _reconcile_deleted(
            registry=registry, store=store, collection=collection, seen=seen | failed_ids
        )
    finally:
        await store.close()

    report.elapsed_s = time.perf_counter() - started
    logger.info(
        "ingest.finished",
        extra={
            "collection": collection,
            "mode": report.mode,
            "scanned": report.scanned,
            "skipped": report.skipped,
            "indexed_docs": report.indexed_docs,
            "indexed_chunks": report.indexed_chunks,
            "orphans_deleted": report.orphans_deleted,
            "deleted_docs": report.deleted_docs,
            "failed": len(report.failed),
            "latency_ms": round(report.elapsed_s * 1000, 1),
        },
    )
    return report


async def _ingest_one(
    *,
    doc: RawDoc,
    connector: Connector,
    registry: Registry,
    store: QdrantStore,
    embedder: Embedder,
    collection: str,
    max_tokens: int,
    rebuild: bool,
    force: bool,
    report: IngestReport,
) -> None:
    """处理单个文档：跳过判定 → 切分 → 嵌入 → upsert → 孤儿清理 → 记账。

    三种跳过语义（tech.md §5 幂等机制 1 + 断点续传）：

    - ``--update``：registry 命中同 ``content_hash`` ⇒ 整篇跳过（不切分、不编码）；
    - ``--rebuild``：忽略上面的账本快路径，但**目标 collection 已与本次切分结果一致时跳过**
      —— 这就是断点续传：中断后重跑只补没写完的文档；
    - ``--force``：无条件重灌（重新嵌入 + 原地覆盖）。
    """
    try:
        content_hash = connector.hash_of(doc)
        existing = await registry.get(doc.doc_id)
        if (
            not rebuild
            and not force
            and existing is not None
            and existing.error is None
            and existing.content_hash == content_hash
        ):
            report.skipped += 1
            return

        chunks = chunk_markdown(doc.text, max_tokens=max_tokens)
        if (
            rebuild
            and not force
            and await _target_already_matches(store, collection, doc.doc_id, chunks)
        ):
            report.skipped += 1
            await _record_doc(
                registry=registry,
                connector=connector,
                doc=doc,
                content_hash=content_hash,
                chunk_count=len(chunks),
            )
            return

        embeddings = await embedder.encode([chunk.text for chunk in chunks])
        updated_at_ts = _to_unix_seconds(doc.updated_at)
        groups: list[str] = []

        points: list[ChunkPoint] = []
        for index, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
            payload = ChunkPayload(
                doc_id=doc.doc_id,
                chunk_index=index,
                heading_path=chunk.heading_path,
                text=chunk.text,
                content_hash=chunk_content_hash(chunk.text),
                token_count=chunk.token_count,
                embedding_model=embedder.embedding_model,
                embedding_version=embedder.embedding_version,
                owner="me",
                visibility="private",
                groups=groups,
                updated_at_ts=updated_at_ts,
            )
            points.append(
                ChunkPoint(
                    point_id=chunk_point_id(doc.doc_id, index, payload.content_hash),
                    payload=payload,
                    embedding=embedding,
                )
            )

        await store.upsert_chunks(collection, points)
        report.orphans_deleted += await store.delete_orphans(
            collection, doc.doc_id, {point.point_id for point in points}
        )
        await _record_doc(
            registry=registry,
            connector=connector,
            doc=doc,
            content_hash=content_hash,
            chunk_count=len(points),
        )
        report.indexed_docs += 1
        report.indexed_chunks += len(points)
        logger.info(
            "ingest.doc_indexed",
            extra={
                "doc_id": doc.doc_id,
                "source_uri": doc.source_uri,
                "chunk_count": len(points),
                "collection": collection,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 单文档失败隔离，绝不中断 run
        logger.exception(
            "ingest.doc_failed", extra={"doc_id": doc.doc_id, "source_uri": doc.source_uri}
        )
        await registry.record_error(
            doc.doc_id, connector.source_type, doc.source_uri, f"{type(exc).__name__}: {exc}"
        )
        report.failed.append(f"{doc.source_uri}: {type(exc).__name__}: {exc}")


async def _target_already_matches(
    store: QdrantStore, collection: str, doc_id: str, chunks: list[Chunk]
) -> bool:
    """目标 collection 是否已持有该文档**本次切分结果**对应的全部 point（断点续传判定）。

    利用内容寻址的确定性：只要 block 切分结果一致，point id 集合就该完全相等。
    """
    expected = {
        chunk_point_id(doc_id, index, chunk_content_hash(chunk.text))
        for index, chunk in enumerate(chunks)
    }
    return await store.scroll_doc_ids(collection, doc_id) == expected


async def _record_doc(
    *,
    registry: Registry,
    connector: Connector,
    doc: RawDoc,
    content_hash: str,
    chunk_count: int,
) -> None:
    """写 registry 账本（摄取成功的统一落账点）。"""
    await registry.upsert(
        DocRecord(
            doc_id=doc.doc_id,
            source_type=connector.source_type,
            source_uri=doc.source_uri,
            title=_title_of(doc),
            frontmatter=doc.frontmatter,
            content_hash=content_hash,
            updated_at=doc.updated_at,
            indexed_at=_now_iso(),
            owner="me",
            visibility="private",
            chunk_count=chunk_count,
            error=None,
        )
    )


async def _reconcile_deleted(
    *, registry: Registry, store: QdrantStore, collection: str, seen: set[str]
) -> int:
    """源侧已删除的文档：删其全部 point + 删注册表行。"""
    deleted = 0
    for record in await registry.list_all():
        if record.doc_id in seen:
            continue
        removed = await store.delete_document(collection, record.doc_id)
        await registry.delete(record.doc_id)
        deleted += 1
        logger.info(
            "ingest.doc_removed",
            extra={
                "doc_id": record.doc_id,
                "source_uri": record.source_uri,
                "points_deleted": removed,
            },
        )
    return deleted


def _build_connector(source_type: str, vault: Path, skip_dirs: tuple[str, ...] | None) -> Connector:
    """按来源类型构造 Connector（v1 只有 Obsidian，新来源在此注册）。"""
    if source_type == SOURCE_TYPE:
        return ObsidianConnector(vault, skip_dirs=skip_dirs)
    raise SystemExit(f"未支持的 source_type: {source_type}")


def _parse_skip_dirs(raw: str | None) -> tuple[str, ...] | None:
    """解析 ``--skip-dirs``：逗号分隔；未提供时返回 ``None``（用连接器默认值）。"""
    if raw is None:
        return None
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _drain_errors(connector: Connector) -> list[SourceError]:
    """取出 Connector 的错误缓冲（``BaseConnector`` 的可选扩展，协议之外）。"""
    if isinstance(connector, BaseConnector):
        return connector.drain_errors()
    return []


def _title_of(doc: RawDoc) -> str:
    """标题：frontmatter ``title`` 优先，否则用文件名。"""
    raw_title = doc.frontmatter.get("title")
    if isinstance(raw_title, str) and raw_title.strip():
        return raw_title.strip()
    return Path(doc.source_uri).stem


def _to_unix_seconds(iso_timestamp: str) -> int:
    """ISO 8601 → Unix 秒整数（payload 的 ``updated_at_ts``，range 索引用）。"""
    if iso_timestamp:
        try:
            return int(datetime.fromisoformat(iso_timestamp).timestamp())
        except ValueError:
            logger.warning("ingest.bad_updated_at", extra={"updated_at": iso_timestamp})
    return int(time.time())


def _now_iso() -> str:
    """当前时间的本地时区 ISO 8601 字符串。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def configure_logging(level: str) -> None:
    """配置结构化日志（code_standards §9：禁止库代码裸 print）。

    同时把 stdout/stderr 切到 UTF-8——Windows 控制台默认 GBK，笔记标题里的 emoji
    会让 run 结束时的汇总打印抛 ``UnicodeEncodeError``，把一次成功的摄取变成失败退出。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数（默认取 ``sys.argv[1:]``）。

    Returns:
        进程退出码：有失败文档时为 1，否则 0。
    """
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    report = asyncio.run(run_ingest(args))
    print(report.summary())
    if report.failed:
        print("失败清单：", file=sys.stderr)
        for item in report.failed:
            print(f"  - {item}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
