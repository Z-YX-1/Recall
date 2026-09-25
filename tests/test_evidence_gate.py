"""证据门槛测试（roadmap R-42 阶段 1；`eval/BASELINE.md` §7）。

门槛 = "精排**最高分**不足就判『笔记里没有』"。三条必须钉住的性质：

1. **默认关闭**（``RECALL_EVIDENCE_MIN_SCORE=0``）⇒ 行为与从前**完全一致**——
   这是一条会改变可观察行为的开关，默认值必须是"不改变任何东西"；
2. **命中时证据带逐字不变**——它是**门槛**不是**裁剪**。按分数逐条删证据会在 t=0.20
   就把 Recall@10 打到 96.67%（黄金集里有题目的期望来源排第 7 名）；
3. **胖端点拿到空证据时不联系 LLM**：既守住"不硬答"，也顺带省下 DeepSeek 额度。

⚠️ 测试**自校准**：先跑一次不带门槛的检索量出真实 top1，再据此设阈值 ——
不把具体分数写死在断言里（换模型/换语料都不会假红）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from ingest import run_ingest
from recall.api import Service, close_service, get_service, kb_answer_core, kb_search_core
from recall.llm import DeepSeekClient
from recall.models import AnswerRequest, SearchRequest
from tests.helpers import IngestEnv, ingest_args, write_note

_RAG_NOTE = """\
# RAG 检索

## 混合检索

混合检索把 dense 与 sparse 两路召回结果用 RRF 融合，再交给 bge-reranker-v2-m3 精排。
检索质量主要取决于切分粒度，必须用 golden QA 评测校准。
"""

_QUESTION = "混合检索把两路召回结果怎么融合"


class _ForbiddenLlm(DeepSeekClient):
    """被调用即失败：用来证明门槛生效时**根本不会**联系 LLM。"""

    def __init__(self) -> None:
        super().__init__(api_key=None, base_url="http://stub", model="stub")
        self.calls = 0

    @property
    def available(self) -> bool:
        """桩恒可用（避免走"未配置"分支）。"""
        return True

    async def complete_json(
        self, prompt: str, *, system: str = "", max_tokens: int = 0
    ) -> dict[str, Any]:
        """记一次调用并立刻失败。"""
        del system, max_tokens
        self.calls += 1
        raise AssertionError(f"门槛已生效，不该调用 LLM：{prompt[:40]}…")


async def _prepare_corpus(ingest_env: IngestEnv) -> None:
    write_note(ingest_env.vault, "RAG检索.md", _RAG_NOTE)
    report = await run_ingest(ingest_args(ingest_env))
    assert report.indexed_docs == 1


async def _rebuild(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch, threshold: float
) -> None:
    """把阈值写进环境并**重建服务单例**（服务持有 settings 快照）。"""
    monkeypatch.setenv("RECALL_EVIDENCE_MIN_SCORE", str(threshold))
    monkeypatch.setenv("RECALL_COLLECTION", ingest_env.collection)
    await close_service()
    await get_service()


@pytest.fixture
async def corpus(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[IngestEnv]:
    """灌好一篇笔记的隔离环境，用完关掉服务单例。"""
    await _prepare_corpus(ingest_env)
    try:
        yield ingest_env
    finally:
        await close_service()


async def test_gate_is_off_by_default(
    corpus: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认 ``0.0`` ⇒ 不做门槛判定（升级不改变既有行为）。"""
    monkeypatch.delenv("RECALL_EVIDENCE_MIN_SCORE", raising=False)
    await _rebuild(corpus, monkeypatch, 0.0)
    service = await get_service()
    assert isinstance(service, Service)
    assert service.settings.evidence_min_score == 0.0

    result = await kb_search_core(SearchRequest(query=_QUESTION, top_k=3))

    assert result.evidence, "默认关闭时必须有证据"


async def test_gate_returns_empty_pack_below_threshold(
    corpus: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """阈值高于实际最高分 ⇒ 返回**空证据包**（判"笔记里没有"）。"""
    await _rebuild(corpus, monkeypatch, 0.0)
    baseline = await kb_search_core(SearchRequest(query=_QUESTION, top_k=3))
    assert baseline.evidence
    top_score = baseline.evidence[0].score

    await _rebuild(corpus, monkeypatch, min(top_score + 0.05, 1.0))
    gated = await kb_search_core(SearchRequest(query=_QUESTION, top_k=3))

    assert gated.evidence == []
    assert gated.references == []


async def test_gate_at_exactly_the_top_score_keeps_the_pack_identical(
    corpus: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """边界 ``score == threshold`` ⇒ **保留**；且证据带与不带门槛时**逐字相同**。

    后一条断言是"门槛 ≠ 裁剪"的证明：它只决定答不答，绝不动证据带。
    """
    await _rebuild(corpus, monkeypatch, 0.0)
    baseline = await kb_search_core(SearchRequest(query=_QUESTION, top_k=3))
    top_score = baseline.evidence[0].score

    await _rebuild(corpus, monkeypatch, top_score)
    kept = await kb_search_core(SearchRequest(query=_QUESTION, top_k=3))

    assert kept.evidence, "恰好等于阈值应保留（判定用 >=）"
    assert kept == baseline, "门槛不得改动证据带——它不是裁剪"


async def test_gate_makes_fat_endpoint_decline_without_calling_llm(
    corpus: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """门槛拦截时，胖端点直接答"笔记里没有"，且**一次 LLM 都不调**。

    这既是"不硬答"的守门，也顺带证明：笔记外问题不会消耗 DeepSeek 额度。
    """
    await _rebuild(corpus, monkeypatch, 1.0)
    service = await get_service()
    assert isinstance(service, Service)
    forbidden = _ForbiddenLlm()
    service.llm = forbidden

    answer = await kb_answer_core(AnswerRequest(query=_QUESTION))

    assert "笔记里没有" in answer.answer
    assert answer.citations == []
    assert answer.references == []
    assert forbidden.calls == 0
