#!/usr/bin/env python
"""R-49 验收脚本：飞书**交互入口**（roadmap R-49e）。

对照 ``spec/roadmap.md`` §四 R-49 与 ``spec/tech.md`` §18 决策 19 逐项核验。
设计原则与 :mod:`tools.verify_r45` 一致：**只读、无副作用、退出码 = 失败项数**。

它把 R-49 能自动验的部分全部跑掉：

===  ================================================================
A    **配置**：`.env` 里飞书凭证齐不齐（只报长度与前缀，**绝不打印 secret**）
B    **依赖**：`lark-oapi` 在、`websockets` 落在 `[15.0.1, 16)` 这个唯一可行窗口
C    **SDK 表面**：我们用到的方法名 / 关键字参数在真实 SDK 上存在
D    **卡片渲染**：`lark_md` 转义真的挡住了 `@所有人` 等语法注入，且引用一条不丢
E    **本机链路**：`POST /kb/answer` 可达（机器人真正依赖的是它）
F    **长连接**：端点发现 ``code=0`` 且拿到 WSS 地址；可选真握手一次
G    **人工步骤**：飞书侧 5 项配置 + 真发一句 —— **脚本只打印，不判 FAIL**
===  ================================================================

用法::

    python tools/verify_r49.py                 # 全跑（含一次真握手）
    python tools/verify_r49.py --skip-wss      # 只做端点发现，不建 WSS
    python tools/verify_r49.py --skip-network  # 只做本地检查（A~E）

.. note::
    与 :mod:`tools.verify_phase6` 一样刻意**不打印任何密钥**：飞书 App Secret 只报长度，
    本机 token 只报前 4 位。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata as metadata
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # 允许 `python tools/verify_r49.py` 直接跑
    sys.path.insert(0, str(REPO_ROOT))

# 统一输出编码（Windows 下管道/重定向会落到 cp936，而全项目是 UTF-8）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FEISHU_BASE: str = "https://open.feishu.cn"
WS_ENDPOINT_PATH: str = "/callback/ws/endpoint"
DEFAULT_API_URL: str = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_S: float = 90.0
"""单次请求超时。

⚠️ **不能设小**：``/kb/answer`` 在**冷启动**时要先装载 bge-m3 + bge-reranker
（实测 16~25 秒）再做检索（约 5s）再调 DeepSeek（约 5s）⇒ 首次调用 35s+ 是正常的。
早期版本取 30s，第一次跑就误报成 FAIL（第二次跑模型已热才通过）。"""

WEBSOCKETS_MIN: tuple[int, int] = (15, 0)
WEBSOCKETS_MAX_EXCLUSIVE: tuple[int, ...] = (16,)
"""``websockets`` 的唯一可行窗口（见 ``tech.md`` §18 决策 19）：

