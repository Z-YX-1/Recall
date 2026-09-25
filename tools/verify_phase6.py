"""Phase 6 验收自动化（roadmap R-40 / R-38 / R-42）。

把**机械可验证**的部分一次跑完，只把"必须在真实 DSH 会话里做"的那一步留给人。

能自动验的：

- **R-40 权限 S2**：``/health`` 免鉴权；``/kb/stats`` 无 key 401、有 key 200；
  **配置与运行态是否一致**（防"改了配置没重启"这种最坑的假绿）；
  审计文件留痕且**不含密钥**。
- **R-42 证据门槛**：当前取值；开启时"笔记内命中 / 笔记外返回空证据"。
- **R-38 watchdog**：watcher 日志新鲜度；``--probe-vault`` 时做**完整**端到端
  （改笔记 → 数字变化 → 再触发幂等 → 删笔记 → 数字复原）。

仍需人工：**用 DSH 问一句笔记里的内容**（唯一不可替代的判据）；
启用鉴权时还要在 ``mcp-servers.json`` 加 ``headers`` 并重开 DSH 会话。

⚠️ 默认**不碰 vault**：会写文件的 R-38 探测必须显式加 ``--probe-vault``，
且探针文件会在 ``finally`` 里删除。

用法::

    python tools/verify_phase6.py                      # 非侵入检查
    python tools/verify_phase6.py --api-key <token>    # 服务已启用鉴权
    python tools/verify_phase6.py --probe-vault        # 额外做 R-38 端到端探测

退出码 = 失败项数（0 即通过）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # 直接运行脚本时保证能 import recall
    sys.path.insert(0, str(REPO_ROOT))

# 统一输出编码：Windows 下 Python 对管道/重定向的 stdout 用本地代码页，
# 而本项目全链路 UTF-8 ⇒ 不统一会"控制台正常、重定向乱码"（同 verify_r45.py）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TIMEOUT_S = 60.0
"""单次 HTTP 请求超时。"""

PROBE_TIMEOUT_S = 90.0
"""R-38 探测里等待 watcher 触发 + 摄取完成的上限。"""

PROBE_POLL_S = 2.0
"""探测轮询间隔。"""


class Report:
    """收集检查结果并即时打印。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

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
        """打印一条不判定成败的观察。"""
        print(f"  [INFO] {text}")

    def skip(self, name: str, reason: str) -> None:
        """打印一条跳过（需人工或前置不满足）。"""
        self.skipped += 1
        print(f"  [SKIP] {name}  （{reason}）")


def step(title: str) -> None:
    """打印一个步骤标题。"""
    print()
    print(title)


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """发一个请求，返回 ``(status, json_or_text)``（不抛异常）。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    all_headers = dict(headers)
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as response:
            body = response.read().decode("utf-8")
            return int(response.status), _maybe_json(body)
    except urllib.error.HTTPError as exc:
        return int(exc.code), _maybe_json(exc.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - 验收脚本要把任何失败转成可读提示
        return 0, f"{type(exc).__name__}: {exc}"


def _maybe_json(text: str) -> Any:
    """尽力把响应体解析成 JSON，失败就原样返回。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _settings() -> Any:
    """读本机配置（与服务的 ``.env`` 同源）。"""
    from recall.config import Settings

    return Settings.from_env()


# --------------------------------------------------------------------------------------
# R-40：鉴权与审计
# --------------------------------------------------------------------------------------


