"""进程级模型缓存：同一 ``(模型, 精度, 设备)`` 只装载一次（tech.md §14 显存纪律）。

两个必须解决的现实问题：

1. **显存纪律**：6GB 显存容不下 bge-m3 与 reranker 的多份副本，进程内必须复用；
2. **原生层崩溃**：transforms 5.x 的权重装载会在内部线程池里并行 materialize，
   两个线程同时装载模型会触发 ``Windows fatal exception: access violation``
   （2026-09-22 实测：pytest 会话中连续装载 bge-m3 多次后进程崩溃）。
   因此装载统一走本模块：**进程内加锁 + 命中缓存复用**。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TypeVar, cast

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

CacheKey = tuple[str, bool, str | None]
"""缓存键：``(模型名, 是否 fp16, 设备)``。"""

_cache: dict[CacheKey, object] = {}
_lock = threading.Lock()


def load_once(key: CacheKey, factory: Callable[[], _T]) -> _T:
    """按 ``key`` 缓存模型实例；首次调用在锁内同步完成装载。

    Args:
        key: 缓存键，形如 ``("BAAI/bge-m3", True, "cuda:0")``。
        factory: 真正的装载函数，仅在缓存未命中时调用一次。

    Returns:
        模型实例；同 ``key`` 恒返回同一对象。
    """
    with _lock:
        cached = _cache.get(key)
        if cached is None:
            logger.info("model_cache.loading", extra={"key": list(key)})
            cached = factory()
            _cache[key] = cached
        else:
            logger.debug("model_cache.hit", extra={"key": list(key)})
        return cast(_T, cached)


def cached_models() -> list[CacheKey]:
    """列出当前已缓存的模型键（诊断 / 测试用，按 ``(模型名, fp16, 设备)`` 排序）。

    ⚠️ 排序键把设备名做了 ``None → ""`` 归一：直接 ``sorted()`` 会在
    ``None`` 与 ``"cpu"`` 之间比较时抛 ``TypeError``（2026-09-22 由单测发现）。
    """
    with _lock:
        return sorted(_cache, key=lambda key: (key[0], key[1], key[2] or ""))


def clear_cache() -> None:
    """清空缓存（测试收尾用；正常服务进程不需要调用）。"""
    with _lock:
        _cache.clear()
