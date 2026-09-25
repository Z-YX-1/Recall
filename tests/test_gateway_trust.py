"""公网接入的两件仓库侧前置（roadmap R-39 待办 B / C）。

**待办 B —— 审计要能分辨远程来源，但不能被伪造。**
隧道 / 反代场景下 TCP 直连对端是代理（通常是 ``127.0.0.1``），审计里只看 ``client``
就分不清是谁。``CF-Connecting-IP`` / ``X-Forwarded-For`` 能给出真实客户端，但它们是
**请求方可以随便写**的普通头 —— 无条件采信等于让任何人**伪造成任意 IP** 写进审计，
那比不记还糟（看着"有来源"，其实是攻击者自己填的）。
故本文件主要验证：**只有直连对端可信时才采信转发头**，且审计**自证出处**
（``client_source`` + ``peer``）。

**待办 C —— `/mcp` 的鉴权可以交给上游网关。**
用于"Coze 侧只能填『凭证』、放不下自定义 header"的情形。风险是身份只能**静态指定**，
所以必须同时验证：网关模式下 ``/mcp`` 放行、但**其他端点仍要 key**，且身份确实按配置赋值
（能配合工具白名单收窄）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from recall.api import IdentityMiddleware, app, close_service, get_service
from recall.audit import CLIENT_SOURCE_PEER, TrustedProxies, resolve_client
from recall.auth import current_identity
from recall.config import Settings
from tests.helpers import IngestEnv

TRUSTED_LOOPBACK = TrustedProxies.parse(["127.0.0.1", "::1"])


def _headers(**items: str) -> dict[str, str]:
    """构造小写键的请求头字典（与 :func:`resolve_client` 的约定一致）。"""
    return {key.lower().replace("_", "-"): value for key, value in items.items()}


# ------------------------------------------------------------------ 待办 B：有效客户端解析


def test_direct_peer_is_used_when_no_forwarded_header() -> None:
    """没有转发头 ⇒ 用 TCP 对端，并标明来源。"""
    client, source = resolve_client({}, peer="203.0.113.9", trusted=TRUSTED_LOOPBACK)

    assert (client, source) == ("203.0.113.9", CLIENT_SOURCE_PEER)


def test_forwarded_headers_are_ignored_from_an_untrusted_peer() -> None:
    """⚠️ **不可信对端写的转发头必须当没看见** —— 否则审计可被任意伪造。"""
    headers = _headers(CF_Connecting_IP="1.2.3.4", X_Forwarded_For="5.6.7.8")

    client, source = resolve_client(headers, peer="203.0.113.9", trusted=TRUSTED_LOOPBACK)

    assert (client, source) == ("203.0.113.9", CLIENT_SOURCE_PEER)


def test_forwarded_headers_are_honoured_from_a_trusted_peer() -> None:
    """来自可信代理（本机隧道）⇒ 采信真实客户端，并记下出处。"""
    client, source = resolve_client(
        _headers(CF_Connecting_IP="1.2.3.4"), peer="127.0.0.1", trusted=TRUSTED_LOOPBACK
    )

    assert (client, source) == ("1.2.3.4", "cf-connecting-ip")


def test_x_forwarded_for_takes_the_leftmost_entry() -> None:
    """通用反代头取**最左**一项（最初的客户端），右侧是各级代理。"""
    client, source = resolve_client(
        _headers(X_Forwarded_For="1.2.3.4, 10.0.0.1, 10.0.0.2"),
        peer="::1",
        trusted=TRUSTED_LOOPBACK,
    )

    assert (client, source) == ("1.2.3.4", "x-forwarded-for")


def test_cf_header_wins_over_x_forwarded_for() -> None:
    """两个头都在时以 Cloudflare 的专用头为准（它更靠近真实来源）。"""
    client, _ = resolve_client(
        _headers(CF_Connecting_IP="1.2.3.4", X_Forwarded_For="9.9.9.9"),
        peer="127.0.0.1",
        trusted=TRUSTED_LOOPBACK,
    )

    assert client == "1.2.3.4"


def test_empty_forwarded_header_falls_back_to_peer() -> None:
    """头存在但为空 ⇒ 回落对端（别把空串写进审计）。"""
    client, source = resolve_client(
        _headers(CF_Connecting_IP="  "), peer="127.0.0.1", trusted=TRUSTED_LOOPBACK
    )

    assert (client, source) == ("127.0.0.1", CLIENT_SOURCE_PEER)


def test_trusted_proxies_support_cidr_and_reject_garbage() -> None:
    """支持网段；非法条目**启动即抛**（配置写错不该静默降级成"谁都不信"）。"""
    parsed = TrustedProxies.parse(["10.0.0.0/8", "::1"])
    assert parsed.contains("10.1.2.3") is True
    assert parsed.contains("192.168.1.1") is False
    assert parsed.contains("not-an-ip") is False  # 主机名不认，宁可不采信

    with pytest.raises(ValueError, match="可信代理"):
        TrustedProxies.parse(["999.999.999.999"])


def test_empty_trusted_proxies_trust_nothing() -> None:
    """空配置 ⇒ 谁都不信（只记对端）。"""
    empty = TrustedProxies.parse([])

    assert empty.enabled is False
    assert resolve_client(
        _headers(CF_Connecting_IP="1.2.3.4"), peer="127.0.0.1", trusted=empty
    ) == ("127.0.0.1", CLIENT_SOURCE_PEER)


# ------------------------------------------------------------------ 待办 B：审计落盘自证


async def test_audit_records_client_provenance(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """端到端：审计行里既有**有效客户端**，也有**对端**与**出处**。"""
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("RECALL_LOG_DIR", str(log_dir))
    monkeypatch.setenv("RECALL_LOG_TO_FILE", "1")
    monkeypatch.setenv("RECALL_COLLECTION", ingest_env.collection)
    await close_service()
    await get_service()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://recall.test") as client:
            response = await client.get("/health", headers={"CF-Connecting-IP": "1.2.3.4"})
    finally:
        await close_service()

    assert response.status_code == 200
    records = [
        json.loads(line)
        for line in (log_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    last = records[-1]
    # 测试的直连对端是 ASGITransport 默认的 127.0.0.1 ⇒ 属可信 ⇒ 采信转发头
    assert last["client"] == "1.2.3.4"
    assert last["client_source"] == "cf-connecting-ip"
    assert last["peer"] == "127.0.0.1"


# ------------------------------------------------------------------ 待办 C：网关鉴权模式


async def _with_mode(
    ingest_env: IngestEnv,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    gateway_user: str,
    keys: str,
) -> None:
    """按给定配置重建服务单例。"""
    monkeypatch.setenv("RECALL_MCP_AUTH_MODE", mode)
    monkeypatch.setenv("RECALL_MCP_GATEWAY_USER", gateway_user)
    monkeypatch.setenv("RECALL_API_KEYS", keys)
    monkeypatch.setenv("RECALL_COLLECTION", ingest_env.collection)
    await close_service()
    await get_service()


async def test_gateway_mode_lets_mcp_through_without_a_key(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """网关模式：``/mcp`` **不再被本进程拦下**（鉴权在网关）；其余端点仍要 key。

    ⚠️ 这里用**桩下游**而不是真实 ``/mcp``：真实 MCP 应用需要 lifespan
    （``StreamableHTTPSessionManager`` 每个实例只能跑一次，模块级 ``mcp_app`` 已被其它用例占用）。
    桩下游足以精确验证中间件的决策，还能顺手读出被赋予的身份。
    """
    await _with_mode(
        ingest_env, monkeypatch, mode="gateway", gateway_user="coze", keys="tok-me:me"
    )

    stub = FastAPI()

    @stub.post("/mcp/")
    async def _echo_identity() -> dict[str, str]:
        return {"user": current_identity().user}

    stub.add_middleware(IdentityMiddleware)
    transport = httpx.ASGITransport(app=stub)
    real_transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://recall.test") as client:
            mcp = await client.post("/mcp/")
        async with httpx.AsyncClient(
            transport=real_transport, base_url="http://recall.test"
        ) as client:
            rest_without_key = await client.get("/kb/stats")
    finally:
        await close_service()

    assert mcp.status_code == 200, "网关模式下 /mcp 不该被本进程要求 key"
    assert mcp.json()["user"] == "coze", "身份应按 RECALL_MCP_GATEWAY_USER 静态赋值"
    assert rest_without_key.status_code == 401, "其余端点仍必须鉴权"


async def test_default_mode_still_requires_a_key_for_mcp(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认（``app``）模式行为不变：``/mcp`` 与 REST 一样要 key。"""
    await _with_mode(
        ingest_env, monkeypatch, mode="app", gateway_user="me", keys="tok-me:me"
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://recall.test") as client:
            mcp = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )
    finally:
        await close_service()

    assert mcp.status_code == 401


async def test_gateway_mode_assigns_the_configured_identity(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """网关模式下身份**按配置静态赋值**，因此能与工具白名单配合收窄权限。"""
    await _with_mode(
        ingest_env, monkeypatch, mode="gateway", gateway_user="coze", keys="tok-me:me"
    )
    try:
        service = await get_service()
        assert service.settings.mcp_gateway_user == "coze"
        assert service.settings.mcp_auth_mode == "gateway"
        # 关键：网关身份若没被白名单限制就拥有全部工具 —— 这正是启动告警要提醒的
        assert service.settings.mcp_tool_policy == {}
    finally:
        await close_service()


def test_invalid_auth_mode_is_rejected_at_startup() -> None:
    """模式拼错 ⇒ 启动即抛（静默退回 app 模式会让人以为已经交给网关了）。"""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("RECALL_MCP_AUTH_MODE", "gatewy")
    try:
        with pytest.raises(ValueError, match="RECALL_MCP_AUTH_MODE"):
            Settings.from_env()
    finally:
        monkeypatch.undo()
