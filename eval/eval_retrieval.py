"""检索评测：Recall@K / MRR（tech.md §10；code_standards §11；roadmap R-31）。

- **query 必须用该 collection 的 embedding 模型编码**，否则分数失真（code_standards §11）；
- ``--collection`` 参数化 ⇒ 新旧 collection 并排 A/B，旧库不删（tech.md §3.1/§10）；
- 命中判定：Top-K 里出现 ``expected_sources`` 中任意一条 ``source_uri`` 即算命中。

用法::

    python eval/eval_retrieval.py --collection recall__bge-m3@v1__md
    python eval/eval_retrieval.py --collection recall__bge-m3@v2__md --no-rerank
    python eval/eval_retrieval.py --k 1,3,5,10 --output eval/baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:  # 直接运行脚本时保证能 import recall
    sys.path.insert(0, str(PROJECT_ROOT))

from recall.api import DEFAULT_COLLECTION, Service, close_service, get_service  # noqa: E402
from recall.auth import effective_filter  # noqa: E402
from recall.models import ChunkPayload, Identity, SearchRequest  # noqa: E402
from recall.store import RECALL_TOP_K  # noqa: E402

logger = logging.getLogger("recall.eval.retrieval")

MAX_EVAL_TOKENS = 32000
"""评测时把预算放到上限，避免预算截断干扰召回指标（预算本身另有单测覆盖）。"""


@dataclass(slots=True)
class QuestionResult:
    """单题评测结果。"""

    question: str
    expected: list[str]
    returned: list[str]
    rank: int | None

    @property
    def hit(self) -> bool:
        """是否命中（任意 Top-K 里出现期望来源）。"""
        return self.rank is not None


@dataclass(slots=True)
class EvalReport:
    """一次评测的汇总（可直接落盘做 A/B 对比）。"""

    collection: str
    mode: str
    golden: str
    total: int
    recall_at: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    elapsed_s: float = 0.0
    results: list[QuestionResult] = field(default_factory=list)


def build_parser() -> argparse.ArgumentParser:
    """构造评测命令行解析器。"""
    parser = argparse.ArgumentParser(description="Recall 检索评测（Recall@K / MRR）")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help="目标 collection 名")
    parser.add_argument(
        "--golden",
        default=str(PROJECT_ROOT / "eval" / "golden_set.jsonl"),
        help="黄金集 JSONL 路径",
    )
    parser.add_argument("--k", default="1,3,5,10", help="逗号分隔的 K 列表")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0 = 全部）")
    parser.add_argument(
        "--no-rerank", action="store_true", help="只评测混合召回（跳过精排），用于分阶段 A/B"
    )
    parser.add_argument("--output", default=None, help="把汇总 JSON 写到该路径")
    parser.add_argument("--log-level", default="WARNING", help="日志级别")
    return parser


def _as_source_list(raw: object) -> list[object]:
    """把黄金集里的 ``expected_sources`` 规整成列表（结构已在校验阶段保证）。"""
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw]


def load_golden(path: Path) -> list[dict[str, object]]:
    """读取黄金集（每行 ``{"question", "expected_sources"}``）。

    Args:
        path: JSONL 文件路径。

    Returns:
        逐行解析后的字典列表。

    Raises:
        SystemExit: 文件不存在或某行结构非法。
    """
    if not path.is_file():
        raise SystemExit(f"黄金集不存在：{path}")
    items: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except ValueError as exc:
            raise SystemExit(f"黄金集第 {number} 行不是合法 JSON：{exc}") from exc
        if not isinstance(item, dict) or "question" not in item or "expected_sources" not in item:
            raise SystemExit(f"黄金集第 {number} 行缺少 question / expected_sources")
        items.append(item)
    return items


async def evaluate(args: argparse.Namespace) -> EvalReport:
    """跑一遍黄金集，返回 Recall@K / MRR。

    Args:
        args: 命令行参数。

    Returns:
        评测汇总（含逐题明细）。
    """
    import os

    os.environ["RECALL_COLLECTION"] = args.collection
    await close_service()
    service = await get_service()

    golden_path = Path(args.golden)
    items = load_golden(golden_path)
    if args.limit > 0:
        items = items[: args.limit]
    ks = sorted({int(part) for part in str(args.k).split(",") if part.strip()})
    max_k = ks[-1]
    mode = "hybrid-only" if args.no_rerank else "hybrid+rerank"

    started = time.perf_counter()
    results: list[QuestionResult] = []
    for item in items:
        question = str(item["question"])
        expected = [str(source) for source in _as_source_list(item["expected_sources"])]
        returned = await _retrieve(service, question, max_k, rerank=not args.no_rerank)
        rank = next(
            (position for position, source in enumerate(returned, start=1) if source in expected),
            None,
        )
        results.append(
            QuestionResult(question=question, expected=expected, returned=returned, rank=rank)
        )

    total = len(results)
    report = EvalReport(
        collection=args.collection,
        mode=mode,
        golden=golden_path.name,
        total=total,
        recall_at={
            k: (sum(1 for item in results if item.rank is not None and item.rank <= k) / total)
            if total
            else 0.0
            for k in ks
        },
        mrr=(sum(1.0 / item.rank for item in results if item.rank is not None) / total)
        if total
        else 0.0,
        elapsed_s=time.perf_counter() - started,
        results=results,
    )
    await close_service()
    return report


async def _retrieve(service: Service, question: str, max_k: int, *, rerank: bool) -> list[str]:
    """取回 Top-``max_k`` 的 ``source_uri`` 序列（query 用该 collection 的 embedding 模型编码）。"""
    if rerank:
        from recall.api import kb_search_core

        result = await kb_search_core(
            SearchRequest(query=question, top_k=max_k, max_tokens=MAX_EVAL_TOKENS)
        )
        return [item.source_uri for item in result.evidence]

    embedding = (await service.embedder.encode([question]))[0]
    hits = await service.store.hybrid_search(
        service.collection,
        dense=embedding.dense,
        sparse_indices=embedding.sparse_indices,
        sparse_values=embedding.sparse_values,
        top_k=RECALL_TOP_K,
        query_filter=effective_filter(Identity(), None),
        limit=max_k,
    )
    uris: list[str] = []
    for hit in hits:
        payload = ChunkPayload.model_validate(hit.payload)
        record = await service.registry.get(payload.doc_id)
        uris.append(record.source_uri if record is not None else "")
    return uris


def render(report: EvalReport, *, show_misses: int = 10) -> str:
    """把汇总渲染成可读文本。"""
    lines = [
        f"collection : {report.collection}",
        f"mode       : {report.mode}",
        f"golden     : {report.golden}（{report.total} 题）",
        "metrics    : "
        + "  ".join(f"Recall@{k}={value:.3f}" for k, value in sorted(report.recall_at.items()))
        + f"  MRR={report.mrr:.3f}",
        f"elapsed    : {report.elapsed_s:.1f}s",
    ]
    misses = [item for item in report.results if not item.hit]
    if misses:
        lines.append(f"未命中 {len(misses)} 题（最多列 {show_misses} 题）：")
        for item in misses[:show_misses]:
            lines.append(f"  - {item.question}")
            lines.append(f"    期望：{item.expected}")
            lines.append(f"    实得：{item.returned[:3]}")
    return "\n".join(lines)


async def _main_async(args: argparse.Namespace) -> int:
    report = await evaluate(args)
    print(render(report))
    if args.output:
        output_path = Path(args.output)
        payload = asdict(report)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"汇总已写入：{output_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