``lark-oapi`` 要 ``<16``，而 **fastmcp 要 ``>=15.0.1``**（另有 mcp / langgraph-sdk /
langsmith / uvicorn 四方约束）⇒ ``[15.0.1, 16)`` 是六方约束的唯一交集。
"""

MANUAL_STEPS: tuple[str, ...] = (
    "① 开发者后台 → 权限管理：勾 `im:message` + `im:message:send_as_bot`（应用身份）",
    "② 开发者后台 → 添加应用能力：开通「机器人」",
    "③ 开发者后台 → 事件与回调：订阅方式选【长连接】，订阅 `im.message.receive_v1`",
    "④ 开发者后台 → 应用发布/可用范围：**把你本人加进可用范围**（否则私聊搜不到机器人）",
    "⑤ 版本管理与发布：创建版本并发布（企业自建应用可能需管理员审核）",
    "⑥ 起第 5 个窗口 `python -m recall.feishu_bot`，然后在飞书里私聊它问一句**笔记里有的**问题",
    "   ⇒ 期望收到**带 [n] 引用的卡片**；日志 `data\\logs\\feishu_bot.log` 应出现"
    " `event_accepted` → `replied`",
)


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
    print(f"\n=== {title} ===")


def _parse_version(raw: str) -> tuple[int, ...]:
    """把 ``"16.1.1"`` 解析成 ``(16, 1, 1)``（非数字段直接丢弃）。"""
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _load_settings() -> Any:
    """读项目配置（会顺带读 ``.env``）。"""
    from recall.config import Settings

    return Settings.from_env()


def check_config(report: Report, settings: Any) -> None:
    """A. 飞书凭证是否齐备（**只报长度与前缀**）。"""
    step("A. 飞书凭证（.env）")
    app_id = (settings.feishu_app_id or "").strip()
    secret = (settings.feishu_app_secret or "").strip()
    report.check(
        "FEISHU_APP_ID 已配置",
        bool(app_id),
        "缺：请在 .env 写入 FEISHU_APP_ID（开发者后台「凭证与基础信息」）",
    )
    if app_id:
        print(f"         app_id 前 12 位 = {app_id[:12]}…（值本身不敏感，但凭据文件不入库）")
    report.check(
        "FEISHU_APP_SECRET 已配置",
        len(secret) >= 16,
        f"缺或过短（当前长度 {len(secret)}）；**值绝不打印**",
    )
    report.check(
        "feishu_enabled（两者齐全才启用，fail-closed）",
        bool(settings.feishu_enabled),
        "未齐备时 `python -m recall.feishu_bot` 会 fail-closed 退出（码 2）—— 这是刻意的",
    )


def check_dependencies(report: Report) -> None:
    """B. 依赖版本窗口。"""
    step("B. 依赖（lark-oapi / websockets）")
    for name in ("lark-oapi", "websockets"):
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            report.check(f"{name} 已安装", False, '跑 `pip install -e ".[dev,eval]"`')
            continue
        report.check(f"{name} 已安装", True)
        print(f"         {name} = {version}")

    try:
        raw = metadata.version("websockets")
    except metadata.PackageNotFoundError:
        return
    parsed = _parse_version(raw)
    in_window = parsed[:2] >= WEBSOCKETS_MIN and parsed[:1] < WEBSOCKETS_MAX_EXCLUSIVE
    report.check(
        f"websockets 落在唯一可行窗口 [{WEBSOCKETS_MIN[0]}.{WEBSOCKETS_MIN[1]}, 16)",
        in_window,
        f"当前 {raw}：>=16 会与 lark-oapi 冲突；<15.0.1 会与 fastmcp 冲突",
    )


def check_sdk_surface(report: Report) -> None:
    """C. 真实 SDK 上的方法名与关键字参数。"""
    step("C. SDK 表面（方法名 / 关键字参数）")
    try:
        import inspect

        import lark_oapi as lark
    except Exception as exc:  # noqa: BLE001 - 探针要报告任何失败
        report.check("import lark_oapi", False, f"{type(exc).__name__}: {exc}")
        return
    report.check("import lark_oapi", True)
    print("         ⚠️ 冷启动约 8.28s（SDK 急切导入全部生成的 API）—— 机器人启动慢是正常的")

    try:
        builder = lark.EventDispatcherHandler.builder("", "")
        report.check(
            "EventDispatcherHandler.builder('', '') 可用（长连接无需 encrypt_key / token）",
            hasattr(builder, "register_p2_im_message_receive_v1"),
            "SDK 改了事件注册方法名？请核对 R-49c 的 build_ws_client",
        )
    except Exception as exc:  # noqa: BLE001
        report.check("EventDispatcherHandler.builder", False, f"{type(exc).__name__}: {exc}")

    params = inspect.signature(lark.ws.Client.__init__).parameters
    for kw in ("event_handler", "log_level", "auto_reconnect"):
        report.check(f"ws.Client 接受 {kw}=", kw in params)


def check_card_rendering(report: Report) -> None:
    """D. 卡片渲染：转义真的挡住了语法注入，且引用一条不丢。"""
    step("D. 卡片渲染（lark_md 转义）")
    from recall.lark_md import MAX_CARD_BODY_CHARS, render_card_body

    nasty = "# 标题\n- 列表\n---\n<at id=all></at> [1] *斜* **粗** `码`"
    references = [{"ref_id": "1", "source_uri": "notes/a.md"}]
    body = render_card_body(nasty, references)

    report.check("@所有人语法被中和（<at …> 不得原样出现）", "<at" not in body)
    report.check(
        "行首块级标记被中和（不出现行首 # / - / ---）",
        not any(line.lstrip().startswith(("#", "-")) for line in body.split("\n")),
    )
    report.check("引用编号已转义（&#91;n&#93;）", "&#91;1&#93;" in body)
    report.check("引用来源完整保留", body.count("notes/a.md") == 1)
    report.check("加粗被保留", "**粗**" in body)

    huge = render_card_body("甲" * 10_000, [])
    report.check(
        f"超长正文被截断到 ≤ {MAX_CARD_BODY_CHARS} 字符",
        len(huge) <= MAX_CARD_BODY_CHARS,
        f"实际 {len(huge)}",
    )


def check_local_api(report: Report, api_url: str, api_key: str | None, timeout: float) -> None:
    """E. 本机 ``POST /kb/answer`` 可达 —— 机器人真正依赖的是它。"""
    step("E. 本机 API 链路（POST /kb/answer）")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
        print(f"         使用 X-API-Key = {api_key[:4]}…（只显示前 4 位）")
    payload = json.dumps({"query": "混合检索 RRF 精排"}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - 本机固定 http 回环地址
        f"{api_url.rstrip('/')}/kb/answer", data=payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        report.check("POST /kb/answer 返回 200", False, f"HTTP {exc.code}（401 ⇒ key 不对）")
        return
    except Exception as exc:  # noqa: BLE001
        report.check("POST /kb/answer 可达", False, f"{type(exc).__name__}: {exc}")
        return

    report.check("POST /kb/answer 返回 200", True)
    report.check("响应含 answer 字段", bool(body.get("answer")))
    report.check(
        "响应含 references 字段（可核对引用的落点）", isinstance(body.get("references"), list)
    )
    excerpt = str(body.get("answer", ""))[:60].replace("\n", " ")
    print(f"         回答节选：{excerpt}…")

    # 检索链路本身也要通 —— 机器人回答的质量完全依赖它。
    # ⚠️ 查询词刻意用 **ASCII**（`bge-m3`）：PowerShell 5.1 的 `Invoke-RestMethod -Body <字符串>`
    # 会把中文按 ANSI 编码，查询词被弄坏后返回 0 条证据（**实测踩过**，不是产品问题）。
    # 本脚本走 `json.dumps(...).encode("utf-8")`，天然没有这个坑；用 ASCII 只是为了让
    # "手工 curl 复核"时也能得到一致结果。
    search_payload = json.dumps({"query": "bge-m3", "top_k": 3}).encode("utf-8")
    search_request = urllib.request.Request(  # noqa: S310 - 本机固定 http 回环地址
        f"{api_url.rstrip('/')}/kb/search", data=search_payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(search_request, timeout=timeout) as response:  # noqa: S310
            found = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        report.check("POST /kb/search 可达", False, f"{type(exc).__name__}: {exc}")
        return
    count = len(found.get("evidence") or [])
    report.check(
        "POST /kb/search 能召回证据（机器人回答的前提）",
        count > 0,
        "0 条：要么证据门槛（RECALL_EVIDENCE_MIN_SCORE）把 top1 拦了，要么查询词编码被弄坏",
    )
    if count:
        print(f"         召回 {count} 条，top1 = {found['evidence'][0].get('score')!r}")


def discover_ws_url(app_id: str, app_secret: str, timeout: float) -> tuple[str, str]:
    """F1. 端点发现：返回 ``(ws_url, note)``；失败时 ``ws_url`` 为空串。"""
    payload = json.dumps({"AppID": app_id, "AppSecret": app_secret}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - 固定官方 https 域名
        f"{FEISHU_BASE}{WS_ENDPOINT_PATH}",
        data=payload,
        headers={"Content-Type": "application/json", "locale": "zh"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    if body.get("code") != 0:
        return "", f"code={body.get('code')} msg={body.get('msg')!r}"
    # ⚠️ 实测结构是 `data.URL`（扁平）；SDK 文档写的是 `data.endpoint.URL` —— 以实测为准
    data = body.get("data") or {}
    url = str(data.get("URL") or "")
    config = data.get("ClientConfig") or {}
    note = (
        f"保活参数（服务端下发）：PingInterval={config.get('PingInterval')} "
        f"ReconnectInterval={config.get('ReconnectInterval')} "
        f"ReconnectCount={config.get('ReconnectCount')}（-1 = 无限重连）"
    )
    return url, note


async def _handshake(url: str) -> None:
    """真握手一次并立即关闭（不订阅、不处理事件）。"""
    from websockets.asyncio.client import connect

    async with connect(url, open_timeout=20, close_timeout=5):
        return


def check_long_connection(
    report: Report,
    app_id: str,
    app_secret: str,
    timeout: float,
    *,
    do_wss: bool,
) -> None:
    """F. 长连接：端点发现（+ 可选真握手）。"""
    step("F. 长连接（端点发现 / WSS 握手）")
    if not app_id or not app_secret:
        report.check("端点发现", False, "凭证不全 ⇒ 见 A 节")
        return
    try:
        url, note = discover_ws_url(app_id, app_secret, timeout)
    except Exception as exc:  # noqa: BLE001
        report.check("POST /callback/ws/endpoint", False, f"{type(exc).__name__}: {exc}")
        return

    if not url:
        report.check("端点发现返回 code=0 且带 WSS 地址", False, note)
        return
    report.check("端点发现返回 code=0 且带 WSS 地址", True)
    print(f"         wss 目标：{url.split('?')[0]}")
    print(f"         {note}")
    print("         ℹ️ 字段形状：实测是 `data.URL`（扁平），SDK 文档写的 `data.endpoint.URL`")

    if not do_wss:
        print("         （--skip-wss：跳过真实握手）")
        return
    try:
        asyncio.run(_handshake(url))
    except Exception as exc:  # noqa: BLE001
        report.check("WSS 握手成功", False, f"{type(exc).__name__}: {exc}")
        return
    report.check("WSS 握手成功（随后主动关闭，无残留连接）", True)


def print_manual_steps() -> None:
    """G. 端到端只能人工 —— 打印步骤，**不判 FAIL**。"""
    step("G. 人工步骤（脚本无法代做，故不计入失败）")
    for line in MANUAL_STEPS:
        print(f"  · {line}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行。"""
    parser = argparse.ArgumentParser(description="Recall 飞书入口验收（roadmap R-49e）")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="本机 API 根地址")
    parser.add_argument(
        "--api-key",
        default=None,
        help="本机 token；不传则取 .env 的 RECALL_WATCHDOG_API_KEY（否则取 key 表首项）",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="单次请求超时秒数")
    parser.add_argument("--skip-network", action="store_true", help="只做本地检查 A~E")
    parser.add_argument("--skip-wss", action="store_true", help="做端点发现但不真握手")
    return parser.parse_args(argv)


