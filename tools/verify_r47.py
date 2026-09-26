#!/usr/bin/env python
"""R-47 验收脚本：「提了名没解释」的回答模板（roadmap R-47，2026-09-26 批准）。

对照 ``spec/roadmap.md`` §五 R-47 与 ``spec/tech.md`` §4.1 / §17 决策记录 17，
把**胖端点那一半**验成机器可判的：把 ``eval/out_of_vault.jsonl`` 里 10 道
``group=mentioned`` 的题逐个丢给 ``POST /kb/answer``，看它有没有"拿提名当解释"。

三层判读（脚本会明确区分，因为**责任方不同**）：

1. ``gate_declined`` —— 证据门槛拦下的（那几道题精排分低于 ``RECALL_EVIDENCE_MIN_SCORE``）：
   服务端直接答"笔记里没有检索到…"，**一次 LLM 都不调**。这是**检索侧**的功劳；
2. ``template_declined`` —— 证据进得来、但模板让它声明"笔记只提及、未解释"：
   这是**生成侧**（R-47）的功劳，也是本脚本真正要验的东西；
3. ``hard_answer`` —— 既没被门槛拦、也没按模板声明 ⇒ **FAIL**，正是 R-32d 禁止的硬答。

⚠️ 对照组：黄金集里"笔记确实解释了"的题**不得**出现拒答措辞 ——
2026-09-24 那次"相邻主题必须点名"的规则就是在这里翻车的（过度拒答），
所以 R-47 落地必须连带回归它。

❗ **仍属人工的一步**：本机胖端点**没有联网能力**，它只能声明"笔记只提及、未解释"。
R-47 要求的另一半（"通过联网搜索去找答案"）只能由**带搜索工具的调用方**（DSH Agent）
完成 ⇒ 请用这 10 道题在 DSH 里问一遍，确认答案**同时**出现"笔记未解释"与联网补充。

用法::

    python tools/verify_r47.py
    python tools/verify_r47.py --api-key <token>       # 服务已启用鉴权
    python tools/verify_r47.py --limit 3               # 每组只跑前 N 题（省 LLM 额度）

退出码 = 失败项数（0 即通过）。

.. note::
    本脚本刻意用 **Python** 而非 PowerShell：Windows 客户端默认 ``ExecutionPolicy = Restricted``，
    ``.ps1`` 需要 ``-ExecutionPolicy Bypass`` 才能跑，而 Python 是项目既有运行时。
    也刻意**不使用 ANSI 颜色** —— 老版 cmd.exe 会把转义序列原样打成乱码。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# 统一输出编码：Windows 下 Python 对**管道/重定向**的 stdout 用本地代码页（cp936），
# 而本项目全链路 UTF-8 ⇒ 不统一就会"控制台正常、重定向乱码"（同 verify_r45.py）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TIMEOUT_S = 180.0
"""单次 ``/kb/answer`` 超时。胖端点要过一次 DeepSeek，比检索端点慢得多。"""

GATE_DECLINE_MARKERS = ("笔记里没有检索到",)
"""门槛拒答的固定措辞（``recall/api.py::kb_answer_core``）⇒ 说明证据被拦。**

⚠️ 用它区分"门槛拒答"与"模板拒答"：前者**没调 LLM**，后者调了。
"""

TEMPLATE_DECLINE_MARKERS = (
    "只提及",
    "仅提及",
    "只提到",
    "仅提到",
    "只是提到",
    "未解释",
    "没有解释",
    "未展开",
    "没有展开",
    "未给出",
    "没有给出",
    "未涉及",
    "只列出",
    "仅列出",
    "只点名",
    "仅点名",
    "笔记里只",
    "笔记中只",
)
"""模板声明"提了名没解释"时的措辞（R-47 提示词要求的语义词）。

⚠️ 这张表是**启发式**：它只能覆盖常见措辞，判不准是它的固有限制。2026-09-26 实测踩到过一次
**假阴性** —— 模型写的是"只列出了名字 / 没有展开 / 未给出解释"这类同义表达，不在**旧表**里，
于是被判成"硬答"并报 FAIL（第 7 题 GraphRAG）。故：① 表已放宽到覆盖常见同义写法；
② 判成 ``hard_answer`` 时下面的提示会**自曝可能是假阴性**，请连答案原文一起看，
以人工判读为准（脚本只是筛子，不是裁判）。
"""

NOT_IN_NOTES_MARKERS = ("笔记里没有", "笔记中没有", "没有找到")
"""黄金集对照组**不得**出现的拒答措辞。"""


@dataclass(frozen=True, slots=True)
class Outcome:
    """一道题的判读结果。"""

    question: str
    kind: str
    """``gate_declined`` / ``template_declined`` / ``hard_answer`` / ``answered`` / ``empty``。"""
    answer: str
    citations: list[int]


class Report:
    """收集检查结果并即时打印。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        """记录并打印一项检查。"""
        if ok:
            self.passed += 1
            print(f"  [ OK ] {name}")
        else:
            self.failed += 1
            print(f"  [FAIL] {name}")
            if detail:
                print(f"         -> {detail}")

    def info(self, text: str) -> None:
        """打印一条信息（不计入通过/失败）。"""
        print(f"  [INFO] {text}")


def step(title: str) -> None:
    """打印一个步骤标题。"""
    print()
    print(title)


