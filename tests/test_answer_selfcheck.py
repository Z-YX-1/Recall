"""方案 D（roadmap R-47，2026-10-03 项目工程师批准）：**拒答时的自检复核**。

**这一层要解决什么**：生成那一次要让模型同时干"理解问题、判断证据、决定答不答"三件事，
实测同一输入（`temperature=0.0`）会在"答 / 拒"之间**横跳** —— 目标问题「切分粒度对召回的影响」
旧提示词 0/3 答，改提示词后 6/8 答，线上 9/10 答，**但仍有约 1/10 会错答成"笔记里没有"**。
拆出一次**只问一件事**的自检（"这段证据有没有直接回答该问题"）后判得准得多：实测样例里
裁判自己就把那句正面回答原文挑了出来。

用例全部**不依赖 GPU / Qdrant / 真 DeepSeek**：直接调 :func:`recall.api._retry_when_declined`，
用桩客户端记录"调了几次、带了什么提示词"。

⚠️ **契约边界**：``_retry_when_declined`` **不负责第一次生成**（那是 ``kb_answer_core`` 做的）——
它只拿到"第一次的返回体"，决定要不要**重生成一次**。所以桩里的 ``complete_json``
只在**重生成**时才会被调到（正常回答应当**一次都不调**）。
"""

from __future__ import annotations

from typing import Any

import pytest

from recall.api import _retry_when_declined
from recall.llm import (
    DECLINE_MARKERS,
    RETRY_INSTRUCTION,
    DeepSeekClient,
    looks_like_decline,
    parse_answered,
)
from recall.models import Evidence

_DECLINE: dict[str, Any] = {"answer": "笔记里只提及该术语、没有解释其原理。", "citations": []}
_NORMAL: dict[str, Any] = {"answer": "粒度决定召回质量 [1]。", "citations": [1]}
_REGENERATED: dict[str, Any] = {
    "answer": "粒度是影响 recall 最大的单一参数 [1]。",
    "citations": [1],
}


class _StubLlm(DeepSeekClient):
    """桩：``complete_json`` 只在**重生成**时被调；自检返回预设结论。"""

    def __init__(self, *, answered: object = False, retry: object = None) -> None:
        super().__init__(api_key=None, base_url="http://stub", model="stub")
        self._answered = answered
        self._retry = _REGENERATED if retry is None else retry
        self.prompts: list[str] = []
        self.self_checks: list[str] = []

    @property
    def available(self) -> bool:
        return True

    async def complete_json(
        self, prompt: str, *, system: str = "", max_tokens: int = 0
    ) -> dict[str, Any]:
        del system, max_tokens
        self.prompts.append(prompt)
        if isinstance(self._retry, Exception):
            raise self._retry
        assert isinstance(self._retry, dict)
        return self._retry

    async def verify_evidence_answers(self, question: str, evidence_text: str) -> bool:
        del evidence_text
        self.self_checks.append(question)
        if isinstance(self._answered, Exception):
            raise self._answered
        assert isinstance(self._answered, bool)
        return self._answered


