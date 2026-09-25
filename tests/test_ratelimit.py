"""写端点限流测试（code_standards §6.1；roadmap R-40 补记）。

规范要求：`POST /kb/ingest` 是**有副作用的写操作**，"自 S2 起必须挂**鉴权 + 限流**"。
鉴权在传输层（`IdentityMiddleware`，见 `tests/test_auth.py`）；**限流在写入口**
（`kb_ingest_core`）—— 因为 `/mcp` 的 `kb_ingest` 工具走同一个 core，
放在中间件只挡得住 REST 那条路。

窗口行为用**注入的假时钟**验证，不靠 `sleep`（否则测试会慢且偶发）。
"""

from __future__ import annotations

import httpx
import pytest

from recall.api import ApiError, close_service, get_service, kb_ingest_core
from recall.models import IngestRequest
from recall.ratelimit import SlidingWindowLimiter
from tests.helpers import IngestEnv

WINDOW_S = 60.0


class _FakeClock:
    """可手动推进的单调时钟（测试窗口行为不睡觉）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        """当前"时间"。"""
        return self.now

    def advance(self, seconds: float) -> None:
        """把时间往前推。"""
        self.now += seconds


# --------------------------------------------------------------------------- 限流器本体


def test_disabled_limiter_allows_everything() -> None:
    """``limit=0`` ⇒ 关闭限流（默认配置里它是打开的，但必须能关）。"""
    limiter = SlidingWindowLimiter(0, 0.0)

    assert limiter.enabled is False
    assert all(limiter.check().allowed for _ in range(50))


def test_allows_up_to_the_limit_then_rejects() -> None:
    """窗口内恰好放行 ``limit`` 次，第 ``limit+1`` 次被拒。"""
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(3, WINDOW_S, clock=clock)

    decisions = [limiter.check() for _ in range(4)]

    assert [item.allowed for item in decisions] == [True, True, True, False]
    assert [item.remaining for item in decisions] == [2, 1, 0, 0]
    assert decisions[3].retry_after_s == int(WINDOW_S)


def test_window_refills_after_it_slides() -> None:
    """窗口滑过之后重新可用（证明是滑动窗口，不是"用满即永久封禁"）。"""
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(1, WINDOW_S, clock=clock)
    assert limiter.check().allowed is True
    assert limiter.check().allowed is False

    clock.advance(WINDOW_S + 0.1)

    assert limiter.check().allowed is True


def test_retry_after_counts_down_as_the_window_slides() -> None:
    """``retry_after_s`` 随等待时间递减（给调用方一个可用的重试提示）。"""
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(1, WINDOW_S, clock=clock)
    limiter.check()

    first = limiter.check()
    clock.advance(30.0)
    later = limiter.check()

    assert first.retry_after_s == int(WINDOW_S)
    assert later.retry_after_s < first.retry_after_s
    assert later.retry_after_s == int(WINDOW_S - 30.0)


def test_buckets_are_independent() -> None:
    """不同 key 各自计数（写入口用单一桶，但接口必须支持分桶）。"""
    limiter = SlidingWindowLimiter(1, WINDOW_S, clock=_FakeClock())

    assert limiter.check("a").allowed is True
    assert limiter.check("b").allowed is True
    assert limiter.check("a").allowed is False


# --------------------------------------------------------------------------- 写入口接线


async def _rebuild(ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    """把限流配置写进环境并重建服务单例（服务持有 settings 快照）。"""
    monkeypatch.setenv("RECALL_INGEST_RATE_LIMIT", limit)
    monkeypatch.setenv("RECALL_COLLECTION", ingest_env.collection)
    monkeypatch.setenv("RECALL_VAULT_PATH", str(ingest_env.vault))
    monkeypatch.setenv("RECALL_REGISTRY_DB", str(ingest_env.registry_db))
    await close_service()
    await get_service()


async def test_ingest_core_rejects_with_429_when_bucket_is_empty(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """桶空后写入口抛语义化 429（REST 与 MCP 共用同一条判定）。"""
    await _rebuild(ingest_env, monkeypatch, "1/60")
    try:
        first = await kb_ingest_core(IngestRequest(mode="update"))
        assert first.scanned == 0  # 空 vault，不会真的索引

        with pytest.raises(ApiError) as excinfo:
            await kb_ingest_core(IngestRequest(mode="update"))
    finally:
        await close_service()

    assert excinfo.value.code == "rate_limited"
    assert excinfo.value.status_code == 429
    assert "秒后重试" in excinfo.value.message


async def test_rest_ingest_returns_the_shared_error_envelope(
    ingest_env: IngestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REST 侧同样走**统一错误信封**（code_standards §6.1）。"""
    from recall.api import app

    await _rebuild(ingest_env, monkeypatch, "1/60")
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://recall.test") as client:
            ok = await client.post("/kb/ingest", json={"mode": "update"})
            limited = await client.post("/kb/ingest", json={"mode": "update"})
    finally:
        await close_service()

    assert ok.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
