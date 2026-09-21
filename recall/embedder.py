"""bge-m3 双向量编码器（tech.md §2 Embedding 行；roadmap R-13）。

- **必须用 FlagEmbedding 的** ``BGEM3FlagModel``：sentence-transformers 只支持 bge-m3 的
  dense 输出，sparse（``lexical_weights``）拿不到（tech.md §2 ✅ 核实）。
- 1024 维 dense + 自带 learned sparse ⇒ 中文混合检索免 jieba。
- 全程 GPU（``use_fp16=True``），数据不出域（tech.md §15 决策 7）。
- 模型版本从 spec 注入：构造参数 → collection metadata 与 point payload，禁止硬编码语义。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
from FlagEmbedding import BGEM3FlagModel

from recall.model_cache import load_once

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "BAAI/bge-m3"
"""HuggingFace 模型名（走 ``HF_ENDPOINT`` 镜像下载，tech.md §12）。"""

DEFAULT_EMBEDDING_MODEL = "bge-m3"
"""写入 collection 名与 payload 的模型标识（tech.md §3.1）。"""

DEFAULT_EMBEDDING_VERSION = "v1"
"""embedding 版本号：换模型必须新建 collection 并排重灌，旧库不删（tech.md §3.1）。"""

DENSE_DIM = 1024
"""bge-m3 dense 向量维度（tech.md §3.3）。"""

DEFAULT_BATCH_SIZE = 16
"""嵌入 batch size，取值区间 16~32（tech.md §2 Embedding 行）。"""

MAX_SEQ_LENGTH = 1024
"""单条文本最大 token 数：块上限 800（cl100k 估算）留足余量，避免静默截断。"""


@dataclass(frozen=True, slots=True)
class Embedding:
    """单条文本的双向量编码结果。

    Attributes:
        dense: 1024 维归一化 dense 向量。
        sparse_indices: learned sparse 的 token id（升序，Qdrant 要求）。
        sparse_values: 与 ``sparse_indices`` 一一对应的权重。
    """

    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]


class Embedder:
    """bge-m3 编码器封装（惰性加载，避免导入即占显存）。"""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        *,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_version: str = DEFAULT_EMBEDDING_VERSION,
        use_fp16: bool = True,
        batch_size: int = DEFAULT_BATCH_SIZE,
        device: str | None = None,
    ) -> None:
        """配置编码器（不加载权重）。

        Args:
            model_name: HuggingFace 模型名或本地路径。
            embedding_model: 写入 payload/metadata 的模型标识。
            embedding_version: 写入 payload/metadata 的版本号。
            use_fp16: 半精度推理（6GB 显存下必须为 ``True``，tech.md §14）。
            batch_size: 推理批大小，建议 16~32。
            device: 形如 ``"cuda:0"`` / ``"cpu"``；``None`` 表示交给 FlagEmbedding 自动选择。
        """
        self.model_name = model_name
        self.embedding_model = embedding_model
        self.embedding_version = embedding_version
        self.use_fp16 = use_fp16
        self.batch_size = batch_size
        self.device = device

    @property
    def dimension(self) -> int:
        """dense 向量维度（建 collection 用）。"""
        return DENSE_DIM

    async def encode(
        self, texts: Sequence[str], *, batch_size: int | None = None
    ) -> list[Embedding]:
        """批量编码文本为 dense + sparse 双向量。

        GPU 推理是阻塞调用，统一用 :func:`asyncio.to_thread` 隔离（code_standards §0.4）。

        Args:
            texts: 待编码文本（query 与 passage 用同一模型编码，tech.md §3.1）。
            batch_size: 覆盖默认批大小。

        Returns:
            与 ``texts`` 等长、顺序一致的 :class:`Embedding` 列表；空输入返回空列表。
        """
        if not texts:
            return []
        return await asyncio.to_thread(
            self._encode_sync, list(texts), batch_size or self.batch_size
        )

    def _ensure_model(self) -> BGEM3FlagModel:
        """取共享的 bge-m3 实例（进程级缓存 + 装载锁，见 :mod:`recall.model_cache`）。"""
        kwargs: dict[str, Any] = {"use_fp16": self.use_fp16}
        if self.device is not None:
            kwargs["devices"] = self.device
        model_name = self.model_name
        return load_once(
            (model_name, self.use_fp16, self.device),
            lambda: BGEM3FlagModel(model_name, **kwargs),
        )

    def _encode_sync(self, texts: list[str], batch_size: int) -> list[Embedding]:
        output = self._ensure_model().encode(
            texts,
            batch_size=batch_size,
            max_length=MAX_SEQ_LENGTH,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        dense_matrix = cast(np.ndarray, output["dense_vecs"])
        lexical_weights = cast("list[dict[str, float]]", output["lexical_weights"])

        embeddings: list[Embedding] = []
        for row, weights in zip(dense_matrix, lexical_weights, strict=True):
            pairs = sorted(
                (int(token_id), float(weight))
                for token_id, weight in weights.items()
                if float(weight) > 0.0
            )
            embeddings.append(
                Embedding(
                    dense=[float(value) for value in row],
                    sparse_indices=[token_id for token_id, _ in pairs],
                    sparse_values=[weight for _, weight in pairs],
                )
            )
        logger.debug(
            "embedder.encoded",
            extra={
                "model_name": self.model_name,
                "count": len(embeddings),
                "batch_size": batch_size,
                "avg_sparse_terms": (
                    sum(len(item.sparse_indices) for item in embeddings) / len(embeddings)
                    if embeddings
                    else 0.0
                ),
            },
        )
        return embeddings
