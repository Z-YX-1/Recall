#!/usr/bin/env python
"""对比「瘦核心 `/kb/search`」与「胖端点 `/kb/answer`」在同一问题上的**证据与答案**。

**为什么需要它**：2026-10-03 项目工程师发现 —— 同一个问题，**飞书机器人答错**，
而 **DSH 答对（准确率接近 100%）**。两条路用的是**同一套检索链路**，差别在：

======================  ==========================================
DSH / Coze              走 MCP ``kb_search``（**瘦核心**）⇒ 证据交给 agent，答案由 agent 组装
飞书机器人 / 客服       走 ``POST /kb/answer``（**胖端点**）⇒ 服务端自己检索 + 组装 + 调 DeepSeek
======================  ==========================================

所以只要把两者的**证据包**并排打出来，就能判定问题出在哪一段：

- **检索没召回**（该在的证据压根没出现）⇒ 检索/切分问题；
- **召回了但被预算截掉**（高分"只提及"块挤掉了低分"有解释"块）⇒ 预算/排序问题；
- **证据齐了但答案仍说"没解释"** ⇒ 生成侧把 R-47 规则**用过头了**。

用法::

    python tools/diagnose_answer.py --question "切分粒度对召回的影响是什么样的"
    python tools/diagnose_answer.py --question "…" --full          # 打印完整证据正文
    python tools/diagnose_answer.py --question "…" --max-tokens 8000

只读、无副作用（不发消息、不写库）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_S = 180.0
PREVIEW_CHARS = 320


def load_env() -> dict[str, str]:
    """从 `.env` 读键值（只读）。"""
    out: dict[str, str] = {}
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        return out
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def default_api_key(env: dict[str, str], explicit: str | None) -> str | None:
    """回环调用本机 API 用的 token。"""
    if explicit:
        return explicit
    if env.get("RECALL_WATCHDOG_API_KEY"):
        return env["RECALL_WATCHDOG_API_KEY"]
    first = (env.get("RECALL_API_KEYS") or "").split(",")[0]
    token = first.partition(":")[0].strip()
    return token or None


def post_json(
    url: str, body: dict[str, Any], api_key: str | None, timeout: float
) -> dict[str, Any]:
    """POST 一个 JSON 端点。

    ⚠️ **必须传 UTF-8 字节**：PowerShell 5.1 用字符串传中文会被按 ANSI 编码弄坏
    （实测过一次查询因此返回 0 条证据，看着像产品故障）。
    """
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(  # noqa: S310 - 本机固定 http 回环地址
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            return payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {"__error__": f"HTTP {exc.code}", "__body__": raw[:400]}
    except Exception as exc:  # noqa: BLE001 - 探针要报告任何失败
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def section_search(
    api_url: str,
    api_key: str | None,
    question: str,
    top_k: int,
    max_tokens: int,
    timeout: float,
    full: bool,
) -> dict[str, Any]:
    """B. 瘦核心：把证据包逐条打出来。"""
    print("\n=== B. 瘦核心 POST /kb/search（DSH/Coze 走的就是这条）===")
    payload = post_json(
        f"{api_url.rstrip('/')}/kb/search",
        {"query": question, "top_k": top_k, "max_tokens": max_tokens},
        api_key,
        timeout,
    )
    if "__error__" in payload:
        print(f"  [FAIL] {payload['__error__']} {payload.get('__body__', '')}")
        return {}
    evidence = payload.get("evidence") or []
    print(f"  证据条数 = {len(evidence)}（请求 top_k={top_k}, max_tokens={max_tokens}）")
    for item in evidence:
        text = str(item.get("text") or "")
        shown = text if full else text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")
        print(f"\n  [ref {item.get('ref_id')}] score={item.get('score'):.4f} chars={len(text)}")
        print(f"      来源: {item.get('source_uri')}")
        print(f"      小节: {item.get('heading_path')}")
        print(f"      正文: {shown}")
    return payload


def section_answer(
    api_url: str, api_key: str | None, question: str, max_tokens: int, timeout: float
) -> dict[str, Any]:
    """C. 胖端点：打印它生成的答案与引用。"""
    print("\n=== C. 胖端点 POST /kb/answer（飞书机器人走的就是这条）===")
    payload = post_json(
        f"{api_url.rstrip('/')}/kb/answer",
        {"query": question, "max_tokens": max_tokens},
        api_key,
        timeout,
    )
    if "__error__" in payload:
        print(f"  [FAIL] {payload['__error__']} {payload.get('__body__', '')}")
        return {}
    print(f"  citations = {payload.get('citations')}")
    print("  ---- 答案全文 ----")
    print("  " + str(payload.get("answer") or "").replace("\n", "\n  "))
    print("  ---- 引用来源 ----")
    for ref in payload.get("references") or []:
        print(f"    [{ref.get('ref_id')}] {ref.get('source_uri')}")
    return payload


def section_verdict(search: dict[str, Any], answer: dict[str, Any]) -> None:
    """D. 把可机械判定的事实列出来（不下结论、不替人判断）。"""
    print("\n=== D. 可机械核对的事实 ===")
    evidence = search.get("evidence") or []
    refs = answer.get("references") or []

    same_count = len(evidence) == len(refs)
    print(f"  证据条数（search） = {len(evidence)}")
    print(f"  引用条数（answer） = {len(refs)}")
    print(f"  两者条数是否一致   = {same_count}")
    print("  ⚠️ 不一致是**重要线索**：胖端点用的是它自己那次检索的结果，与 search 各算各的")

    search_sources = [str(item.get("source_uri") or "") for item in evidence]
    answer_sources = [str(ref.get("source_uri") or "") for ref in refs]
    if answer_sources:
        missing = [s for s in answer_sources if s not in search_sources]
        print(f"  answer 引用里、search 证据里没有的来源数 = {len(missing)}")
        for src in missing[:5]:
            print(f"    - {src}")

    print("  同一来源出现多次（说明**相邻块合并**或**同文档多块**占了预算）:")
    counts: dict[str, int] = {}
    for src in answer_sources or search_sources:
        counts[src] = counts.get(src, 0) + 1
    for src, count in sorted(counts.items(), key=lambda kv: -kv[1])[:5]:
        if count > 1:
            print(f"    {count}×  {src}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行。"""
    parser = argparse.ArgumentParser(description="对比瘦核心与胖端点的证据/答案")
    parser.add_argument("--question", required=True, help="要核对的原始问题")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="本机 API 根地址")
    parser.add_argument("--api-key", default=None, help="本机 token；默认取 .env")
    parser.add_argument("--top-k", type=int, default=20, help="search 的 top_k")
    parser.add_argument("--max-tokens", type=int, default=3000, help="证据预算 / 生成预算")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="单次请求超时秒数")
    parser.add_argument("--full", action="store_true", help="打印完整证据正文（默认截断 320 字）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """跑完 B/C/D，返回失败段数（0 即两段都通）。"""
    args = parse_args(argv)
    print("Recall 答案一致性排查：瘦核心 vs 胖端点")
    print(f"问题：{args.question}")
    env = load_env()
    api_key = default_api_key(env, args.api_key)

    search = section_search(
        args.api_url, api_key, args.question, args.top_k, args.max_tokens, args.timeout, args.full
    )
    answer = section_answer(args.api_url, api_key, args.question, args.max_tokens, args.timeout)
    section_verdict(search, answer)

    failed = int("__error__" in search) + int("__error__" in answer)
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