def resolve_api_key(settings: Any, explicit: str | None) -> str | None:
    """决定回环调用本机 API 用的 token。"""
    if explicit:
        return explicit
    if settings.watchdog_api_key:
        return str(settings.watchdog_api_key)
    return next(iter(settings.api_keys), None)


def main(argv: list[str] | None = None) -> int:
    """跑完全部检查，返回**失败项数**（0 即通过）。"""
    args = parse_args(argv)
    report = Report()

    print("Recall 飞书入口验收（roadmap R-49e）")
    print("模式：只读、无副作用；退出码 = 失败项数")

    settings = _load_settings()
    check_config(report, settings)
    check_dependencies(report)
    check_sdk_surface(report)
    check_card_rendering(report)

    if args.skip_network:
        print("\n（--skip-network：跳过 E/F 两节）")
    else:
        check_local_api(report, args.api_url, resolve_api_key(settings, args.api_key), args.timeout)
        check_long_connection(
            report,
            (settings.feishu_app_id or "").strip(),
            (settings.feishu_app_secret or "").strip(),
            args.timeout,
            do_wss=not args.skip_wss,
        )

    print_manual_steps()

    print(f"\n结果：{report.passed} 项通过 / {report.failed} 项失败")
    if report.failed == 0:
        print("⇒ 自动部分全部通过；端到端请按 G 节人工确认。")
    return report.failed


if __name__ == "__main__":
    raise SystemExit(main())
