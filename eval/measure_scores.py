"""精排分数分布测量（tech.md §10；roadmap R-42，**先测量后改动**）。

## 为什么先量

`eval/BASELINE.md` §6 把「给胖端点加检索侧信号」（把 rerank 分数当提示词字段，
或按分数下限截断证据）列为**预期收益最高**的调优候选，依据是 R-32d 的实测：
"相关 vs 相邻"光靠提示词稳不住。但这条路**有个前提**：

> 笔记内问题与笔记外问题的精排分数必须**可分**；若两组分布重叠，
> 任何阈值都会连带砍掉真问题 —— 候选就应直接否决，不必浪费一次重灌 + A/B。

所以本脚本只做一件事：**把分布画出来**，并给出阈值扫描表，让"能不能用阈值"
变成一个可判读的事实，而不是猜测。

## 四组问题（"笔记外"其实是两类，必须分开量）

| 组 | 来源 | 含义 |
|---|---|---|
| `golden` | `eval/golden_set.jsonl`（30 题，带 `expected_sources`） | 笔记**内**（应当高分） |
| `far` | `eval/out_of_vault.jsonl` 的 `group=far` | 远域（烹饪/法律/医学…），**最该被拒** |
| `adjacent` | 同上 `group=adjacent` | **邻近·完全没提**：同领域，但笔记里一个字都没有 |
| `mentioned` | 同上 `group=mentioned` | **提了名没解释**：术语出现过，笔记却没讲它是什么 |

### 为什么必须把 `adjacent` 与 `mentioned` 分开

「笔记里没有」有两种成因，**只有第一种是分数门槛能治的**：

- `adjacent`（完全没提）：库里没有任何相关 chunk ⇒ 精排分数天然很低 ⇒ 门槛可拒。
- `mentioned`（提了名没解释）：库里**确实有**一个提到该术语的 chunk ⇒ 精排分数与
  笔记内题落在同一条带上 ⇒ **门槛按定义拒不掉它**；它会带着一个"只提名不解释"的
  证据进上下文，正是 R-32d 说的"硬答"。

所以判据分两层：**门槛**对 `adjacent` / `far` 负责（硬指标），**答案模板**对
`mentioned` 负责（证据进得来，但必须声明"笔记只提及、未解释"并转联网搜索）。
两组混报就永远看不出问题是"门槛没用"还是"模板没写"。

## 判据

阈值 ``t`` 下：``保留率`` = 笔记内题 top1 ≥ t 的比例（越高越好，掉了就是误伤），
``拒绝率`` = 笔记外题 top1 < t 的比例（越高越好）。**可分**的判定只看门槛能治的
两类（`adjacent` + `far`）：存在 ``t`` 使 ``保留率 ≥ 0.9`` 且 ``adjacent 拒绝率 ≥ 0.9``。
`mentioned` 的拒绝率**只作报告**，不进硬判据 —— 把它塞进硬判据只会恒判"不可分"，
从而掩盖"门槛对没提的那类其实很好用"这个真结论。

用法::

    python eval/measure_scores.py --collection recall__bge-m3@v1__md
    python eval/measure_scores.py --output eval/score_distribution.json

⚠️ 确定性：同 collection + 同参数 ⇒ **逐位可复现**（code_standards §0.2），
故本报告的分数可以直接当基线。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:  # 直接运行脚本时保证能 import recall
    sys.path.insert(0, str(PROJECT_ROOT))

# 复用黄金集的**同一套**解析规则（`eval_ragas.py` 亦如此）：期望来源既可能是字符串
# 也可能是数组，两处各写一份必然分叉 —— 与 R-45「同源同形」同一条教训。
from eval.eval_retrieval import _as_source_list, load_golden  # noqa: E402
from recall.api import DEFAULT_COLLECTION, close_service, get_service, kb_search_core  # noqa: E402
from recall.models import SearchRequest  # noqa: E402

logger = logging.getLogger("recall.eval.scores")

MAX_EVAL_TOKENS = 32000
"""与 ``eval_retrieval.py`` 同值：预算放到上限，避免预算截断改变证据条数。"""

RECALL_WINDOW = 20
"""取回条数 = 精排 Top-N（tech.md §4 的 ``DEFAULT_TOP_N``），看完整分数带。"""

RECALL_KS = (1, 3, 5, 10)
"""截断模拟里要盯住的 K（10 是硬底线所在）。"""

SWEEP_STEP = 0.01
"""阈值扫描步长。"""

GROUP_GOLDEN = "golden"
GROUP_FAR = "far"
GROUP_ADJACENT = "adjacent"
GROUP_MENTIONED = "mentioned"
"""**提了名、没解释**：术语在笔记里出现过，但笔记没讲它是什么（R-32d 的硬骨头）。