def check_auth(report: Report, base: str, api_key: str, settings: Any) -> dict[str, str]:
    """检查鉴权：公共路径、无 key/有 key 两条路，以及**配置与运行态是否一致**。

    Returns:
        后续请求该带的头。
    """
    step("R-40 一、鉴权（REST 与 /mcp 共用同一份 key 表）")
    headers = {"X-API-Key": api_key} if api_key else {}

    health_status, _ = request("GET", f"{base}/health", headers={})
    report.check("GET /health 免鉴权可访问（探活）", health_status == 200, f"实得 {health_status}")

    anon_status, anon_body = request("GET", f"{base}/kb/stats", headers={})
    if settings.auth_enabled:
        report.check(
            "GET /kb/stats 无 key ⇒ 401（统一错误信封）",
            anon_status == 401,
            f"实得 {anon_status}",
        )
        if isinstance(anon_body, dict):
            report.check(
                "401 走统一错误信封（code=unauthorized）",
                anon_body.get("error", {}).get("code") == "unauthorized",
                f"实得 {anon_body}",
            )

        if api_key:
            key_status, _ = request("GET", f"{base}/kb/stats", headers=headers)
            report.check("GET /kb/stats 带 key ⇒ 200", key_status == 200, f"实得 {key_status}")
        else:
            report.skip("带 key 的放行路径", "未提供 --api-key")
            report.info("服务已启用鉴权 ⇒ 请加 --api-key <token> 复跑，以验证放行路径")
    else:
        report.check(
            "未配置 key 表 ⇒ 不做鉴权（S1 语义，升级不打断本机使用）",
            anon_status == 200,
            f"实得 {anon_status}",
        )
        if anon_status == 401:
            report.info(
                "⚠️ 配置里没有 key 表，服务却在要 key ⇒ 服务进程可能是**改了配置之前**启动的，请重启"
            )
    return headers


