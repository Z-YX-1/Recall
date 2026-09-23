"""bge-reranker-v2-m3 精排（tech.md §2 Rerank 行、§4；roadmap R-19）。

- 只吃召回候选（默认 Top-20），GPU 秒级；
- ``normalize=True`` 输出 **0~1** 分数——⚠️ 默认是 sigmoid 前的原始分，跨查询不可直接比
  （tech.md §2 已核实）；
- ``use_fp16=True``：与 embedder 同驻 6GB 显存（tech.md §14）；
- 排序**确定性**：分数相同时按候选序号升序，保证同输入同输出（code_standards §0.2）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from FlagEmbedding import FlagReranker

from recall.model_cache import inference_lock, load_once

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
"""HuggingFace 模型名（走 ``HF_ENDPOINT`` 镜像下载，tech.md §12）。"""

DEFAULT_TOP_N = 20
"""精排后保留条数（tech.md §4：bge-reranker-v2-m3 精排 Top-20）。"""

DEFAULT_BATCH_SIZE = 32

MAX_LENGTH = 1024
"""query+passage 的最大 token 数；块上限 800（cl100k 估算）留足余量，避免静默截断。"""

RERANK_HEADING_SEPARATOR = "\n"


def build_rerank_document(heading_path: str, text: str) -> str:
    """拼出喂给精排的文本：**标题路径 + 块正文**。

    ⚠️ 标题不能省。实测（2026-09-22，30 题黄金集，其余条件完全相同）：

    | 精排输入 | Recall@1 | Recall@3 | MRR |
    | :--- | ---: | ---: | ---: |
    | 只喂块正文 | 0.467 | 0.700 | 0.591 |
    | **标题 + 正文** | **0.767** | **0.933** | **0.859** |

    逐题对比：加标题后 **13 题变好、0 题变差**。原因是这类笔记的话题信号大部分在标题里
    （如「🎲 ai-Temperature 与确定性控制 —— 全景解析」），只喂正文会让 cross-encoder
    失去最强的判别特征，把 RRF 原本排第 1 的文档压到第 2~3。

    Args:
        heading_path: 块的标题路径，如 ``"标题 > 小节"``；为空时只用正文。
        text: 块正文。

    Returns:
        精排输入文本。
    """
    heading = heading_path.strip()
    return f"{heading}{RERANK_HEADING_SEPARATOR}{text}" if heading else text


@dataclass(frozen=True, slots=True)
class RerankHit:
    """一条精排结果：候选序号 + 归一化分数。

    Attributes:
        index: 对应传入 ``documents`` 的下标。
        score: 0~1 的归一化分数（``normalize=True``）。
    """

    index: int
    score: float


class Reranker:
    """bge-reranker-v2-m3 封装（惰性加载，避免导入即占显存）。"""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        *,
        use_fp16: bool = True,
        batch_size: int = DEFAULT_BATCH_SIZE,
        device: str | None = None,
    ) -> None:
        """配置重排器（不加载权重）。

        Args:
            model_name: HuggingFace 模型名或本地路径。
            use_fp16: 半精度推理（6GB 显存下必须为 ``True``，tech.md §14）。
            batch_size: 推理批大小。
            device: 形如 ``"cuda:0"`` / ``"cpu"``；``None`` 交给 FlagEmbedding 自动选择。
        """
        self.model_name = model_name
        self.use_fp16 = use_fp16
        self.batch_size = batch_size
        self.device = device

    async def rerank(
        self, query: str, documents: Sequence[str], *, top_n: int = DEFAULT_TOP_N
    ) -> list[RerankHit]:
        """对候选文档精排，返回分数降序的 Top-N。

        GPU 推理是阻塞调用，用 :func:`asyncio.to_thread` 隔离（code_standards §0.4）。

        Args:
            query: 用户原始 query（同模型同措辞，不做改写）。
            documents: 候选块原文，顺序与召回结果一致。
            top_n: 保留条数。

        Returns:
            按分数降序（同分按原序号升序）的 :class:`RerankHit` 列表；空输入返回空列表。
        """
        if not documents or top_n <= 0:
            return []
        return await asyncio.to_thread(self._rerank_sync, query, list(documents), top_n)

    def _ensure_model(self) -> FlagReranker:
        """取共享的 reranker 实例（进程级缓存 + 装载锁，见 :mod:`recall.model_cache`）。"""
        kwargs: dict[str, Any] = {"use_fp16": self.use_fp16, "normalize": True}
        if self.device is not None:
            kwargs["devices"] = self.device
        model_name = self.model_name
        return load_once(
            (model_name, self.use_fp16, self.device),
            lambda: FlagReranker(model_name, **kwargs),
        )

    def _rerank_sync(self, query: str, documents: list[str], top_n: int) -> list[RerankHit]:
        model = self._ensure_model()
        # ⚠️ 必须加锁：compute_score 每次调用都会改模型（self.model.half()），
        # 并发调用共享实例会抛 "expected scalar type Float but found Half"。
        with inference_lock():
            raw_scores = model.compute_score(
                [[query, document] for document in documents],
                batch_size=self.batch_size,
                max_length=MAX_LENGTH,
            )
        scores = _as_score_list(raw_scores)
        if len(scores) != len(documents):
            raise RuntimeError(
                f"reranker 返回 {len(scores)} 个分数，与 {len(documents)} 个候选不匹配"
            )
        order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
        hits = [RerankHit(index=index, score=scores[index]) for index in order[:top_n]]
        logger.debug(
            "reranker.scored",
            extra={
                "model_name": self.model_name,
                "candidate_count": len(documents),
                "kept": len(hits),
                "top_score": hits[0].score if hits else None,
            },
        )
        return hits


def _as_score_list(raw: Any) -> list[float]:  # noqa: ANN401 - FlagEmbedding 返回类型未标注
    """把 ``compute_score`` 的返回值规整成 ``list[float]``（单条时它返回标量）。"""
    if isinstance(raw, (int, float)):
        return [float(raw)]
    return [float(score) for score in raw]
