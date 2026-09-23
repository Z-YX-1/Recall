"""RAG 质量评测：Ragas faithfulness / answer relevancy（tech.md §10；roadmap R-32）。

评测对象是 **kb_answer 胖端点的真实回答**：先真跑一遍"检索 → 组装 → DeepSeek 生成"，
再由 Ragas 用 DeepSeek 当裁判打分。同时校验 ``citations`` 与 ``references``
一一对应（code_standards §8 的硬性要求）。

需要 ``DEEPSEEK_API_KEY``（生成与裁判共用同一个 key）。

用法::

    python eval/eval_ragas.py --limit 10
    python eval/eval_ragas.py --limit 10 --output eval/ragas_baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import logging
import sys
import time
from collections.abc import Coroutine
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypeVar, cast

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:  # 直接运行脚本时保证能 import recall
    sys.path.insert(0, str(PROJECT_ROOT))

from openai import AsyncOpenAI  # noqa: E402
from ragas import EvaluationDataset, SingleTurnSample, aevaluate  # noqa: E402
from ragas.dataset_schema import EvaluationResult  # noqa: E402
from ragas.embeddings.base import BaseRagasEmbedding  # noqa: E402
from ragas.llms import llm_factory  # noqa: E402
from ragas.metrics import AnswerRelevancy, Faithfulness  # noqa: E402
from ragas.metrics.base import Metric  # noqa: E402

from recall.api import close_service, get_service, kb_answer_core, kb_search_core  # noqa: E402
from recall.config import Settings  # noqa: E402
from recall.embedder import Embedder  # noqa: E402
from recall.models import AnswerRequest, SearchRequest  # noqa: E402

logger = logging.getLogger("recall.eval.ragas")

JUDGE_TIMEOUT = 120.0
"""裁判 LLM 单次请求超时（faithfulness 会对每条陈述发一次请求）。"""

METRIC_COLUMNS = ("faithfulness", "answer_relevancy")

_T = TypeVar("_T")


class LocalBgeEmbedding(BaseRagasEmbedding):
    """把 Recall 本地 bge-m3（:class:`~recall.embedder.Embedder`）接成 ragas 的 embedding 接口。

    ``AnswerRelevancy`` 需要 embedding 来算"逆问题 ↔ 原问题"的相似度；用本地模型既省一次
    云调用，也符合"笔记内容不出域"的红线（tech.md §15 决策 7）。
    """

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
        """异步编码单条文本。"""
        del kwargs
        embeddings = await self._embedder.encode([text])
        return embeddings[0].dense if embeddings else []

    async def aembed_texts(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        """异步批量编码。"""
        del kwargs
        return [item.dense for item in await self._embedder.encode(texts)]

    def embed_text(self, text: str, **kwargs: Any) -> list[float]:
        """同步编码单条文本（供非异步调用方使用）。"""
        del kwargs
        return _run_sync(self.aembed_text(text))

    def embed_texts(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        """同步批量编码。"""
        del kwargs
        return _run_sync(self.aembed_texts(texts))

    # ⚠️ 下面四个方法是 ragas 的 **另一套** embedding 接口（BaseRagasEmbeddings 用的是
    # query/documents 命名，BaseRagasEmbedding 用的是 text/texts）。
    # AnswerRelevancy 走的是前者——只实现 text 那一套会报
    # `AttributeError: 'LocalBgeEmbedding' object has no attribute 'embed_query'`，
    # 指标直接缺席（2026-09-23 实测）。两套都实现才能覆盖不同 ragas 版本。

    async def aembed_query(self, text: str, **kwargs: Any) -> list[float]:
        """异步编码单条 query。"""
        return await self.aembed_text(text, **kwargs)

    async def aembed_documents(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        """异步编码一批文档。"""
        return await self.aembed_texts(texts, **kwargs)

    def embed_query(self, text: str, **kwargs: Any) -> list[float]:
        """同步编码单条 query。"""
        del kwargs
        return _run_sync(self.aembed_query(text))

    def embed_documents(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        """同步编码一批文档。"""
        del kwargs
        return _run_sync(self.aembed_documents(texts))


def _run_sync(coroutine: Coroutine[Any, Any, _T]) -> _T:
    """在独立线程的事件循环里执行协程（避免与已运行的事件循环冲突）。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


@dataclass(slots=True)
class AnswerRecord:
    """一题的生成结果与引用一致性。"""

    question: str
    answer: str
    citations: list[int]
    references: list[dict[str, str]]
    contexts: list[str]
    citations_consistent: bool
    citation_note: str = ""


@dataclass(slots=True)
class RagasReport:
    """一次 RAG 质量评测的汇总。"""

    total: int
    citation_consistent_rate: float
    scores: dict[str, float] = field(default_factory=dict)
    elapsed_s: float = 0.0
    records: list[AnswerRecord] = field(default_factory=list)


def build_parser() -> argparse.ArgumentParser:
    """构造评测命令行解析器。"""
    parser = argparse.ArgumentParser(description="Recall RAG 质量评测（Ragas）")
    parser.add_argument(
        "--golden", default=str(PROJECT_ROOT / "eval" / "golden_set.jsonl"), help="黄金集路径"
    )
    parser.add_argument("--limit", type=int, default=10, help="评测题数（控制裁判调用成本）")
    parser.add_argument("--top-k", type=int, default=5, help="每题用于评测的证据条数")
    parser.add_argument("--max-tokens", type=int, default=3000, help="证据 token 预算")
    parser.add_argument("--output", default=None, help="把汇总 JSON 写到该路径")
    parser.add_argument("--log-level", default="WARNING", help="日志级别")
    return parser


