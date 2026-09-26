"""R-47：「提了名没解释」的规则必须在**四个**落地处同时存在（roadmap R-47，2026-09-26 批准）。

为什么值得一组专门用例：这条规则分散在**两条作答路径**上 —— 胖端点（服务端自己调 LLM）
与瘦路径（DSH Agent 按 skill 组装）。两条路径各自维护一份模板，**任何一处漏改都是静默失效**：
用户看到的只是"模型又开始硬答了"，没有任何报错。

这正是 R-45「同源同形」那条教训的再次应用 —— **同一规则的多处副本必须有断言盯住一致性**。

⚠️ 判据是**「证据解释了问题吗」**，不是「证据讲的是不是这个问题」。二者必须能区分：
2026-09-24 曾试图用"证据只是相邻主题时必须点名"来治硬答，结果**过度拒答**复发
（问了笔记里写过的"切分器粒度怎么选"，却被答成"没有相关内容"）⇒ 那次回退了。
本组用例同时钉住"新规则不得把那条回退过的措辞带回来"。
"""

from __future__ import annotations

from pathlib import Path

from recall.assemble import FIDELITY_RULES, assemble
from recall.llm import SYSTEM_PROMPT
from recall.models import Evidence

REPO_ROOT = Path(__file__).resolve().parent.parent

_MENTION_MARKERS = ("只是提到", "只提及", "提了名")
"""出现任一即可（四份副本的措辞按各自文体略有不同）。"""


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_fidelity_rules_carry_the_mentioned_rule() -> None:
    """瘦路径的规则常量必须写明"只是提到、没有解释"时的处置。"""
    assert "只是提到" in FIDELITY_RULES
    assert "没有解释" in FIDELITY_RULES


def test_system_prompt_carries_the_mentioned_rule() -> None:
    """胖端点的系统提示词必须写明同一条规则（两条路径不许分叉）。"""
    assert "只是提到" in SYSTEM_PROMPT
    assert "没有解释" in SYSTEM_PROMPT


def test_assemble_prompt_reaches_the_model_with_the_rule() -> None:
    """规则不只是躺在常量里 —— 它必须真的进入生成用 prompt。"""
    evidence = [
        Evidence(
            ref_id="1",
            source_uri="笔记.md",
            heading_path="上下文工程",
            text="长上下文训练（YaRN / LongRoPE 位置外推）",
            score=0.9,
        )
    ]

    prompt, _ = assemble(evidence)

    assert "只是提到" in prompt


def test_skill_and_fallback_copy_both_carry_the_rule() -> None:
    """Agent 侧的两份副本都要有：`skill/` 是源头，`ASSEMBLY.md` 是没有 skill 时的兜底。"""
    skill = _read("skill/recall-assembly.md")
    fallback = _read("ASSEMBLY.md")

    for name, text in (("skill/recall-assembly.md", skill), ("ASSEMBLY.md", fallback)):
        assert any(marker in text for marker in _MENTION_MARKERS), f"{name} 缺少规则"
        assert "提了名" in text, f"{name} 缺少「情形 B：提了名没解释」"


def test_rule_distinguishes_mentioned_from_the_reverted_adjacent_wording() -> None:
    """不得把 2026-09-24 回退过的"相邻主题必须点名"措辞带回规则常量。

    两者判据不同：**「证据解释了问题吗」** vs **「证据讲的是这个问题吗」**。后者靠提示词
    稳不住（实测会过度拒答），故只在文档里作为历史说明保留，不进常量。
    """
    assert "相邻主题" not in FIDELITY_RULES
