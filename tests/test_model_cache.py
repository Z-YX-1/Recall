"""进程级模型缓存必测项（tech.md §14 显存纪律；roadmap R-23b）。

这个模块存在的理由是修一个**原生层崩溃**：连续多次装载 bge-m3 会触发
``Windows fatal exception: access violation``（transformers 5.x 权重装载用内部
线程池并行 materialize，两线程同时装载即崩）。因此这里必须验证两件事：

1. 同 key 只装载一次、**返回同一对象**（消除重复装载）；
2. 并发请求同一个 key 时也只装载一次（装载锁生效）。

测试用**假的装载函数**（不真的加载 1.2GB 模型），但走的是与生产完全相同的
:func:`~recall.model_cache.load_once` 路径。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from recall.model_cache import cached_models, clear_cache, load_once


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    """每个用例前后清空缓存，避免相互干扰。"""
    clear_cache()
    yield
    clear_cache()


def test_same_key_loads_only_once_and_returns_same_object() -> None:
    calls: list[str] = []

    def factory() -> object:
        calls.append("loaded")
        return object()

    key = ("BAAI/bge-m3", True, None)
    first = load_once(key, factory)
    second = load_once(key, factory)

    assert first is second
    assert calls == ["loaded"]
    assert cached_models() == [key]


def test_different_keys_load_separately() -> None:
    keys = [("BAAI/bge-m3", True, None), ("BAAI/bge-m3", False, None), ("BAAI/bge-m3", True, "cpu")]
    loaded = [load_once(key, object) for key in keys]

    assert len({id(item) for item in loaded}) == 3
    # 设备名含 None 与 "cpu"，排序必须 None 安全（曾经的 TypeError 回归点）
    assert cached_models() == sorted(keys, key=lambda key: (key[0], key[1], key[2] or ""))


def test_concurrent_loads_share_one_instance() -> None:
    """装载锁：并发请求同 key 时工厂只被调用一次（这正是崩溃的修复点）。"""
    calls = 0
    lock = threading.Lock()

    def factory() -> object:
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.05)  # 模拟加载耗时，放大竞态窗口
        return object()

    key = ("BAAI/bge-reranker-v2-m3", True, None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: load_once(key, factory), range(8)))

    assert calls == 1
    assert len({id(item) for item in results}) == 1


def test_clear_cache_forces_reload() -> None:
    key = ("BAAI/bge-m3", True, None)
    first = load_once(key, object)
    clear_cache()
    second = load_once(key, object)

    assert first is not second
    assert cached_models() == [key]
