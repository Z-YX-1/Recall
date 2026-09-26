"""评分测量脚本的分组语义测试（roadmap R-42 阶段 2；`eval/BASELINE.md` §7.6）。

这批用例钉住的**不是数字而是语义** —— 「笔记里没有」必须分成两类，否则报告会说出
错误的结论：

1. ``mentioned``（提了名没解释）与 ``adjacent``（完全没提）必须**能被分别统计**：
   前者的库里有相关 chunk、精排分数落在笔记内题的带上，后者没有；
2. **空档（可分性）只由 ``far`` + ``adjacent`` 决定** —— 把 ``mentioned`` 算进
   "笔记外最高分"会让空档恒为空，从而错误地判「门槛不可用」，而那类问题的责任方
   本来就是**答案模板**；
3. 未知 group 必须**报错**而不是被静默归到某一类（静默归类会让基线悄悄变质）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.measure_scores import (
    GROUP_ADJACENT,
    GROUP_FAR,
    GROUP_GOLDEN,
    GROUP_MENTIONED,
    PROJECT_ROOT,
    QuestionScore,
    judge,
    load_out_of_vault,
    recommend,
    simulate_truncation,
    summarize,
    sweep_thresholds,
)


def _score(question: str, group: str, top1: float) -> QuestionScore:
    """构造一条只带 top1 的分数记录（纯函数用例不需要证据带）。"""
    return QuestionScore(
        question=question, group=group, top1=top1, top3_mean=top1, top5_mean=top1, count=1
    )


def test_loader_accepts_the_three_outside_groups(tmp_path: Path) -> None:
    """``far`` / ``adjacent`` / ``mentioned`` 都能读入。"""
    path = tmp_path / "items.jsonl"
    path.write_text(
        "\n".join(
            [
                f'{{"question": "a", "group": "{GROUP_FAR}"}}',
                f'{{"question": "b", "group": "{GROUP_ADJACENT}"}}',
                f'{{"question": "c", "group": "{GROUP_MENTIONED}"}}',
                "",
            ]
        ),
        encoding="utf-8",
    )

    items = load_out_of_vault(path)

    assert [item["group"] for item in items] == [GROUP_FAR, GROUP_ADJACENT, GROUP_MENTIONED]


def test_loader_rejects_an_unknown_group(tmp_path: Path) -> None:
    """未知 group 报错（不静默归类）。"""
    path = tmp_path / "items.jsonl"
    path.write_text('{"question": "a", "group": "nearby"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="group 必须是"):
        load_out_of_vault(path)


def test_repository_question_set_is_loadable() -> None:
    """仓库里的真题集必须能被解析（防数据文件被改坏后基线悄悄消失）。

    同时**间接守住分类的机械判据**：三条"笔记外"组必须都存在且非空 —— 少一组
    就意味着报告会缺一类结论，而报告不会自己发现这一点。
    """
    items = load_out_of_vault(PROJECT_ROOT / "eval" / "out_of_vault.jsonl")

    counts = {group: 0 for group in (GROUP_FAR, GROUP_ADJACENT, GROUP_MENTIONED)}
    for item in items:
        counts[item["group"]] += 1

    assert all(count > 0 for count in counts.values()), counts
    assert len({item["question"] for item in items}) == len(items), "题目不得重复"


def test_mentioned_is_reported_as_its_own_group() -> None:
    """``summarize`` / ``sweep_thresholds`` 都必须给出 ``mentioned`` 一栏。"""
    scores = [
        _score("笔记内", GROUP_GOLDEN, 0.9),
        _score("完全没提", GROUP_ADJACENT, 0.1),
        _score("提及未解释", GROUP_MENTIONED, 0.7),
    ]

    stats = {stat.group: stat.n for stat in summarize(scores)}
    assert stats[GROUP_MENTIONED] == 1

    row = sweep_thresholds(scores)[50]  # t = 0.50
    assert row.adjacent_rejected == 1.0
    assert row.mentioned_rejected == 0.0, "0.7 ≥ 0.5 ⇒ 门槛拒不掉「提及未解释」"


def test_gap_ignores_mentioned_so_the_gate_stays_usable() -> None:
    """``mentioned`` 高于笔记内最低分时，**不得**把结论拖成"不可分"。

    这是本组语义的核心：那类问题的分数与笔记内题重叠是**已知且预期**的，
    真结论是"门槛治不了它，交给答案模板"，而不是"门槛没用"。
    """
    scores = [
        _score("笔记内高", GROUP_GOLDEN, 0.90),
        _score("笔记内低", GROUP_GOLDEN, 0.80),
        _score("远域", GROUP_FAR, 0.05),
        _score("完全没提", GROUP_ADJACENT, 0.10),
        _score("提及未解释", GROUP_MENTIONED, 0.95),
    ]

    threshold, detail = recommend(scores, sweep_thresholds(scores), simulate_truncation(scores))

    assert threshold is not None, "mentioned 不该让门槛判为不可分"
    assert abs(threshold - 0.45) < 0.02, f"空档应取 [0.10, 0.80] 的中点，实得 {threshold}"
    assert "答案模板" in detail, "必须写明「提及未解释」的责任方是模板"


def test_judge_separability_uses_adjacent_not_mentioned() -> None:
    """可分性判据只看 ``adjacent``：``mentioned`` 拒不掉也不影响"可分"结论。"""
    scores = [
        _score("笔记内", GROUP_GOLDEN, 0.90),
        _score("完全没提", GROUP_ADJACENT, 0.10),
        _score("提及未解释", GROUP_MENTIONED, 0.88),
    ]

    verdict, best = judge(sweep_thresholds(scores))

    assert best is not None
    assert verdict.startswith("可分")
    assert "答案模板" in verdict
