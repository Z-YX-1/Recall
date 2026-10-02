"""R-47：「提了名没解释」的规则必须在**两条作答路径**上都存在，且在胖端点那条路上**只出现一次**
（roadmap R-47，2026-09-26 批准；2026-10-03 项目工程师拍板 A+B 修订）。

为什么值得一组专门用例：这条规则分散在**两条作答路径**上 —— 胖端点（服务端自己调 LLM）
与瘦路径（DSH Agent 按 skill 组装）。两条路径各自维护一份模板，**任何一处漏改都是静默失效**：
用户看到的只是"模型又开始硬答了"，没有任何报错。

⚠️ 2026-10-03 修订了两件事，本组用例相应改为钉住**新**契约：

- **B 去重**：规则在胖端点那条路上**只能有一份**（系统提示词）。此前 `FIDELITY_RULES`
  与 `SYSTEM_PROMPT` 各一份、而生产代码两条都发 ⇒ 拒答规则被放大到压过"有相关内容就必须作答"。
- **A 从属**：第 2 条（有相关内容就作答）**优先**；只有证据**整体上**只是提名才拒答；
  并澄清"问题里的术语"指问题真正在问的那个词、"没给定义 ≠ 问题没被回答"。

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


def test_mentioned_rule_lives_only_in_the_system_prompt() -> None:
    """🔴 **去重（2026-10-03 项目工程师拍板 B）**：这条规则只能有**一份**进模型。

    实测事故：胖端点"证据里有正面回答却仍答『只提及未解释』"（问题「切分粒度对召回的影响」，
    检索完全正确、最高分证据原文即答案）。根因之一是规则被写了两遍 —— `FIDELITY_RULES`
    进**用户** prompt、`SYSTEM_PROMPT` 进**系统** prompt，而生产代码
    （`api.py::kb_answer_core`）**两条都发** ⇒ 拒答规则被放大到压过"有相关内容就必须作答"。

    保留在**系统提示词**里，是因为本模块早就实测过"拒答规则只写在用户提示词末尾会被忽略"。
    """
    assert "只是提到" not in FIDELITY_RULES, "用户 prompt 里不该再有第二份（会放大拒答）"
    assert "只是提到" in SYSTEM_PROMPT


def test_assemble_prompt_does_not_duplicate_the_refusal_rule() -> None:
    """``assemble()`` 产出的用户 prompt **不得**再内嵌那条拒答规则（同一条规则的第二次投喂）。"""
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

    assert "只是提到" not in prompt
    assert "证据" in prompt, "规则块本身还要在（只是少了那一句）"


def test_system_prompt_subordinates_the_refusal_rule_to_answering() -> None:
    """🔴 **从属关系（2026-10-03 项目工程师拍板 A）**：第 2 条优先 + 两条澄清必须在场。

    否则模型会把**证据里顺带出现的另一个术语**（实测是 `chunk_size`）当成"问题里的术语"，
    再以"没给它下定义"为由整段拒答 —— 而问题问的是**影响**、不是**定义**。
    """
    assert "第 2 条" in SYSTEM_PROMPT and "优先" in SYSTEM_PROMPT
    assert "整体上" in SYSTEM_PROMPT, "必须限定为「证据整体上只是提名」才拒答"
    assert "问题真正在问的那个词" in SYSTEM_PROMPT, "必须澄清「问题里的术语」指哪个词"
    assert "没给出某个术语的定义" in SYSTEM_PROMPT, "必须澄清「没给定义 ≠ 问题没被回答」"


def test_system_prompt_carries_the_mentioned_rule() -> None:
    """胖端点的系统提示词必须写明这条规则（它是现在**唯一**的那份）。"""
    assert "只是提到" in SYSTEM_PROMPT
    assert "没有解释" in SYSTEM_PROMPT


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
