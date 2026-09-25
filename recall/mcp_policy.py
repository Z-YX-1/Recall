"""MCP 工具可见性与越权调用控制（roadmap R-39 前置件）。

## 为什么需要

扣子（Coze）官方 MCP 文档明确写着：MCP 提供的**工具名、说明与参数都会占用 Agent 上下文**，
工具越多越容易选错、也越费 Token 与积分（建议每个 Agent 最多 10 个 MCP）。
而本项目对外暴露 4 个工具，其中 ``kb_ingest`` 是**写端点**（能重灌整个知识库）。

**更重要的一条架构约束**：本机只有一份模型（bge-m3 + reranker ≈4.5GB 显存，
tech.md §12）。所以"为公网另起一个实例、用 server-wide 白名单只暴露读工具"这条路
会**双份占显存**（正是 R-23b 那类崩溃的土壤）⇒ 想让"本机 DSH 拿全套工具、
公网来访者只拿读工具"同时成立，只能**按身份区分**。

## 做法

``RECALL_MCP_TOOL_POLICY="coze:kb_search|kb_answer"``（``用户:工具1|工具2``，逗号分隔多条）。

- **未列出的用户不受限**（拿全部工具）⇒ 空配置等于不启用，既不改变既有行为，
  也不会因为漏配某个用户而把人锁死；
- **两道闸**：``tools/list`` 按身份**过滤可见性**（不把写工具摆到对方眼前），
  ``tools/call`` 再**拒绝越权调用**（可见性 ≠ 权限——藏着不等于挡住，所以两道都要）；
- 身份来自 R-40 的 contextvar（同一请求任务内可见），与 REST 侧同源。

⚠️ 越权拒绝是**语义化错误**（``ToolError``），不是静默返回空——静默失败会让人以为
"这个工具没数据"，而真相是"你没权限"。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool, ToolResult
from mcp import types as mt

from recall.auth import current_identity

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """身份 → 可用工具白名单。

    Attributes:
        rules: ``{user: {tool, …}}``；**未出现的用户不受限**（见模块 docstring）。
    """

    rules: Mapping[str, frozenset[str]]

    @property
    def enabled(self) -> bool:
        """是否配置了任何规则（空表 = 不启用）。"""
        return bool(self.rules)

    def allows(self, user: str, tool: str) -> bool:
        """该身份是否可用该工具。

        Args:
            user: 身份 ``user``。
            tool: 工具名。

        Returns:
            未为该用户配置规则时恒为 ``True``；否则要求工具在白名单内。
        """
        allowed = self.rules.get(user)
        return True if allowed is None else tool in allowed

    def hidden(self, user: str, tools: Sequence[str]) -> list[str]:
        """该身份看不到的工具名（用于启动/调用日志，便于排查"工具怎么没了"）。"""
        return sorted(tool for tool in tools if not self.allows(user, tool))

    def unknown_tools(self, known: Sequence[str]) -> list[str]:
        """规则里写了但项目并不存在的工具名（配置笔误要能发现）。"""
        known_set = set(known)
        return sorted(
            {tool for allowed in self.rules.values() for tool in allowed if tool not in known_set}
        )


class ToolPolicyMiddleware(Middleware):
    """按身份过滤 ``tools/list`` 并拒绝越权 ``tools/call``。

    策略**每次请求**从服务单例的配置读取（与证据门槛同源），因此改配置只需重启进程，
    不需要重新构造 FastMCP 实例；测试也能通过重建服务单例来切换策略。
    """

    def __init__(self, resolve_policy: Callable[[], ToolPolicy]) -> None:
        """记录策略解析器。

        Args:
            resolve_policy: 无参可调用对象，返回当前的 :class:`ToolPolicy`。
        """
        self._resolve = resolve_policy

    def _policy(self) -> ToolPolicy:
        """取当前策略。"""
        return self._resolve()

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """把该身份无权使用的工具从列表里摘掉。"""
        tools = list(await call_next(context))
        policy = self._policy()
        if not policy.enabled:
            return tools
        user = current_identity().user
        kept = [tool for tool in tools if policy.allows(user, tool.name)]
        if len(kept) != len(tools):
            logger.info(
                "mcp.tools_hidden",
                extra={"user": user, "hidden": policy.hidden(user, [t.name for t in tools])},
            )
        return kept

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """越权调用直接拒绝（**可见性 ≠ 权限**，两道闸都要有）。"""
        policy = self._policy()
        if policy.enabled:
            user = current_identity().user
            name = context.message.name
            if not policy.allows(user, name):
                logger.warning(
                    "mcp.tool_denied", extra={"user": user, "tool": name}
                )
                raise ToolError(
                    f"当前身份（{user}）没有调用工具 {name} 的权限。"
                    "如需使用，请在服务端把它加入该身份的 RECALL_MCP_TOOL_POLICY 白名单。"
                )
        return await call_next(context)