class _SelfCheckStub(DeepSeekClient):
    """只桩 ``complete_json``，**保留真实的** ``verify_evidence_answers`` 与自洽检查。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(api_key=None, base_url="http://stub", model="stub")
        self._payload = payload

    @property
    def available(self) -> bool:
        return True

    async def complete_json(
        self, prompt: str, *, system: str = "", max_tokens: int = 0
    ) -> dict[str, Any]:
        del prompt, system, max_tokens
        return self._payload


async def test_self_check_true_without_a_quoted_sentence_counts_as_not_answered() -> None:
    """裁判说「回答了」却**引不出原句** ⇒ 按「没回答」处理（廉价的自洽检查）。

    动机（2026-10-03 实测）：把 10 题 `mentioned` 组跑下来，裁判**宽松放行**了一题（GraphRAG），
    而它本该拒答；加上这道检查后该组回到 **0 硬答**，同时目标问题仍 **8/8 答** —— 两边都不塌。
    """
    blank = _SelfCheckStub({"answered": True, "sentence": "   "})
    assert await blank.verify_evidence_answers("问题", "证据") is False

    quoted = _SelfCheckStub({"answered": True, "sentence": "粒度是影响 recall 最大的单一参数"})
    assert await quoted.verify_evidence_answers("问题", "证据") is True

    denied = _SelfCheckStub({"answered": False, "sentence": ""})
    assert await denied.verify_evidence_answers("问题", "证据") is False


def _evidence(text: str = "粒度是整条 RAG 链路里影响 recall 最大的单一参数") -> Evidence:
    return Evidence(
        ref_id="1", source_uri="笔记.md", heading_path="切分 > 痛点", text=text, score=0.95
    )


async def _run(
    llm: _StubLlm, payload: dict[str, Any], *, evidence: list[Evidence] | None = None
) -> dict[str, Any]:
    """跑一次复核，返回最终采用的返回体。"""
    return await _retry_when_declined(
        llm,
        "原始提示词",
        payload,
        query="切分粒度对召回的影响是什么样的",
        evidence=[_evidence()] if evidence is None else evidence,
        trace_id="t-test",
    )


# ── 判据本身 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "answer",
    [
        "笔记里只提及该术语、没有解释其原理。",
        "证据只是提到它，没有展开说明。",
        "笔记里没有相关内容。",
        "该术语仅列出，未解释。",
    ],
)
def test_looks_like_decline_catches_the_observed_phrasings(answer: str) -> None:
    assert looks_like_decline(answer) is True


def test_looks_like_decline_is_false_for_a_real_answer() -> None:
    assert looks_like_decline(str(_NORMAL["answer"])) is False


def test_decline_markers_are_not_empty() -> None:
    """空表会让方案 D 永不触发（静默失效），必须有断言。"""
    assert DECLINE_MARKERS


@pytest.mark.parametrize(
    ("raw", "expected"), [(True, True), (False, False), ("true", True), ("否", False)]
)
def test_parse_answered_tolerates_bool_and_string(raw: object, expected: bool) -> None:
    assert parse_answered({"answered": raw}) is expected


def test_parse_answered_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_answered({"answered": 3.14})


# ── 触发条件 ─────────────────────────────────────────────────────────────────


async def test_normal_answer_never_triggers_the_self_check() -> None:
    """🔴 **正常回答必须零额外开销** —— 这是方案 D 能被接受的前提（实测约 9/10 的题走这条）。"""
    llm = _StubLlm()

    payload = await _run(llm, _NORMAL)

    assert payload == _NORMAL
    assert llm.self_checks == [], "正常回答不该调自检（会白白多 0.72s）"
    assert llm.prompts == [], "正常回答不该重生成"


async def test_no_evidence_skips_the_self_check() -> None:
    """没有证据就没什么可复核的（此时拒答本就正确）。"""
    llm = _StubLlm()

    payload = await _run(llm, _DECLINE, evidence=[])

    assert payload == _DECLINE
    assert llm.self_checks == []


# ── 自检为"是" ⇒ 重生成（方案 D 的正路）────────────────────────────────────────


async def test_self_check_true_regenerates_with_the_retry_instruction() -> None:
    llm = _StubLlm(answered=True)

    payload = await _run(llm, _DECLINE)

    assert payload == _REGENERATED
    assert len(llm.prompts) == 1
    assert llm.prompts[0].startswith("原始提示词"), "必须在原提示词基础上追加，不能丢掉证据"
    assert RETRY_INSTRUCTION in llm.prompts[0]
    assert llm.self_checks == ["切分粒度对召回的影响是什么样的"]


# ── 自检为"否" ⇒ 保留原拒答（R-47 不被放回来）────────────────────────────────


async def test_self_check_false_keeps_the_original_decline() -> None:
    """真·只提及的题**就该**拒答 —— 自检说不算回答时必须原样保留。"""
    llm = _StubLlm(answered=False)

    payload = await _run(llm, _DECLINE)

    assert payload == _DECLINE
    assert llm.prompts == [], "自检为否时不该重生成"


# ── 降级：复核自己出问题，绝不能弄丢用户的回答 ───────────────────────────────


async def test_self_check_failure_keeps_the_original_answer() -> None:
    llm = _StubLlm(answered=RuntimeError("self check down"))

    payload = await _run(llm, _DECLINE)

    assert payload == _DECLINE


async def test_regeneration_failure_keeps_the_original_answer() -> None:
    llm = _StubLlm(answered=True, retry=RuntimeError("llm down"))

    payload = await _run(llm, _DECLINE)

    assert payload == _DECLINE
