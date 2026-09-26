"""R-48 的触发策略契约：**默认先查 + 未查必披露**（roadmap R-48，2026-09-26 批准）。

为什么值得钉住：这条策略**不是代码分支**，而是**四处提示词/描述文本**的约定 ——
MCP 服务端 instructions、`kb_search` 工具描述、`skill/recall-assembly.md`、
`ASSEMBLY.md`（无 skill 时的兜底）。它们各自被不同的消费者读到：

| 落地处 | 谁读到 |
| :--- | :--- |
| `FastMCP(instructions=…)` | 连上 `/mcp` 的客户端（DSH、Coze）——**每次会话都在上下文里** |
| `kb_search` 工具描述 | 模型做**工具选择**时看的那段文字（决定"要不要查"） |
| `skill/recall-assembly.md` | DSH 技能目录 + 技能正文 |
| `ASSEMBLY.md` | 会话里没加载 skill 时的兜底 |

任一处漏改 ⇒ 触发策略静默退化（用户看到的只是"agent 又开始不查库直接联网了"），
与 R-45「同源同形」、R-47 四处模板是同一类风险，故用断言盯住。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from recall.api import mcp

REPO_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_FIRST_MARKERS = ("默认先查", "默认动作")
"""两处措辞按各自文体略不同（skill 用"默认先查"、工具描述用"默认动作"）。"""

_DISCLOSE_MARKERS = ("没有查你的知识库", "没查知识库", "未查知识库")
"""未检索就必须披露的那句话。"""


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _tool_description(name: str) -> str:
    """取某个 MCP 工具的**实际描述文本**（模型做工具选择时读到的就是它）。

    ⚠️ 不能读 ``kb_search.__doc__``：``@mcp.tool`` 装饰后拿到的不再是原始函数对象，
    文档串要经 :meth:`FastMCP.get_tools` 取 —— 一次断言写法错误会让人以为契约没生效。
    """
    tools = asyncio.run(mcp.get_tools())
    tool = tools.get(name)
    assert tool is not None, f"未注册的 MCP 工具：{name}"
    return tool.description or ""


def test_mcp_instructions_carry_the_trigger_policy() -> None:
    """服务端 instructions 是最稳的一处：连上来的客户端每次都读得到。"""
    instructions = mcp.instructions or ""

    assert any(marker in instructions for marker in _DEFAULT_FIRST_MARKERS)
    assert any(marker in instructions for marker in _DISCLOSE_MARKERS)


def test_kb_search_description_carries_the_trigger_policy() -> None:
    """工具描述是模型**决定要不要调**时唯一读到的文字，必须自带"默认先查"。"""
    description = _tool_description("kb_search")

    assert any(marker in description for marker in _DEFAULT_FIRST_MARKERS)
    assert any(marker in description for marker in _DISCLOSE_MARKERS)
    assert "情形 B" in description, "应把「只提到未解释」的处置指回组装规范"


def test_kb_answer_description_states_it_always_retrieves() -> None:
    """胖端点自己检索 ⇒ 调用方不需要披露义务，描述里要写清这个区别。"""
    assert "总是自己检索" in _tool_description("kb_answer")


def test_skill_and_fallback_copy_carry_the_trigger_policy() -> None:
    """Agent 侧两份副本都要有：`skill/` 是源头，`ASSEMBLY.md` 是兜底。"""
    for name in ("skill/recall-assembly.md", "ASSEMBLY.md"):
        text = _read(name)
        assert "触发策略" in text, f"{name} 缺少触发策略一节"
        assert any(marker in text for marker in _DEFAULT_FIRST_MARKERS), name
        assert any(marker in text for marker in _DISCLOSE_MARKERS), name


def test_trigger_policy_keeps_the_skip_case_narrow() -> None:
    """"可以跳过"必须附带"完全无关"这个窄条件，否则会退化成"什么都不查"。"""
    for name in ("skill/recall-assembly.md", "ASSEMBLY.md"):
        text = _read(name)
        assert "完全无关" in text, f"{name} 未限定跳过条件"
