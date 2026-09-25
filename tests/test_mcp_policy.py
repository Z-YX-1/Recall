"""MCP 工具白名单测试（roadmap R-39 前置件）。

两件事必须同时成立：

1. **可见性**：``tools/list`` 不把无权使用的工具摆到对方面前（扣子官方文档指出工具名/
   说明/参数都占 Agent 上下文并费 Token）；
2. **权限**：``tools/call`` 越权直接拒绝 —— **藏着不等于挡住**，只做 1 是假安全。

默认（空策略）必须**什么都不改变**：未列出的身份拿全部工具。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextvars import Token

import pytest
from fastmcp import Client

from ingest import run_ingest
from recall.api import MCP_TOOL_NAMES, close_service, get_service, mcp
from recall.auth import reset_current_identity, set_current_identity
from recall.mcp_policy import ToolPolicy
from recall.models import Identity
from tests.helpers import IngestEnv, ingest_args, write_note

POLICY = "coze:kb_search|kb_answer"

_NOTE = """\
# RAG 检索

## 混合检索

混合检索把 dense 与 sparse 两路召回结果用 RRF 融合，再交给 bge-reranker-v2-m3 精排。
"""


async def _use_policy(ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch, policy: str) -> None:
    """把策略写进环境并重建服务单例（中间件每次调用现读配置）。"""
    monkeypatch.setenv("RECALL_MCP_TOOL_POLICY", policy)
    monkeypatch.setenv("RECALL_COLLECTION", ingest_env.collection)
    monkeypatch.setenv("RECALL_VAULT_PATH", str(ingest_env.vault))
    monkeypatch.setenv("RECALL_REGISTRY_DB", str(ingest_env.registry_db))
    await close_service()
    await get_service()


@pytest.fixture
async def mcp_env(ingest_env: IngestEnv) -> AsyncIterator[IngestEnv]:
    """用完关掉服务单例，避免策略泄漏到别的用例。"""
    try:
        yield ingest_env
    finally:
        await close_service()


def _as(user: str) -> Token[Identity | None]:
    """把当前身份设成 ``user``，返回复位令牌。"""
    return set_current_identity(Identity(user=user, groups=[]))


# --------------------------------------------------------------------------- 策略本体


def test_empty_policy_is_disabled_and_allows_everything() -> None:
    """空策略 = 不启用；**未列出的用户不受限**（漏配只会多给，不会锁死人）。"""
    policy = ToolPolicy({})

    assert policy.enabled is False
    assert policy.allows("anyone", "kb_ingest") is True
    assert policy.hidden("anyone", ["kb_ingest"]) == []


def test_listed_user_is_limited_to_the_whitelist() -> None:
    """列了白名单的用户只能用白名单里的工具。"""
    policy = ToolPolicy({"coze": frozenset({"kb_search", "kb_answer"})})

    assert policy.enabled is True
    assert policy.allows("coze", "kb_search") is True
    assert policy.allows("coze", "kb_ingest") is False
    assert policy.allows("me", "kb_ingest") is True  # 未列出 ⇒ 不受限
    assert policy.hidden("coze", ["kb_search", "kb_ingest"]) == ["kb_ingest"]


def test_unknown_tool_names_are_detectable() -> None:
    """配置笔误要能发现（否则白名单静默失效，人还以为挡住了）。"""
    policy = ToolPolicy({"coze": frozenset({"kb_search", "kb_serch"})})

    assert policy.unknown_tools(["kb_search", "kb_ingest"]) == ["kb_serch"]


# --------------------------------------------------------------------------- 可见性


async def test_default_policy_exposes_every_tool(
    mcp_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认（空策略）⇒ 4 个工具一个不少，行为与从前完全一致。"""
    await _use_policy(mcp_env, monkeypatch, "")

    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert names == set(MCP_TOOL_NAMES)


async def test_whitelisted_identity_sees_only_its_tools(
    mcp_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单身份只看得到被允许的工具；未列出的身份仍然看得到全部。"""
    await _use_policy(mcp_env, monkeypatch, POLICY)

    token = _as("coze")
    try:
        async with Client(mcp) as client:
            coze_tools = {tool.name for tool in await client.list_tools()}
    finally:
        reset_current_identity(token)

    token = _as("me")
    try:
        async with Client(mcp) as client:
            owner_tools = {tool.name for tool in await client.list_tools()}
    finally:
        reset_current_identity(token)

    assert coze_tools == {"kb_search", "kb_answer"}
    assert owner_tools == set(MCP_TOOL_NAMES)


# --------------------------------------------------------------------------- 权限（第二道闸）


async def test_denied_call_is_rejected_not_silently_empty(
    mcp_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """越权调用必须**明确报错**，不能静默返回空——否则人会以为"这工具没数据"。"""
    await _use_policy(mcp_env, monkeypatch, POLICY)

    token = _as("coze")
    try:
        async with Client(mcp) as client:
            result = await client.call_tool("kb_ingest", {"mode": "update"}, raise_on_error=False)
    finally:
        reset_current_identity(token)

    assert result.is_error is True
    text = result.content[0].text if result.content else ""
    assert "权限" in text
    assert "kb_ingest" in text


async def test_every_tool_outside_the_whitelist_is_denied_on_call(
    mcp_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单之外的工具**逐个**验证（写端点与统计端点都拦住）。"""
    await _use_policy(mcp_env, monkeypatch, POLICY)

    token = _as("coze")
    try:
        async with Client(mcp) as client:
            denied = {
                name: await client.call_tool(name, {}, raise_on_error=False)
                for name in ("kb_ingest", "kb_stats")
            }
    finally:
        reset_current_identity(token)

    for name, result in denied.items():
        assert result.is_error is True, f"{name} 应被拒绝"
        text = result.content[0].text if result.content else ""
        assert "权限" in text, f"{name} 的拒绝理由应说明是权限问题"


async def test_allowed_tool_still_works_for_whitelisted_identity(
    mcp_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """放行的工具照常可用（白名单不是"一律拒绝"）。

    ⚠️ 这里身份必须用 ``me``：``ingest.py`` 目前**硬编码** ``owner="me",
    visibility="private"``（不读 frontmatter），所以**只有 `me` 能看见语料** ——
    换个身份（例如给 Coze 另发一把 token 映射成 ``coze``）会被权限过滤全部挡掉、
    检索恒为空。这条约束已登记为 R-39 的前置事项。
    """
    write_note(mcp_env.vault, "RAG检索.md", _NOTE)
    report = await run_ingest(ingest_args(mcp_env))
    assert report.failed == []
    await _use_policy(mcp_env, monkeypatch, "me:kb_search|kb_answer")

    token = _as("me")
    try:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "kb_search", {"query": "混合检索怎么融合", "top_k": 3}
            )
    finally:
        reset_current_identity(token)

    data = result.structured_content
    assert data is not None and data["evidence"], "白名单内的工具应正常工作"
