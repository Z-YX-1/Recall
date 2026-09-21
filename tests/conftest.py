"""pytest 共享夹具。

设计原则：

- **纯单测**（切分、id、registry、connector）不依赖任何外部服务，永远可跑；
- **集成测试**（Qdrant / GPU 模型）在依赖不可用时 ``skip`` 并给出原因，
  依赖可用时真实运行——不用 mock 冒充端到端。
- 模型走 **离线缓存**（``HF_HUB_OFFLINE=1``）：测试不产生网络请求，结果可复现。
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import pytest

# 必须早于 FlagEmbedding / transformers 的导入：让模型只从本地缓存加载
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from recall.embedder import DEFAULT_MODEL_NAME, Embedder  # noqa: E402
from recall.store import QdrantStore  # noqa: E402

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")


def _port_open(url: str, timeout: float = 1.0) -> bool:
    """探测 Qdrant 端口是否可连（同步实现，避免事件循环作用域问题）。"""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6333
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _model_cached(repo: str) -> bool:
    """判断 HF 本地缓存里是否已有模型权重。"""
    hf_home = os.getenv("HF_HOME")
    base = Path(hf_home) / "hub" if hf_home else Path.home() / ".cache" / "huggingface" / "hub"
    folder = base / ("models--" + repo.replace("/", "--"))
    if not folder.is_dir():
        return False
    return any(folder.rglob("*.safetensors")) or any(folder.rglob("pytorch_model.bin"))


@pytest.fixture(scope="session")
def qdrant_url() -> str:
    """Qdrant 服务地址；服务不可用时跳过依赖它的用例。"""
    if not _port_open(QDRANT_URL):
        pytest.skip(f"Qdrant 未运行（{QDRANT_URL}）")
    return QDRANT_URL


@pytest.fixture
async def store(qdrant_url: str) -> AsyncIterator[QdrantStore]:
    """按用例生命周期管理的 Qdrant 适配层。"""
    client = QdrantStore(qdrant_url)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture(scope="session")
def embedder() -> Embedder:
    """会话级 bge-m3 编码器（惰性加载，权重只进显存一次）。"""
    if not _model_cached(DEFAULT_MODEL_NAME):
        pytest.skip(f"本地缓存缺少模型 {DEFAULT_MODEL_NAME}")
    return Embedder(device=os.getenv("RECALL_TEST_DEVICE"))


@pytest.fixture
def unique_collection() -> str:
    """一次性 collection 名，避免用例之间互相污染。"""
    return f"recall-test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """空 vault 目录。"""
    root = tmp_path / "vault"
    root.mkdir()
    return root
