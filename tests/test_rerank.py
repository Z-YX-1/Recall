"""重排必测项（tech.md §2 Rerank 行：``normalize=True`` ⇒ 0~1 分数；§4：精排 Top-20）。"""

from __future__ import annotations

from recall.rerank import Reranker

_RELEVANT = "混合检索用 RRF 把 dense 与 sparse 两路召回结果融合，再交给重排模型精排。"
_NOISE = "五花肉切块，冷水下锅焯水，加冰糖炒出糖色后小火慢炖。"


async def test_rerank_puts_the_relevant_document_first(reranker: Reranker) -> None:
    hits = await reranker.rerank(
        "混合检索怎么融合两路召回结果？", [_NOISE, _RELEVANT, _NOISE], top_n=3
    )
    assert hits[0].index == 1
    assert {hit.index for hit in hits} == {0, 1, 2}


async def test_rerank_scores_are_normalized_to_unit_range(reranker: Reranker) -> None:
    """⚠️ tech.md §2：默认是 sigmoid 前的原始分，跨查询不可比；必须 normalize=True。"""
    hits = await reranker.rerank("RAG 检索质量", [_RELEVANT, _NOISE], top_n=2)
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)


async def test_rerank_respects_top_n(reranker: Reranker) -> None:
    hits = await reranker.rerank("检索", [_RELEVANT, _NOISE, _RELEVANT, _NOISE], top_n=2)
    assert len(hits) == 2


async def test_rerank_is_deterministic(reranker: Reranker) -> None:
    documents = [_RELEVANT, _NOISE, _RELEVANT]
    first = await reranker.rerank("混合检索", documents, top_n=3)
    second = await reranker.rerank("混合检索", documents, top_n=3)
    assert first == second


async def test_rerank_handles_empty_and_zero_top_n(reranker: Reranker) -> None:
    assert await reranker.rerank("q", [], top_n=5) == []
    assert await reranker.rerank("q", [_RELEVANT], top_n=0) == []


async def test_rerank_single_document_returns_scalar_score(reranker: Reranker) -> None:
    """FlagEmbedding 对单条输入返回标量而非列表，实现必须兼容。"""
    hits = await reranker.rerank("混合检索", [_RELEVANT], top_n=1)
    assert len(hits) == 1
    assert hits[0].index == 0
