"""R-46 回归：系统代理把"连不上"变成 HTTP 502 时，仍须给出**语义化 503**。

**现场**（2026-09-25 实测）：本机开着系统代理（`karingService` @127.0.0.1:3067，
写进 Windows 系统代理），httpx 的 `trust_env=True` 把连 Qdrant 的请求也发给代理；
代理对**不可达端口**返回 **HTTP 502 空体**（连保留地址 `192.0.2.1` 也一样），
于是 qdrant-client 抛 `UnexpectedResponse` 而不是连接错误 ⇒ `with_retry` 只认 httpx
连接异常的判断漏掉它 ⇒ `/kb/search` 退化成**带堆栈的 500**，而用户本该看到
503「请启动 qdrant.exe」（R-27i 的设计被绕过）。

两条防线各测一条：

1. **根因**：`Settings.from_env()` 把回环地址并入 `NO_PROXY`，且**只加回环**——
   绝不能设 `*`，否则会连出网代理（DeepSeek）一起打断；
2. **兜底**：502 / 503 / 504 也算"Qdrant 不可达"，照样映射成 503。
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from recall.api import ApiError, Service, close_service, kb_search_core
from recall.config import LOCAL_HOSTS_NO_PROXY, Settings
from recall.models import SearchRequest
from recall.store import QdrantStore, StoreUnavailableError

# --------------------------------------------------------------------------- 桩：网关 502


class _Gateway502:
    """对任何请求都回 **502 空体**的本地桩（模拟代理对不可达目标的行为）。"""

    def __init__(self) -> None:
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """桩服务的根地址。"""
        assert self._server is not None
        host, port = self._server.server_address[0], int(self._server.server_address[1])
        host_text = host.decode("utf-8") if isinstance(host, bytes) else str(host)
        return f"http://{host_text}:{port}"

    def __enter__(self) -> _Gateway502:
        class Handler(BaseHTTPRequestHandler):
            def _respond_502(self) -> None:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = _respond_502
            do_POST = _respond_502
            do_HEAD = _respond_502

            def log_message(self, *args: Any) -> None:
                """静音。"""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


# --------------------------------------------------------------------------- 防线 1：NO_PROXY


def test_localhost_is_merged_into_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """回环地址必须绕过代理；用户已有的条目**原样保留**。"""
    monkeypatch.setenv("NO_PROXY", "example.com,10.0.0.0/8")

    Settings.from_env()

    parts = [item.strip() for item in _env("NO_PROXY").split(",")]
    assert "example.com" in parts and "10.0.0.0/8" in parts
    for host in LOCAL_HOSTS_NO_PROXY:
        assert host in parts


def test_no_proxy_never_becomes_a_blanket_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    """**绝不能设 ``*``**：出网代理（DeepSeek）可能正需要它，全局禁用会一起打断。"""
    monkeypatch.setenv("NO_PROXY", "example.com")

    Settings.from_env()

    assert "*" not in _env("NO_PROXY")


def test_no_proxy_merge_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """重复建配置不会把条目越堆越多（``recall`` 包导入时就会建一次）。"""
    monkeypatch.setenv("NO_PROXY", "example.com")

    Settings.from_env()
    first = _env("NO_PROXY")
    Settings.from_env()

    assert _env("NO_PROXY") == first


def _env(name: str) -> str:
    """读环境变量（小工具，避免在各处重复 import os）。"""
    import os

    return os.environ.get(name, "")


# --------------------------------------------------------------------------- 防线 2：错误映射


async def test_qdrant_answering_502_is_reported_as_unavailable() -> None:
    """store 层：网关 502 也算"Qdrant 不可达"，抛 ``StoreUnavailableError``。"""
    with _Gateway502() as gateway:
        store = QdrantStore(gateway.url, timeout=5)
        try:
            with pytest.raises(StoreUnavailableError):
                await store.collection_exists("recall__whatever")
        finally:
            await store.close()


async def test_kb_search_maps_a_502_gateway_to_semantic_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API 层（**R-46 的核心回归**）：502 网关 ⇒ 503 ``qdrant_unavailable`` 而不是 500。

    与 `test_search.py::test_kb_search_reports_qdrant_down_as_semantic_503` 是同一断言，
    区别只在"不可达"的表现形式：那边是**连接被拒**，这边是**代理回 502**。
    """
    with _Gateway502() as gateway:
        dead = await Service.create(
            dataclasses.replace(Settings.from_env(), qdrant_url=gateway.url),
            collection="recall__whatever",
        )
        monkeypatch.setattr("recall.api._service", dead)
        try:
            with pytest.raises(ApiError) as excinfo:
                await kb_search_core(SearchRequest(query="任何问题"))
        finally:
            await close_service()

    assert excinfo.value.code == "qdrant_unavailable"
    assert excinfo.value.status_code == 503
    assert "qdrant.exe" in excinfo.value.message  # 仍然告诉用户怎么修


# ------------------------------------------------------- 防线 3：Qdrant 自身降级（500）


class _DegradedQdrant:
    """回 Qdrant "未从先前错误恢复" 的 500（模拟磁盘 IO 错误后的降级状态）。

    现场报文（2026-09-25 实测）：
    ``{"status":{"error":"Service internal error: Not recovered from previous error:
    Service runtime error: IO Error: 拒绝访问。 (os error 5)"}}``
    """

    BODY = (
        '{"status":{"error":"Service internal error: Not recovered from previous error: '
        'Service runtime error: IO Error: 拒绝访问。 (os error 5)"},"time":0.012}'
    ).encode()

    def __init__(self, body: bytes) -> None:
        self.body = body
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """桩服务的根地址。"""
        assert self._server is not None
        host, port = self._server.server_address[0], int(self._server.server_address[1])
        host_text = host.decode("utf-8") if isinstance(host, bytes) else str(host)
        return f"http://{host_text}:{port}"

    def __enter__(self) -> _DegradedQdrant:
        body = self.body

        class Handler(BaseHTTPRequestHandler):
            def _respond(self) -> None:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _respond
            do_POST = _respond

            def log_message(self, *args: Any) -> None:
                """静音。"""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


async def test_qdrant_degraded_500_is_reported_as_unavailable() -> None:
    """Qdrant 处于"未恢复"降级态 ⇒ 也归入不可达（语义化 503 + 提示重启），而不是裸 500。"""
    with _DegradedQdrant(_DegradedQdrant.BODY) as stub:
        store = QdrantStore(stub.url, timeout=5)
        try:
            with pytest.raises(StoreUnavailableError):
                await store.collection_exists("recall__whatever")
        finally:
            await store.close()


async def test_other_500s_are_not_swallowed_as_unavailable() -> None:
    """**别过度匹配**：普通 500（原因不是降级）仍按原样抛出，不伪装成"不可达"。"""
    with _DegradedQdrant(b'{"status":{"error":"something else went wrong"}}') as stub:
        store = QdrantStore(stub.url, timeout=5)
        try:
            with pytest.raises(Exception) as excinfo:
                await store.collection_exists("recall__whatever")
        finally:
            await store.close()

    assert not isinstance(excinfo.value, StoreUnavailableError), (
        "只有『未从先前错误恢复』才算降级；其它 500 应保持原样，"
        "否则会把真故障误报成『Qdrant 没启动』"
    )


@pytest.fixture(autouse=True)
def _no_proxy_for_gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """确保桩服务本身不会被系统代理截走（与生产侧 `NO_PROXY` 修复同一件事）。"""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    yield