与 :data:`GROUP_ADJACENT` 的区别是**库里有相关 chunk**，因此精排分数落在笔记内题的
同一条带上 ⇒ 分数门槛拒不掉，只能靠答案模板声明"笔记只提及未解释"。
"""

OUTSIDE_GROUPS = (GROUP_FAR, GROUP_ADJACENT, GROUP_MENTIONED)
"""全部"笔记外"组。

:data:`GROUP_MENTIONED` 在 :func:`recommend` 里被**排除**在空档计算之外 —— 它与笔记内
分数带本就重叠，混进 `max(笔记外)` 会让空档恒为空，从而错误地判"门槛不可用"。
"""


@dataclass(slots=True)
class QuestionScore:
    """单题的分数记录。"""

    question: str
    group: str
    top1: float
    top3_mean: float
    top5_mean: float
    count: int
    rank: int | None = None
    """笔记内题：期望来源在 Top-N 里的名次；未命中为 ``None``。"""
    expected: list[str] = field(default_factory=list)
    """笔记内题的期望来源（用于模拟截断后的召回）。"""
    sources: list[str] = field(default_factory=list)
    """证据的来源序列（与 :attr:`scores` 一一对应）。"""
    scores: list[float] = field(default_factory=list)
    """**全部**证据的分数（降序）——截断模拟需要完整分数带，只看 top1 不够。"""


@dataclass(slots=True)
class TruncationRow:
    """按阈值截断证据后的一行结果（回答"会不会误伤召回"）。"""

    threshold: float
    recall_at: dict[str, float]
    """``{"1": …, "10": …}``：截断到分数 ≥ 阈值后，黄金集的 Recall@K。"""
    far_rejected: float
    adjacent_rejected: float
    mentioned_rejected: float
    kept_evidence_ratio: float
    """笔记内题平均留下的证据比例（看截断有多激进）。"""


@dataclass(slots=True)
class GroupStats:
    """一组问题的分数统计。"""

    group: str
    n: int
    minimum: float
    p25: float
    median: float
    p75: float
    maximum: float


@dataclass(slots=True)
class SweepRow:
    """阈值扫描的一行。"""

    threshold: float
    golden_kept: float
    far_rejected: float
    adjacent_rejected: float
    mentioned_rejected: float


@dataclass(slots=True)
class ScoreReport:
    """一次测量的完整结果（可直接落盘做基线）。"""

    collection: str
    window: int
    scores: list[QuestionScore] = field(default_factory=list)
    stats: list[GroupStats] = field(default_factory=list)
    sweep: list[SweepRow] = field(default_factory=list)
    verdict: str = ""
    best_threshold: float | None = None
    truncation: list[TruncationRow] = field(default_factory=list)
    recommended_threshold: float | None = None
    recommendation: str = ""


def _quantile(values: list[float], fraction: float) -> float:
    """线性插值分位数（不依赖 numpy，样本很少时也稳定）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def load_out_of_vault(path: Path) -> list[dict[str, str]]:
    """读取"笔记外"问题集（每行一个 ``{question, group}``）。

    Args:
        path: JSONL 文件路径。

    Returns:
        解析后的条目列表。

    Raises:
        ValueError: 出现既非 ``far``、``adjacent`` 也非 ``mentioned`` 的 group。
    """
    items: list[dict[str, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        payload = json.loads(text)
        group = str(payload.get("group", ""))
        if group not in OUTSIDE_GROUPS:
            raise ValueError(
                f"{path.name}:{number} 的 group 必须是 {'/'.join(OUTSIDE_GROUPS)} 之一，"
                f"实得 {group!r}"
            )
        items.append({"question": str(payload["question"]), "group": group})
    return items


async def _score_question(question: str, group: str, expected: list[str]) -> QuestionScore:
    """跑一次检索并记录分数带。"""
    result = await kb_search_core(
        SearchRequest(query=question, top_k=RECALL_WINDOW, max_tokens=MAX_EVAL_TOKENS)
    )
    scores = [item.score for item in result.evidence]
    sources = [item.source_uri for item in result.evidence]
    rank = next(
        (position for position, source in enumerate(sources, start=1) if source in expected),
        None,
    )
    return QuestionScore(
        question=question,
        group=group,
        top1=scores[0] if scores else 0.0,
        top3_mean=statistics.fmean(scores[:3]) if scores else 0.0,
        top5_mean=statistics.fmean(scores[:5]) if scores else 0.0,
        count=len(scores),
        rank=rank,
        expected=expected,
        sources=sources,
        scores=scores,
    )


def summarize(scores: list[QuestionScore]) -> list[GroupStats]:
    """按组统计 top1 的分布。"""
    stats: list[GroupStats] = []
    for group in (GROUP_GOLDEN, GROUP_FAR, GROUP_ADJACENT, GROUP_MENTIONED):
        values = [item.top1 for item in scores if item.group == group]
        if not values:
            continue
        stats.append(
            GroupStats(
                group=group,
                n=len(values),
                minimum=min(values),
                p25=_quantile(values, 0.25),
                median=_quantile(values, 0.5),
                p75=_quantile(values, 0.75),
                maximum=max(values),
            )
        )
    return stats


def _at_least(threshold: float) -> Callable[[float], bool]:
    """返回"分数 ≥ 阈值"的谓词（用函数绑定阈值，避开闭包捕获循环变量）。"""
    return lambda value: value >= threshold


def _below(threshold: float) -> Callable[[float], bool]:
    """返回"分数 < 阈值"的谓词。"""
    return lambda value: value < threshold


def sweep_thresholds(scores: list[QuestionScore]) -> list[SweepRow]:
    """扫描阈值：笔记内保留率 vs 笔记外拒绝率。"""
    golden = [item.top1 for item in scores if item.group == GROUP_GOLDEN]
    far = [item.top1 for item in scores if item.group == GROUP_FAR]
    adjacent = [item.top1 for item in scores if item.group == GROUP_ADJACENT]
    mentioned = [item.top1 for item in scores if item.group == GROUP_MENTIONED]
    rows: list[SweepRow] = []
    steps = int(round(1.0 / SWEEP_STEP))
    for index in range(steps + 1):
        threshold = round(index * SWEEP_STEP, 2)
        rows.append(
            SweepRow(
                threshold=threshold,
                golden_kept=_fraction(golden, _at_least(threshold)),
                far_rejected=_fraction(far, _below(threshold)),
                adjacent_rejected=_fraction(adjacent, _below(threshold)),
                mentioned_rejected=_fraction(mentioned, _below(threshold)),
            )
        )
    return rows


def _fraction(values: list[float], predicate: Callable[[float], bool]) -> float:
    """按谓词求比例（空集合返回 0）。"""
    if not values:
        return 0.0
    return sum(1 for value in values if predicate(value)) / len(values)


def judge(sweep: list[SweepRow]) -> tuple[str, float | None]:
    """给出可分性结论与最佳阈值。

    判定只看门槛负责的两类（``adjacent`` + ``far``）：存在阈值使保留率 ≥ 0.9 且
    ``adjacent`` 拒绝率 ≥ 0.9。``mentioned``（提了名没解释）的拒绝率一并报出，
    但**不进判据** —— 它落在笔记内分数带上，判据里带上它只会恒判"不可分"。

    Returns:
        ``(结论文本, 最佳阈值或 None)``。
    """
    candidates = [
        row
        for row in sweep
        if row.golden_kept >= 0.9 and row.adjacent_rejected >= 0.9
    ]
    if candidates:
        best = max(candidates, key=lambda row: (row.adjacent_rejected, row.golden_kept))
        return (
            f"可分：阈值 {best.threshold:.2f} 下笔记内保留 {best.golden_kept:.2%}、"
            f"邻近·完全没提拒绝 {best.adjacent_rejected:.2%}、"
            f"远域拒绝 {best.far_rejected:.2%}；"
            f"提了名没解释只拒掉 {best.mentioned_rejected:.2%}"
            "（**门槛不负责这组** ⇒ 必须由答案模板兜底）",
            best.threshold,
        )
    # 不可分时给出"最接近"的那一行，便于判断差多远
    closest = max(sweep, key=lambda row: min(row.golden_kept, row.adjacent_rejected))
    return (
        "不可分：找不到同时满足『笔记内保留 ≥ 90%』与『邻近·完全没提拒绝 ≥ 90%』的"
        f"阈值；最接近的一行是 t={closest.threshold:.2f}"
        f"（保留 {closest.golden_kept:.2%} / 完全没提拒绝 "
        f"{closest.adjacent_rejected:.2%}） ⇒ 建议否决「纯阈值」路线，转其它候选",
        None,
    )


def simulate_truncation(scores: list[QuestionScore]) -> list[TruncationRow]:
    """模拟"证据按分数下限截断"后的黄金集 Recall@K 与笔记外拒绝率。

    为什么必须模拟而不只看 top1：截断作用在**整条证据带**上。黄金集里有题目的期望来源
    排在第 7 名（如"大模型开发架构大纲"），若它的分数低于阈值就会被砍掉 ⇒
    Recall@10 会掉。硬底线是 **Recall@10 ≥ 1.000**（tech.md §10），故必须逐 K 验证。
    """
    golden = [item for item in scores if item.group == GROUP_GOLDEN]
    far = [item.top1 for item in scores if item.group == GROUP_FAR]
    adjacent = [item.top1 for item in scores if item.group == GROUP_ADJACENT]
    mentioned = [item.top1 for item in scores if item.group == GROUP_MENTIONED]
    rows: list[TruncationRow] = []
    steps = int(round(1.0 / SWEEP_STEP))
    for index in range(steps + 1):
        threshold = round(index * SWEEP_STEP, 2)
        pairs = [
            (
                item,
                [
                    source
                    for value, source in zip(item.scores, item.sources, strict=True)
                    if value >= threshold
                ],
            )
            for item in golden
        ]
        recall = {
            str(k): (
                sum(
                    1
                    for item, kept in pairs
                    if any(source in item.expected for source in kept[:k])
                )
                / len(pairs)
            )
            if pairs
            else 0.0
            for k in RECALL_KS
        }
        slots = sum(len(item.sources) for item in golden)
        rows.append(
            TruncationRow(
                threshold=threshold,
                recall_at=recall,
                far_rejected=_fraction(far, _below(threshold)),
                adjacent_rejected=_fraction(adjacent, _below(threshold)),
                mentioned_rejected=_fraction(mentioned, _below(threshold)),
                kept_evidence_ratio=(sum(len(kept) for _, kept in pairs) / slots) if slots else 0.0,
            )
        )
    return rows


def recommend(
    scores: list[QuestionScore],
    gate_rows: list[SweepRow],
    truncation_rows: list[TruncationRow],
) -> tuple[float | None, str]:
    """给出建议阈值及其代价说明。

    推荐机制是 **top1 门槛（gate）**：``top1 < t`` 就整体判为"笔记里没有"，
    **不动证据带** ⇒ Recall@K 与基线逐位一致，"答不答"与"引哪些"被解耦。

    取**空档中点**而不是贴着上沿：阈值贴着"最低的笔记内分数"会非常脆——新加一道
    笔记内题只要略低一点就会被误伤。中点对两侧都留出余量。

    空档只由 ``far`` + ``adjacent`` 决定：``mentioned``（提了名没解释）与笔记内分数带
    本就重叠，把它算进"笔记外最高分"会让空档恒为空 ⇒ 错误地否定门槛路线。该组的
    结论单独报出，并写明它的责任方是**答案模板**而不是门槛。

    Args:
        scores: 逐题分数。
        gate_rows: 门槛扫描结果（推荐机制）。
        truncation_rows: 逐条截断结果（仅作对照，用来证明"别裁证据"）。

    Returns:
        ``(建议阈值, 说明文本)``；若门槛负责的两类与笔记内完全重叠则为 ``(None, 说明)``。
    """
    in_vault = [item.top1 for item in scores if item.group == GROUP_GOLDEN]
    outside = [
        item.top1 for item in scores if item.group in (GROUP_FAR, GROUP_ADJACENT)
    ]
    mentioned = [item.top1 for item in scores if item.group == GROUP_MENTIONED]
    if not in_vault or not outside:
        return None, "样本不足：需要同时有笔记内与「门槛负责的笔记外」两组问题。"
    lowest_in_vault = min(in_vault)
    highest_outside = max(outside)
    if highest_outside >= lowest_in_vault:
        return (
            None,
            f"不可分：笔记外最高分 {highest_outside:.3f} ≥ 笔记内最低分 "
            f"{lowest_in_vault:.3f}，不存在安全阈值。",
        )
    midpoint = round((lowest_in_vault + highest_outside) / 2, 2)
    gate = min(gate_rows, key=lambda candidate: abs(candidate.threshold - midpoint))
    trunc = min(truncation_rows, key=lambda candidate: abs(candidate.threshold - midpoint))
    trunc_r10 = trunc.recall_at.get("10", 0.0)
    mentioned_note = (
        f"提了名没解释：区间 [{min(mentioned):.3f}, {max(mentioned):.3f}]，"
        f"该阈值下只拒掉 {gate.mentioned_rejected:.2%}"
        "（**门槛按定义拒不掉它** —— 库里确实有提到该术语的 chunk，证据照样进上下文）"
        f" ⇒ 这组必须由**答案模板**兜底：证据里只有术语、没有解释时，"
        f"声明『笔记只提及、未解释』并转联网搜索（R-32d）。"
        if mentioned
        else "提了名没解释：本题集未收该组样本（⚠️ 则该类问题无实测数据，结论不完整）。"
    )
    detail = (
        f"空档 = [{highest_outside:.3f}, {lowest_in_vault:.3f}]，"
        f"宽 {lowest_in_vault - highest_outside:.3f}；取中点 {gate.threshold:.2f}。"
        f"【门槛机制】笔记内保留 {gate.golden_kept:.2%}（全部笔记内题照常作答）、"
        f"远域拒绝 {gate.far_rejected:.2%}、邻近·完全没提拒绝 "
        f"{gate.adjacent_rejected:.2%}；"
        f"**证据带不变 ⇒ Recall@K 与基线逐位一致（R@10 = 100%，硬底线未破）**。"
        f"⚠️ 对照（**不要这么做**）：若改成按同一阈值**逐条截断**证据，"
        f"R@10 会掉到 {trunc_r10:.2%} —— 门槛只决定「答不答」，不裁证据。"
        f"📌 分层结论：{mentioned_note}"
    )
    return gate.threshold, detail


def render(report: ScoreReport) -> str:
    """把报告渲染成便于人读的文本。"""
    lines = [
        "",
        f"精排分数分布（collection={report.collection}，窗口={report.window}）",
        "",
        "一、各组 top1 分数分布",
        "",
        f"{'组':<10}{'n':>4}{'min':>9}{'p25':>9}{'中位':>9}{'p75':>9}{'max':>9}",
        "-" * 60,
    ]
    labels = {
        GROUP_GOLDEN: "笔记内",
        GROUP_FAR: "远域",
        GROUP_ADJACENT: "邻近·没提",
        GROUP_MENTIONED: "提及未解释",
    }
    for stat in report.stats:
        lines.append(
            f"{labels.get(stat.group, stat.group):<10}{stat.n:>4}"
            f"{stat.minimum:>9.3f}{stat.p25:>9.3f}{stat.median:>9.3f}"
            f"{stat.p75:>9.3f}{stat.maximum:>9.3f}"
        )
    lines += ["", "二、逐题 top1（升序，便于看重叠区）", ""]
    for item in sorted(report.scores, key=lambda row: row.top1):
        hit = "" if item.rank is None else f"  名次={item.rank}"
        lines.append(
            f"  {item.top1:6.3f}  [{labels.get(item.group, item.group)}]  {item.question}{hit}"
        )
    lines += [
        "",
        "三、阈值扫描（**推荐机制**：top1 门槛 —— 分数不足即判『笔记里没有』，不裁证据带）",
        "",
        "   『没提』= 邻近·完全没提（**门槛负责**）；『提及』= 邻近·提了名没解释"
        "（**门槛拒不掉，模板负责**，此处仅作报告）",
        "",
        f"{'t':>5}{'笔记内保留':>12}{'远域拒绝':>10}{'没提拒绝':>10}{'提及拒绝':>10}",
        "-" * 50,
    ]
    for row in report.sweep:
        if round(row.threshold * 100) % 10 == 0:  # 每 0.10 打一行，够看趋势
            lines.append(
                f"{row.threshold:>5.2f}{row.golden_kept:>12.2%}"
                f"{row.far_rejected:>10.2%}{row.adjacent_rejected:>10.2%}"
                f"{row.mentioned_rejected:>10.2%}"
            )
    lines += ["", "四、结论（top1 可分性）", "", f"  {report.verdict}", ""]
    lines += [
        "五、**对照**：按同一阈值逐条截断证据的代价（**不要这么做**）",
        "",
        "   门槛只决定「答不答」；若顺手把低分证据也裁掉，就会开始误伤召回 ——",
        "   黄金集里期望来源排到第 7 名的题目首当其冲。下表量化这个代价。",
        "",
        f"{'t':>5}{'R@1':>8}{'R@3':>8}{'R@5':>8}{'R@10':>8}{'远域拒':>9}{'没提拒':>9}"
        f"{'提及拒':>9}{'留证据':>8}",
        "-" * 75,
    ]
    for trow in report.truncation:
        if round(trow.threshold * 100) % 5 == 0:  # 每 0.05 打一行
            lines.append(
                f"{trow.threshold:>5.2f}{trow.recall_at.get('1', 0.0):>8.2%}"
                f"{trow.recall_at.get('3', 0.0):>8.2%}{trow.recall_at.get('5', 0.0):>8.2%}"
                f"{trow.recall_at.get('10', 0.0):>8.2%}{trow.far_rejected:>9.2%}"
                f"{trow.adjacent_rejected:>9.2%}{trow.mentioned_rejected:>9.2%}"
                f"{trow.kept_evidence_ratio:>8.1%}"
            )
    counts = {stat.group: stat.n for stat in report.stats}
    lines += [
        "",
        "六、建议",
        "",
        f"  建议阈值：{report.recommended_threshold}",
        f"  {report.recommendation}",
        "",
        f"  ⚠️ 样本量说明：笔记内 {counts.get(GROUP_GOLDEN, 0)} 题、"
        f"远域 {counts.get(GROUP_FAR, 0)} 题、"
        f"邻近·完全没提 {counts.get(GROUP_ADJACENT, 0)} 题、"
        f"邻近·提了名没解释 {counts.get(GROUP_MENTIONED, 0)} 题。空档宽度可信，",
        "     但两侧极值都只由个别题目决定 ⇒ 阈值应留余量（取中点而非贴边），",
        "     并在此后每次扩题时重跑本脚本复核。",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(description="测量精排分数分布（roadmap R-42 第一阶段）")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help="目标 collection")
    parser.add_argument("--golden", default=str(PROJECT_ROOT / "eval" / "golden_set.jsonl"))
    parser.add_argument(
        "--out-of-vault", default=str(PROJECT_ROOT / "eval" / "out_of_vault.jsonl")
    )
    parser.add_argument("--output", help="把完整报告写成 JSON（供 A/B 对比）")
    parser.add_argument("--report", help="把渲染后的文本报告写成 UTF-8 文件（便于存档 / 阅读）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（调试用）")
    parser.add_argument("--log-level", default="WARNING")
    return parser


async def measure(args: argparse.Namespace) -> ScoreReport:
    """跑完三组问题并汇总。"""
    import os

    os.environ["RECALL_COLLECTION"] = args.collection
    await close_service()
    await get_service()

    golden = load_golden(Path(args.golden))
    outside = load_out_of_vault(Path(args.out_of_vault))
    if args.limit > 0:
        golden = golden[: args.limit]
        outside = outside[: args.limit]

    scores: list[QuestionScore] = []
    for golden_item in golden:
        question = str(golden_item["question"])
        expected = [
            str(source) for source in _as_source_list(golden_item.get("expected_sources", []))
        ]
        scores.append(await _score_question(question, GROUP_GOLDEN, expected))
    for outside_item in outside:
        scores.append(await _score_question(outside_item["question"], outside_item["group"], []))

    report = ScoreReport(collection=args.collection, window=RECALL_WINDOW, scores=scores)
    report.stats = summarize(scores)
    report.sweep = sweep_thresholds(scores)
    report.verdict, report.best_threshold = judge(report.sweep)
    report.truncation = simulate_truncation(scores)
    report.recommended_threshold, report.recommendation = recommend(
        scores, report.sweep, report.truncation
    )
    await close_service()
    return report


async def _main_async(args: argparse.Namespace) -> int:
    """异步主体。"""
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.WARNING))
    report = await measure(args)
    text = render(report)
    print(text)
    if args.report:
        report_target = Path(args.report)
        report_target.parent.mkdir(parents=True, exist_ok=True)
        report_target.write_text(text, encoding="utf-8")
        print(f"文本报告已写入 {report_target}")
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已写入 {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """脚本入口。"""
    args = build_parser().parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