def check_audit(report: Report, settings: Any, api_key: str) -> None:
    """检查审计文件：存在、逐行合法 JSON、**不含任何密钥**、含被拒记录。"""
    step("R-40 二、审计日志 data/logs/audit.jsonl")
    path: Path = settings.audit_log_path
    if not settings.log_to_file:
        report.skip("审计文件检查", "RECALL_LOG_TO_FILE=0（本进程不写文件）")
        return
    if not path.exists():
        report.check(
            "审计文件存在",
            False,
            f"未找到 {path} ⇒ 服务进程可能是 **R-40 之前**启动的（那时还没有中间件），"
            "重启 python -m recall.api 后再跑",
        )
        return

    raw = path.read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    report.check(f"审计文件存在且有记录（{len(lines)} 行）", bool(lines), f"{path}")

    records: list[dict[str, Any]] = []
    bad = 0
    for line in lines[-200:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    report.check("最近 200 行都是合法 JSON", bad == 0, f"{bad} 行解析失败")

    secrets = list(settings.api_keys) + ([api_key] if api_key else [])
    leaked = [secret for secret in secrets if secret and secret in raw]
    report.check("审计文件**不含任何密钥**", not leaked, f"发现 {len(leaked)} 个密钥字样")

    if settings.auth_enabled:
        outcomes = {record.get("outcome") for record in records}
        report.check(
            "既有放行也有拒绝的留痕（ok / unauthorized）",
            {"ok", "unauthorized"} <= outcomes,
            f"实得 {sorted(item for item in outcomes if item)}",
        )
    for record in records[-3:]:
        report.info(
            f"最近一条: {record.get('ts')} user={record.get('user')} "
            f"{record.get('method')} {record.get('path')} -> {record.get('status')} "
            f"({record.get('outcome')})"
        )


# --------------------------------------------------------------------------------------
# R-42：证据门槛
# --------------------------------------------------------------------------------------


def _questions() -> tuple[str, str]:
    """取一道笔记内题与一道笔记外题（远域，最该被拒）。"""
    golden_path = REPO_ROOT / "eval" / "golden_set.jsonl"
    outside_path = REPO_ROOT / "eval" / "out_of_vault.jsonl"
    golden = [
        json.loads(line)
        for line in golden_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    outside = [
        json.loads(line)
        for line in outside_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    far = next(item for item in outside if item["group"] == "far")
    return str(golden[0]["question"]), str(far["question"])


def check_gate(report: Report, base: str, headers: dict[str, str], settings: Any) -> None:
    """检查证据门槛的当前取值与实际行为。"""
    step("R-42 证据门槛（RECALL_EVIDENCE_MIN_SCORE）")
    threshold = settings.evidence_min_score
    report.info(f"当前配置值 = {threshold}（0.0 = 不启用）")

    in_vault, out_of_vault = _questions()
    in_status, in_body = request(
        "POST", f"{base}/kb/search", headers=headers, payload={"query": in_vault, "top_k": 3}
    )
    out_status, out_body = request(
        "POST", f"{base}/kb/search", headers=headers, payload={"query": out_of_vault, "top_k": 3}
    )
    if in_status != 200 or out_status != 200:
        report.check("两次检索均可调用", False, f"笔记内 {in_status} / 笔记外 {out_status}")
        return

    in_evidence = in_body.get("evidence", []) if isinstance(in_body, dict) else []
    out_evidence = out_body.get("evidence", []) if isinstance(out_body, dict) else []
    report.check(
        f"笔记内问题有证据（{len(in_evidence)} 条）",
        bool(in_evidence),
        f"query={in_vault!r}",
    )

    if threshold > 0.0:
        report.check(
            "门槛已启用 ⇒ 笔记外问题返回**空证据**",
            not out_evidence,
            f"实得 {len(out_evidence)} 条，最高分 "
            f"{out_evidence[0]['score']:.3f}" if out_evidence else "",
        )
    else:
        report.info(
            f"门槛未启用（默认）⇒ 笔记外问题仍返回 {len(out_evidence)} 条低相关片段。"
            "若要用门槛，在 .env 写 RECALL_EVIDENCE_MIN_SCORE=0.58 并重启"
            "（见 eval/BASELINE.md §7.5）"
        )


# --------------------------------------------------------------------------------------
# R-38：watchdog
# --------------------------------------------------------------------------------------


def check_watchdog_state(report: Report, settings: Any) -> None:
    """看 watcher 的日志新鲜度，判断它是否在跑（非侵入）。"""
    step("R-38 一、watcher 状态")
    log = settings.log_dir / "watchdog.log"
    if not log.exists():
        report.check(
            "watcher 日志存在",
            False,
            f"未找到 {log} ⇒ watcher **似乎没在跑**（另开终端执行 python -m recall.watchdog）",
        )
        return
    age = time.time() - log.stat().st_mtime
    fresh = age < 3600
    report.check(
        f"watcher 日志在最近 1 小时内更新过（{age / 60:.1f} 分钟前）",
        fresh,
        "日志很久没动 ⇒ watcher 可能已退出（启动时会写 watchdog.started）",
    )
    lines = [
        line
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    for line in lines[-5:]:
        report.info(f"watchdog.log: {line[:150]}")


def probe_vault(report: Report, base: str, headers: dict[str, str], settings: Any) -> None:
    """R-38 端到端探测：改笔记 → 数字变化 → 幂等 → 删笔记 → 复原。

    ⚠️ 会**真实写入并删除** vault 里一个临时文件（``finally`` 保证删除）。
    """
    step("R-38 二、端到端探测（--probe-vault；会临时写一个探针文件并删除）")
    vault: Path | None = settings.vault_path
    if vault is None or not vault.is_dir():
        report.check("vault 可用", False, f"RECALL_VAULT_PATH={vault}")
        return

    before = _stats_documents(base, headers)
    if before is None:
        report.check("读取 /kb/stats 基线", False, "拿不到统计")
        return
    report.info(f"基线：documents={before[0]} points={before[1]}")

    probe = vault / f"_recall_验收探针_{uuid.uuid4().hex[:8]}.md"
    probe.write_text(
        "# Recall 验收探针\n\n这是一个临时文件，用于验证 watchdog 会把变更同步进知识库。\n",
        encoding="utf-8",
    )
    report.info(f"已写入探针 {probe.name}，等待 watcher 触发…")
    try:
        grown = _wait_for_documents(base, headers, before[0] + 1)
        report.check(
            f"watcher 触发后 documents 增长（{before[0]} → {grown[0] if grown else '未变化'}）",
            grown is not None,
            f"等待 {PROBE_TIMEOUT_S:.0f}s 未见变化 ⇒ watcher 没在跑，或去抖后未触发",
        )
        if grown is None:
            return

        # 幂等：再直接触发一次，点数不应变化
        status, _ = request(
            "POST", f"{base}/kb/ingest", headers=headers, payload={"mode": "update"}
        )
        report.check("手动再触发一次摄取（幂等检查）", status == 200, f"实得 HTTP {status}")
        again = _stats_documents(base, headers)
        report.check(
            "重复触发后 points 不变（幂等三机制）",
            again is not None and again[1] == grown[1],
            f"{grown[1]} → {again[1] if again else '?'}",
        )
    finally:
        probe.unlink(missing_ok=True)
        report.info(f"已删除探针 {probe.name}")

    restored = _wait_for_documents(base, headers, before[0])
    report.check(
        f"删除探针后 documents 复原（→ {before[0]}）",
        restored is not None,
        "等待超时：孤儿清理可能还没跑完",
    )


def _stats_documents(base: str, headers: dict[str, str]) -> tuple[int, int] | None:
    """取 ``(documents, points_count)``；失败返回 ``None``。"""
    status, body = request("GET", f"{base}/kb/stats", headers=headers)
    if status != 200 or not isinstance(body, dict):
        return None
    return int(body.get("documents", 0)), int(body.get("points_count", 0))


def _wait_for_documents(
    base: str, headers: dict[str, str], target: int
) -> tuple[int, int] | None:
    """等到 ``documents == target``（或超时）。"""
    deadline = time.monotonic() + PROBE_TIMEOUT_S
    while time.monotonic() < deadline:
        current = _stats_documents(base, headers)
        if current is not None and current[0] == target:
            return current
        time.sleep(PROBE_POLL_S)
    return None


# --------------------------------------------------------------------------------------
# R-39：工具白名单 / 文档权限 / 网关与审计来源（全部非侵入）
# --------------------------------------------------------------------------------------


def _sse_json(text: str) -> Any:
    """从 Streamable HTTP 的 SSE 响应里取出第一个 JSON 对象。

    MCP 端点默认回 ``text/event-stream``：正文形如 ``event: message\\ndata: {...}``。
    若不是 SSE（例如已是纯 JSON）则直接解析。
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        return _maybe_json(stripped)
    for line in stripped.splitlines():
        if line.startswith("data:"):
            return _maybe_json(line[len("data:") :].strip())
    return None


def mcp_tools(base: str, headers: dict[str, str]) -> tuple[int, list[str]]:
    """调 MCP 的 ``tools/list``，返回 ``(status, 工具名列表)``。

    无会话模式下每个请求自带一次握手，因此可以直接发 ``tools/list``。
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    accept = {"Accept": "application/json, text/event-stream", **headers}
    status, body = request("POST", f"{base}/mcp/", headers=accept, payload=payload)
    if status != 200:
        return status, []
    parsed = _sse_json(body) if isinstance(body, str) else body
    if not isinstance(parsed, dict):
        return status, []
    tools = parsed.get("result", {}).get("tools", [])
    return status, [str(item.get("name", "")) for item in tools if isinstance(item, dict)]


def check_tool_policy(report: Report, base: str, settings: Any, api_key: str) -> None:
    """按身份验证 MCP 工具可见性（roadmap R-39 前置件）。"""
    step("R-39 一、MCP 工具可见性（RECALL_MCP_TOOL_POLICY）")
    # 延迟导入：`recall.api` 会连带拉起 FastEmbedding 等重依赖，只在真正需要时付这个代价。
    from recall.api import MCP_TOOL_NAMES

    if not settings.mcp_tool_policy:
        report.info("未配置工具白名单 ⇒ 所有身份都能看到全部工具（默认行为，未收窄）")
    else:
        for user, allowed in sorted(settings.mcp_tool_policy.items()):
            report.info(f"策略：{user} ⇒ {sorted(allowed)}")

    keys = list(settings.api_keys.items())
    # 没配 key 表（S1）时也要验一次：用匿名身份 —— 此时默认身份是 me，未列白名单 ⇒ 应看到全部工具。
    candidates: list[tuple[str, str]] = keys or [(api_key, "(匿名)")]
    if not keys and not api_key:
        report.info("未配置 RECALL_API_KEYS ⇒ 以匿名身份检查（默认身份 me）")

    for token, user in candidates:
        headers = {"X-API-Key": token} if token else {}
        status, tools = mcp_tools(base, headers)
        if status != 200:
            report.check(
                f"身份 {user}：tools/list 可调用",
                False,
                f"HTTP {status}（若服务是新配的鉴权，注意重启；见上面的运行态一致性检查）",
            )
            continue
        expected = settings.mcp_tool_policy.get(user)
        if expected is None:
            report.check(
                f"身份 {user}：未列白名单 ⇒ 应看到全部工具（{len(tools)} 个）",
                set(tools) == set(MCP_TOOL_NAMES),
                f"实得 {sorted(tools)}",
            )
        else:
            report.check(
                f"身份 {user}：只应看到白名单工具 {sorted(expected)}",
                set(tools) == set(expected),
                f"实得 {sorted(tools)}",
            )


def check_document_permissions(report: Report, settings: Any) -> None:
    """看真实语料的权限分布（只读注册表，不碰 vault）。"""
    step("R-39 二、文档权限分布（frontmatter → owner/visibility）")
    import sqlite3

    db = Path(settings.registry_db)
    if not db.exists():
        report.check("注册表可读", False, f"未找到 {db}")
        return
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT owner, visibility, COUNT(*) FROM documents GROUP BY owner, visibility"
            ).fetchall()
    except sqlite3.Error as exc:
        report.check("注册表可读", False, f"{type(exc).__name__}: {exc}")
        return

    total = sum(int(row[2]) for row in rows)
    report.check(f"注册表可读（{total} 篇）", True)
    for owner, visibility, count in rows:
        report.info(f"{owner} / {visibility}：{count} 篇")
    public = sum(int(row[2]) for row in rows if str(row[1]) == "public")
    if public == 0:
        report.info(
            "没有任何 public 文档 ⇒ 外部身份（映射成非 me 的 token）会**检索不到任何东西**。"
            "若要给外部接入看内容，在笔记 frontmatter 写 visibility: public（见 tech.md §3.4）"
        )
    else:
        report.info(f"有 {public} 篇 public 文档 ⇒ 非 me 身份可见这些")


def check_gateway_and_audit_source(report: Report, base: str, settings: Any, api_key: str) -> None:
    """检查网关鉴权模式与审计来源可追溯（roadmap R-39 待办 B/C）。"""
    step("R-39 三、网关鉴权模式与审计来源")
    report.info(f"mcp_auth_mode = {settings.mcp_auth_mode}（app = 本进程校验 key）")
    report.info(f"trusted_proxies = {list(settings.trusted_proxies)}")
    if settings.mcp_auth_mode == "gateway":
        report.info(f"网关身份 = {settings.mcp_gateway_user}")
        if not settings.mcp_tool_policy.get(settings.mcp_gateway_user):
            report.check(
                "网关身份必须配工具白名单（否则拥有全部工具，含写端点）",
                False,
                f"RECALL_MCP_TOOL_POLICY 里没有 {settings.mcp_gateway_user} 的规则",
            )
        else:
            report.check("网关身份已配工具白名单", True)

        # 网关模式下 /mcp 不该被本进程要求 key
        status, body = request(
            "POST",
            f"{base}/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        report.check(
            "网关模式下 /mcp 不要求 key（不该是 401）",
            status != 401,
            f"HTTP {status}",
        )
        del body

    path: Path = settings.audit_log_path
    if not path.exists():
        report.skip("审计来源分布", "审计文件还不存在（重启后再看）")
        return
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    sources: dict[str, int] = {}
    for record in records:
        key = str(record.get("client_source", "(旧记录无此字段)"))
        sources[key] = sources.get(key, 0) + 1
    report.info(f"最近 {len(records)} 条审计的 client_source 分布：{sources or '（无记录）'}")
    direct = {"peer", "(旧记录无此字段)"}
    forwarded = sum(count for key, count in sources.items() if key not in direct)
    if forwarded:
        report.check("审计记录了转发来源（隧道/反代可达时有效）", True)
        sample = next(
            (item for item in reversed(records) if item.get("client_source") not in (None, "peer")),
            None,
        )
        if sample:
            report.info(
                f"示例：client={sample.get('client')} source={sample.get('client_source')} "
                f"peer={sample.get('peer')}"
            )
    else:
        report.info(
            "全部为 peer ⇒ 目前都是直连；走隧道/反代后这里应出现 "
            "cf-connecting-ip 或 x-forwarded-for"
        )


# --------------------------------------------------------------------------------------


def main() -> int:
    """跑完全部检查，返回失败项数。"""
    parser = argparse.ArgumentParser(description="Phase 6 验收入口（R-40 / R-38 / R-42）")
    parser.add_argument("--base", default="", help="API 根地址；默认按配置的 host/port 拼")
    parser.add_argument("--api-key", default="", help="服务启用鉴权时的 key")
    parser.add_argument(
        "--probe-vault",
        action="store_true",
        help="额外做 R-38 端到端探测（**会临时写入并删除 vault 里的一个探针文件**）",
    )
    args = parser.parse_args()

    settings = _settings()
    base: str = args.base.rstrip("/") or f"http://{settings.host}:{settings.port}"
    report = Report()

    print()
    print("=" * 60)
    print(" Phase 6 验收入口（R-40 权限 / R-42 门槛 / R-38 watchdog / R-39 接入前置）")
    print(f" 目标服务：{base}")
    print("=" * 60)

    step("R-0 服务可达性")
    status, _ = request("GET", f"{base}/health", headers={})
    if status == 0:
        report.check("服务可达", False, "连不上")
        print()
        print(f"  服务没起来。先执行：{sys.executable} -m recall.api")
        return 1
    report.check("服务可达（/health 200）", status == 200, f"实得 {status}")

    headers = check_auth(report, base, args.api_key, settings)
    check_audit(report, settings, args.api_key)
    check_gate(report, base, headers, settings)
    check_tool_policy(report, base, settings, args.api_key)
    check_document_permissions(report, settings)
    check_gateway_and_audit_source(report, base, settings, args.api_key)
    check_watchdog_state(report, settings)
    if args.probe_vault:
        probe_vault(report, base, headers, settings)
    else:
        report.skip("R-38 端到端探测", "需显式加 --probe-vault（它会临时写一个探针文件）")

    step("仍需人工完成的一步")
    if settings.auth_enabled:
        report.info(
            '在 $DSH_HOME/mcp-servers.json 的 recall 条目加 "headers": {"X-API-Key": "<token>"}，'
            "然后**重开 DSH 会话**问一句笔记里的内容"
        )
    report.info("用 DSH 问一句「刚改过的那篇笔记」里的内容，确认能命中（这是唯一不可替代的判据）")

    print()
    print("=" * 60)
    if report.failed == 0:
        print(f" 结论：通过   （{report.passed} 项通过 / {report.skipped} 项跳过）")
    else:
        print(
            f" 结论：不通过 （通过 {report.passed} / 失败 {report.failed} /"
            f" 跳过 {report.skipped}）"
        )
    print("=" * 60)
    print()
    return report.failed


if __name__ == "__main__":
    raise SystemExit(main())
