#!/usr/bin/env python
"""R-45 验收脚本：``GET /kb/stats``（只读，无副作用）。

对照 ``spec/roadmap.md`` 的 R-45 与 ``spec/tech.md`` §8 / §17 决策记录 14，
验收三条不变量：

A. 同源同形 —— REST 与 MCP 工具 ``kb_stats`` 逐字段相等；
B. 只读     —— 反复调用不改变任何状态（payload 稳定 + registry 库文件不动）；
C. 未破坏   —— 既有四个端点与 MCP 工具名不受影响。

用法::

    python tools/verify_r45.py

退出码 = 失败项数（0 即通过）。

.. note::
    本脚本刻意用 **Python** 而非 PowerShell 实现：Windows 客户端默认
    ``ExecutionPolicy = Restricted``，``.ps1`` 需要 ``-ExecutionPolicy Bypass``
    才能运行，而 Python 是项目既有运行时、无此限制。也刻意**不使用 ANSI 颜色** ——
    老版 cmd.exe 会把转义序列原样打印成乱码。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# 统一输出编码：Windows 下 Python 对**管道/重定向**的 stdout 会用本地代码页（cp936），
# 而本项目全链路是 UTF-8（源码、git、日志）⇒ 不统一就会"控制台正常、重定向乱码"。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EXPECTED_FIELDS = (
    "collection",
    "collections",
    "qdrant",
    "collection_ready",
    "points_count",
    "documents",
    "failed_documents",
    "embedding_model",
    "embedding_version",
    "chunker",
    "created_at",
)
LEGACY_PATHS = ("/health", "/kb/search", "/kb/answer", "/kb/ingest")
STATS_TEST = "tests/test_search.py::test_rest_stats_endpoint_matches_the_mcp_tool"
TIMEOUT_S = 30.0


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


def step(title: str) -> None:
    """打印一个步骤标题。"""
    print()
    print(title)


def fetch_json(url: str) -> Any:
    """GET 一个 JSON 端点。"""
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def post_status(url: str) -> int:
    """对端点发一个空 POST，返回 HTTP 状态码（不抛异常）。"""
    request = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def canonical(payload: Any) -> str:
    """把响应体压成可逐位比较的规范字符串。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def run_pytest(python: str) -> bool:
    """跑「REST 与 MCP 同源同形」那一项回归测试。"""
    completed = subprocess.run(
        [python, "-m", "pytest", STATS_TEST, "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    tail = [line for line in completed.stdout.splitlines() if line.strip()][-1:]
    for line in tail:
        print(f"           {line.strip()}")
    return completed.returncode == 0


def main() -> int:
    """跑完 7 步检查，返回失败项数。"""
    parser = argparse.ArgumentParser(description="R-45 验收：GET /kb/stats")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="recall.api 服务地址")
    parser.add_argument(
        "--registry-db",
        default=str(REPO_ROOT / "data" / "registry.db"),
        help="SQLite 注册表路径（只读性取证用）",
    )
    parser.add_argument("--skip-pytest", action="store_true", help="跳过第 7 步")
    args = parser.parse_args()

    base: str = args.base.rstrip("/")
    registry_db: str = args.registry_db
    report = Report()

    print()
    print("=" * 56)
    print(" R-45 验收：GET /kb/stats")
    print(f" 目标服务：{base}")
    print("=" * 56)

    # -- 步骤 1 --------------------------------------------------------------
    step("步骤 1/7  服务可达性（/health）")
    try:
        health = fetch_json(f"{base}/health")
        print(f"         /health -> {canonical(health)}")
    except Exception as exc:  # noqa: BLE001 - 验收脚本要把任何失败转成可读提示
        report.check("服务可达", False, str(exc))
        print()
        print("  服务没起来。先执行（另开一个终端，保持不关）：")
        print(f"      {sys.executable} -m recall.api")
        print("  等它打印 api.service_ready 后再跑本脚本。")
        return 1
    report.check("服务可达（/health 返回 200）", True)

    # -- 步骤 2 --------------------------------------------------------------
    step("步骤 2/7  GET /kb/stats 字段完整性")
    stats = fetch_json(f"{base}/kb/stats")
    print("         响应体：")
    for line in json.dumps(stats, indent=4, ensure_ascii=False).splitlines():
        print(f"           {line}")

    actual = set(stats) if isinstance(stats, dict) else set()
    missing = [name for name in EXPECTED_FIELDS if name not in actual]
    report.check(
        f"11 个契约字段齐全（缺失：{'、'.join(missing) if missing else '无'}）", not missing
    )
    report.check(
        f"qdrant=true 且 collection_ready=true"
        f"（实得 qdrant={stats.get('qdrant')} ready={stats.get('collection_ready')}）",
        bool(stats.get("qdrant")) and bool(stats.get("collection_ready")),
    )
    report.check(
        f"points_count > 0（实得 {stats.get('points_count')}）",
        stats.get("points_count", 0) > 0,
    )
    report.check(f"documents > 0（实得 {stats.get('documents')}）", stats.get("documents", 0) > 0)
    report.check(
        f"failed_documents = 0（实得 {stats.get('failed_documents')}）",
        stats.get("failed_documents") == 0,
    )
    report.check(
        f"chunker = md-heading-v1（实得 {stats.get('chunker')}）",
        stats.get("chunker") == "md-heading-v1",
    )

    # -- 步骤 3 --------------------------------------------------------------
    step("步骤 3/7  与 /health 交叉一致（两份口径来自不同代码路径）")
    print(
        f"         /health.documents    = {health.get('documents')}"
        f"   /kb/stats.documents    = {stats.get('documents')}"
    )
    print(
        f"         /health.points_count = {health.get('points_count')}"
        f"   /kb/stats.points_count = {stats.get('points_count')}"
    )
    report.check(
        "documents 两处相等",
        health.get("documents") == stats.get("documents"),
        f"health={health.get('documents')} stats={stats.get('documents')}",
    )
    report.check(
        "points_count 两处相等",
        health.get("points_count") == stats.get("points_count"),
        f"health={health.get('points_count')} stats={stats.get('points_count')}",
    )

    # -- 步骤 4 --------------------------------------------------------------
    step("步骤 4/7  只读性（判据 B）")
    db_path = Path(registry_db)
    before_ns = db_path.stat().st_mtime_ns if db_path.exists() else None
    reference = canonical(stats)
    for _ in range(5):
        fetch_json(f"{base}/kb/stats")
    repeat = canonical(fetch_json(f"{base}/kb/stats"))
    report.check("连调 6 次 payload 逐位相同", reference == repeat)
    if before_ns is None:
        print(f"         （未找到 {registry_db}，跳过 mtime 取证）")
    else:
        after_ns = db_path.stat().st_mtime_ns
        report.check("registry.db 未被写入（mtime 不变）", before_ns == after_ns)
        print(f"         registry.db mtime: 调用前={before_ns} 调用后={after_ns}")

    # -- 步骤 5 --------------------------------------------------------------
    step("步骤 5/7  方法约束：只接受 GET")
    status = post_status(f"{base}/kb/stats")
    print(f"         POST /kb/stats -> HTTP {status}")
    report.check("POST 被拒（期望 405）", status == 405, f"实得 {status}")

    # -- 步骤 6 --------------------------------------------------------------
    step("步骤 6/7  OpenAPI 已登记该路由")
    openapi = fetch_json(f"{base}/openapi.json")
    paths: dict[str, Any] = openapi.get("paths", {})
    for name in paths:
        print(f"           {name}")
    stats_methods = set(paths.get("/kb/stats", {}))
    report.check(
        "/kb/stats 已登记且仅 get",
        "/kb/stats" in paths and "get" in stats_methods and "post" not in stats_methods,
        f"实得 {sorted(stats_methods)}",
    )
    lost = [name for name in LEGACY_PATHS if name not in paths]
    report.check(f"既有四个端点仍在（缺失：{'、'.join(lost) if lost else '无'}）", not lost)

    # -- 步骤 7 --------------------------------------------------------------
    step("步骤 7/7  同源同形回归测试（判据 A：REST == MCP structuredContent）")
    if args.skip_pytest:
        print("         （已用 --skip-pytest 跳过）")
    else:
        print("         执行 pytest（约需 10~30 秒）...")
        report.check("REST 与 MCP 逐字段相等（pytest 通过）", run_pytest(sys.executable))

    # -- 汇总 ----------------------------------------------------------------
    print()
    print("=" * 56)
    if report.failed == 0:
        print(f" 验收结论：通过   （{report.passed} 项检查全绿）")
    else:
        print(f" 验收结论：不通过 （通过 {report.passed} 项，失败 {report.failed} 项）")
    print("=" * 56)
    print()
    return report.failed


if __name__ == "__main__":
    sys.exit(main())
