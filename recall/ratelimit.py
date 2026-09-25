"""进程内滑动窗口限流（code_standards §6.1；roadmap R-40 补记）。

`POST /kb/ingest` 是**有副作用的写操作**，规范要求它"自 S2 起必须挂**鉴权 + 限流**"。
鉴权已在 `recall/api.py::IdentityMiddleware`（传输层职责）；本模块补上限流。

**为什么限流放在 `kb_ingest_core` 而不是中间件**：写入口**同时**服务 REST 与 MCP
（`/mcp` 的 `kb_ingest` 工具走的是同一个 core），放在中间件只挡得住 REST 那条路。
各守其位：**鉴权在传输层、限流在业务入口**。

**为什么按"入口"而不是"调用者"计数**：摄取消耗的是**全局稀缺资源**
（一份模型、一个 Qdrant、一把 `inference_lock`），第二个并发请求对谁都没好处。
所以这里一个桶管全部调用者，与身份无关。

**局限（不假装它是分布式限流）**：状态在**进程内** —— 多进程部署时各算一份。
本机单进程（tech.md §12）够用；公网暴露时若需更强限流，应在网关层
（Cloudflare Access / 反向代理）再加一道，见 roadmap R-39。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

MAX_TRACKED_KEYS = 256
"""跟踪的 key 上限；超过就整体清理一次，避免长期运行内存无界增长。"""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """一次限流判定的结果。

    Attributes:
        allowed: 是否放行。
        retry_after_s: 被拒时还需等待的整秒数；放行时为 ``0``。
        remaining: 本窗口内还剩几次；被拒时为 ``0``。
    """

    allowed: bool
    retry_after_s: int
    remaining: int


class SlidingWindowLimiter:
    """按 key 的滑动窗口限流（``limit`` 次 / ``window_s`` 秒）。

    Args:
        limit: 窗口内允许的次数；``<= 0`` 表示**关闭限流**。
        window_s: 窗口长度（秒）。
        clock: 单调时钟，测试可注入以**不靠 sleep** 验证窗口行为。
    """

    def __init__(
        self,
        limit: int,
        window_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """是否启用（``limit`` 与窗口都为正）。"""
        return self._limit > 0 and self._window > 0

    @property
    def limit(self) -> int:
        """窗口内允许的次数（0 表示关闭）。"""
        return self._limit

    def check(self, key: str = "default") -> RateLimitDecision:
        """登记一次调用并判定是否放行。

        Args:
            key: 桶名；本项目写入口用单一桶（见模块 docstring）。

        Returns:
            放行/拒绝与剩余额度、等待秒数。
        """
        if not self.enabled:
            return RateLimitDecision(True, 0, 0)
        now = self._clock()
        with self._lock:
            if len(self._hits) > MAX_TRACKED_KEYS:
                self._sweep(now)
            timestamps = self._hits.setdefault(key, deque())
            self._prune(timestamps, now)
            if len(timestamps) >= self._limit:
                retry_after = max(1, math.ceil(timestamps[0] + self._window - now))
                return RateLimitDecision(False, retry_after, 0)
            timestamps.append(now)
            return RateLimitDecision(True, 0, self._limit - len(timestamps))

    def _prune(self, timestamps: deque[float], now: float) -> None:
        """丢掉滑出窗口的时间戳。"""
        edge = now - self._window
        while timestamps and timestamps[0] <= edge:
            timestamps.popleft()

    def _sweep(self, now: float) -> None:
        """清理所有空桶（内存保护，不改变任何判定结果）。"""
        for key in list(self._hits):
            self._prune(self._hits[key], now)
            if not self._hits[key]:
                del self._hits[key]