def _load_questions(path: Path, group: str, limit: int) -> list[str]:
    """从 JSONL 里取某一组的题目（保持文件顺序，可复现）。"""
    questions: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        payload = json.loads(text)
        if str(payload.get("group", "")) == group:
            questions.append(str(payload["question"]))
    return questions[:limit] if limit > 0 else questions


def _load_golden(path: Path, limit: int) -> list[str]:
    """取黄金集前 N 题作为**对照组**（这些题笔记里确实讲过，不得拒答）。"""
    questions: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        questions.append(str(json.loads(text)["question"]))
    return questions[:limit] if limit > 0 else questions


def ask(base: str, headers: dict[str, str], question: str) -> Outcome:
    """调一次 ``/kb/answer`` 并判读它属于哪一类。"""
    body = json.dumps({"query": question}).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/kb/answer",
        data=body,
        method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        return Outcome(question, "error", f"HTTP {exc.code}: {detail}", [])
    except OSError as exc:
        return Outcome(question, "error", f"{type(exc).__name__}: {exc}", [])

    answer = str(payload.get("answer", ""))
    citations = [int(item) for item in payload.get("citations", [])]
    if any(marker in answer for marker in GATE_DECLINE_MARKERS):
        kind = "gate_declined"
    elif any(marker in answer for marker in TEMPLATE_DECLINE_MARKERS):
        kind = "template_declined"
    elif answer.strip():
        kind = "hard_answer"
    else:
        kind = "empty"
    return Outcome(question, kind, answer, citations)


def _print_outcome(index: int, item: Outcome) -> None:
    """打印一道题的完整答案（人工复核要看原文）。"""
    print(f"  {index:2d}. [{item.kind}] {item.question}")
    print(f"      答：{item.answer[:300] or '(空)'}")
    if item.citations:
        print(f"      引用：{item.citations}")


def main() -> int:
    """跑完全部检查，返回失败项数。"""
    parser = argparse.ArgumentParser(description="R-47 回答模板验收（提了名没解释）")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="服务地址")
    parser.add_argument("--api-key", default="", help="启用鉴权时的 X-API-Key")
    parser.add_argument(
        "--limit", type=int, default=0, help="每组只跑前 N 题（省 LLM 额度；0 = 全部）"
    )
    parser.add_argument(
        "--out-of-vault",
        default=str(REPO_ROOT / "eval" / "out_of_vault.jsonl"),
        help="笔记外题集（取 group=mentioned）",
    )
    parser.add_argument(
        "--golden", default=str(REPO_ROOT / "eval" / "golden_set.jsonl"), help="黄金集（对照组）"
    )
    args = parser.parse_args()

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    base = args.base.rstrip("/")
    report = Report()

    print("=" * 70)
    print(" R-47 验收：回答模板的「提了名没解释」规则")
    print(f" 目标服务：{base}")
    print("=" * 70)

    mentioned = _load_questions(Path(args.out_of_vault), "mentioned", args.limit)
    step(f"一、「提了名没解释」{len(mentioned)} 题（必须声明「笔记只提及、未解释」，不得硬答）")
    if not mentioned:
        report.check("题集里有 mentioned 组", False, "eval/out_of_vault.jsonl 里没有该组")
        return report.failed

    kinds: dict[str, int] = {}
    for index, question in enumerate(mentioned, start=1):
        item = ask(base, headers, question)
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
        _print_outcome(index, item)
        report.check(
            f"第 {index} 题未被硬答（{item.kind}）",
            item.kind in {"gate_declined", "template_declined"},
            f"实得 {item.kind}：{item.answer[:120]}"
            "  ⚠️ 判成 hard_answer 时请连答案原文一起看 —— 若它其实已声明『只提及 / 未解释』，"
            "那就是关键词表没命中（假阴性），以人工判读为准",
        )

    step("二、判读汇总（责任方不同，必须分开看）")
    report.info(f"门槛拦下（不调 LLM）：{kinds.get('gate_declined', 0)}/{len(mentioned)}")
    report.info(f"模板声明（R-47 生效）：{kinds.get('template_declined', 0)}/{len(mentioned)}")
    report.info(f"硬答（FAIL）：{kinds.get('hard_answer', 0)}/{len(mentioned)}")
    report.info(
        "📌 若『模板声明』为 0 而『门槛拦下』为 10 ⇒ 说明门槛在跑、但模板规则没生效"
        "（多半是服务进程没重启，仍跑着旧提示词）"
    )

    control = _load_golden(Path(args.golden), args.limit or 5)
    step(f"三、对照组：黄金集 {len(control)} 题（笔记确实讲过 ⇒ **不得**拒答）")
    for index, question in enumerate(control, start=1):
        item = ask(base, headers, question)
        refused = any(marker in item.answer for marker in NOT_IN_NOTES_MARKERS)
        _print_outcome(index, item)
        report.check(
            f"对照第 {index} 题照常作答（未过度拒答）",
            not refused and bool(item.answer.strip()),
            f"实得：{item.answer[:120]}",
        )

    step("仍需人工完成的一步（本机胖端点无联网能力）")
    report.info(
        "用这 10 道题在 **DSH 里问一遍**：答案须**同时**出现『笔记只提及/未解释』"
        "与**联网补充**的内容（R-47 的另一半）。"
    )

    print()
    print("=" * 70)
    print(f" 结论：{'通过' if report.failed == 0 else '不通过'} "
          f"（通过 {report.passed} / 失败 {report.failed}）")
    print("=" * 70)
    return report.failed


if __name__ == "__main__":
    sys.exit(main())