def check_citation_consistency(
    answer: str, citations: list[int], references: list[dict[str, str]]
) -> tuple[bool, str]:
    """校验 ``citations`` 与 ``references`` 一一对应（code_standards §8）。

    Args:
        answer: 回答正文。
        citations: 胖端点返回的引用编号。
        references: 与 ``[n]`` 一一对应的出处数组（索引 = n-1）。

    Returns:
        ``(是否一致, 说明)``。
    """
    problems: list[str] = []
    for number in citations:
        if not 1 <= number <= len(references):
            problems.append(f"[{number}] 越界（共 {len(references)} 条出处）")
        elif not references[number - 1].get("source_uri"):
            problems.append(f"[{number}] 缺少 source_uri")
        if f"[{number}]" not in answer:
            problems.append(f"citations 声明了 [{number}]，但正文没有该角标")
    return (not problems), "；".join(problems)


async def collect_answers(args: argparse.Namespace) -> list[AnswerRecord]:
    """对黄金集逐题真跑 kb_answer，收集回答与引用。"""
    from eval.eval_retrieval import load_golden

    await close_service()
    await get_service()

    items = load_golden(Path(args.golden))[: args.limit]
    records: list[AnswerRecord] = []
    for index, item in enumerate(items, start=1):
        question = str(item["question"])
        result = await kb_answer_core(AnswerRequest(query=question, max_tokens=args.max_tokens))
        consistent, note = check_citation_consistency(
            result.answer, result.citations, result.references
        )
        records.append(
            AnswerRecord(
                question=question,
                answer=result.answer,
                citations=result.citations,
                references=result.references,
                contexts=[],
                citations_consistent=consistent,
                citation_note=note,
            )
        )
        logger.info("ragas.collected", extra={"index": index, "total": len(items)})
    return records


async def collect_contexts(records: list[AnswerRecord], top_k: int) -> None:
    """为每题补上检索到的证据原文（faithfulness 的判据）。"""
    for record in records:
        search = await kb_search_core(SearchRequest(query=record.question, top_k=top_k))
        record.contexts = [item.text for item in search.evidence]


def _mean_scores(frame: Any) -> dict[str, float]:  # noqa: ANN401 - pandas DataFrame
    """从 Ragas 结果表里取各指标均值（缺列 / 全 NaN 时跳过）。"""
    scores: dict[str, float] = {}
    for column in METRIC_COLUMNS:
        if column not in frame.columns:
            continue
        value = frame[column].mean()
        if value == value:  # 过滤 NaN
            scores[column] = float(value)
    return scores


async def evaluate_rag_quality(args: argparse.Namespace) -> RagasReport:
    """跑一遍完整评测：生成 → 引用一致性 → Ragas 打分。"""
    settings = Settings.from_env()
    if not settings.deepseek_api_key:
        raise SystemExit("缺少 DEEPSEEK_API_KEY：Ragas 需要裁判 LLM（写进 .env 后重跑）")

    started = time.perf_counter()
    records = await collect_answers(args)
    await collect_contexts(records, args.top_k)

    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=JUDGE_TIMEOUT,
        max_retries=2,
    )
    judge = llm_factory(settings.deepseek_model, provider="openai", client=client)
    embedding = LocalBgeEmbedding(Embedder())

    dataset = EvaluationDataset(
        samples=[
            SingleTurnSample(
                user_input=record.question,
                response=record.answer,
                retrieved_contexts=record.contexts or ["（无证据）"],
            )
            for record in records
        ]
    )
    # ⚠️ 用 ragas.metrics 的传统 Metric 类：aevaluate 的类型签名接受 Metric，
    # 而 ragas.metrics.collections 的新类不是 Metric 子类（0.4.3 实测）。
    metrics: list[Metric] = [Faithfulness(), AnswerRelevancy()]
    result = cast(
        EvaluationResult,
        await aevaluate(
            dataset=dataset,
            metrics=metrics,
            llm=judge,
            embeddings=embedding,
            show_progress=False,
            raise_exceptions=False,
        ),
    )
    scores = _mean_scores(result.to_pandas())
    await close_service()

    consistent = sum(1 for record in records if record.citations_consistent)
    return RagasReport(
        total=len(records),
        citation_consistent_rate=consistent / len(records) if records else 0.0,
        scores=scores,
        elapsed_s=time.perf_counter() - started,
        records=records,
    )


def render(report: RagasReport) -> str:
    """把汇总渲染成可读文本。"""
    lines = [
        f"题数       : {report.total}",
        f"引用一致性 : {report.citation_consistent_rate:.3f}",
        "Ragas 指标 : "
        + "  ".join(f"{key}={value:.3f}" for key, value in sorted(report.scores.items())),
        f"耗时       : {report.elapsed_s:.1f}s",
    ]
    problems = [record for record in report.records if not record.citations_consistent]
    if problems:
        lines.append(f"引用不一致 {len(problems)} 题：")
        for record in problems[:5]:
            lines.append(f"  - {record.question} ⇒ {record.citation_note}")
    return "\n".join(lines)


async def _main_async(args: argparse.Namespace) -> int:
    report = await evaluate_rag_quality(args)
    print(render(report))
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
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
