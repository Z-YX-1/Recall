#!/usr/bin/env python
"""R-49 深度排查脚本：机器人**收得到但回不出去**时用（roadmap R-49c/R-49e）。

它把「回复失败」这条链**逐段拆开、每段都拿真实响应说话**，不靠猜：

===  ==========================================================================
A    **凭证**：`.env` 里的 `FEISHU_APP_ID`/`SECRET`；取 `tenant_access_token`
B    **现场**：解析 `--dump-frame` 落下的原始帧，取出 `message_id`/`chat_id`/发送者 `open_id`
C    **回复·文本**：`POST /im/v1/messages/{id}/reply`，`msg_type=text`
D    **回复·卡片**：同端点，`msg_type=interactive`（机器人真正走的那条）
E    **主动发·文本**：`POST /im/v1/messages`，`receive_id_type=chat_id`（reply 不行时的退路）
F    **结论**：把每段的 `code`/`msg`/`log_id` 汇总成一张表
===  ==========================================================================

每一步都打印飞书的 **`code` / `msg` / `log_id`** —— `log_id` 是找飞书客服时唯一有用的东西。

用法::

    python tools/diagnose_r49.py                 # 全跑（**会给你的飞书发消息**）
    python tools/diagnose_r49.py --dry-run       # 只做 A/B（不产生任何消息）
    python tools/diagnose_r49.py --frame <路径>  # 指定原始帧文件

.. warning::
    C/D/E 三步**会真的往你的飞书会话里发消息**（这正是诊断目的：看它到底能不能发）。
    不想发就加 ``--dry-run``。脚本**绝不打印任何密钥**。
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

FEISHU_BASE = "https://open.feishu.cn"
TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
REPLY_PATH = "/open-apis/im/v1/messages/{message_id}/reply"
SEND_PATH = "/open-apis/im/v1/messages"
DEFAULT_FRAME = REPO_ROOT / "data" / "logs" / "feishu_bot_frame.json"
TIMEOUT_S = 30.0


class Evidence:
    """收集每一段的真实响应，最后汇总。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def record(self, step: str, ok: bool, code: Any, msg: Any, log_id: Any = "") -> None:
        """记一行证据并即时打印。"""
        mark = "[ OK ]" if ok else "[FAIL]"
        print(f"  {mark} {step}: code={code!r} msg={msg!r} log_id={log_id!r}")
        self.rows.append((step, "OK" if ok else "FAIL", str(code), str(msg)))

    def summary(self) -> None:
        """打印汇总表。"""
        print("\n=== F. 汇总 ===")
        for step, status, code, msg in self.rows:
            print(f"  {status:4}  {step:<28} code={code:<12} msg={msg[:60]}")


def _post(path: str, body: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    """POST 一个飞书接口，**把 HTTP 层错误也折成 dict** 以便统一取证。"""
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - 固定官方 https 域名
        f"{FEISHU_BASE}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
            payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            return payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"code": f"HTTP {exc.code}", "msg": raw[:200]}
        return parsed
    except Exception as exc:  # noqa: BLE001 - 探针要报告任何失败
        return {"code": type(exc).__name__, "msg": str(exc)}


def load_env() -> dict[str, str]:
    """从 `.env` 读键值（只读，不进仓库）。"""
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


def step_a_token(env: dict[str, str], evidence: Evidence) -> str | None:
    """A. 取 tenant_access_token。"""
    print("\n=== A. 凭证 / 取 token ===")
    app_id = env.get("FEISHU_APP_ID", "")
    secret = env.get("FEISHU_APP_SECRET", "")
    print(f"  app_id = {app_id[:12]}…（值不敏感）；secret 长度 = {len(secret)}（**值绝不打印**）")
    if not app_id or not secret:
        evidence.record("取 tenant_access_token", False, "-", "凭证缺失", "")
        return None
    payload = _post(TOKEN_PATH, {"app_id": app_id, "app_secret": secret})
    ok = payload.get("code") == 0 and bool(payload.get("tenant_access_token"))
    evidence.record(
        "取 tenant_access_token",
        ok,
        payload.get("code"),
        payload.get("msg"),
        payload.get("log_id", ""),
    )
    return str(payload.get("tenant_access_token") or "") or None


def step_b_frame(frame_path: Path, evidence: Evidence) -> dict[str, str]:
    """B. 从原始帧里取出回复所需的三样东西。"""
    print(f"\n=== B. 现场（原始帧：{frame_path}）===")
    if not frame_path.is_file():
        print("  [warn] 没有原始帧文件 —— 请用 `--dump-frame` 起一次机器人并让它收到一条消息")
        evidence.record("读取原始帧", False, "-", "文件不存在", "")
        return {}
    frame = json.loads(frame_path.read_text(encoding="utf-8"))
    message = ((frame.get("event") or {}).get("message")) or {}
    sender = ((frame.get("event") or {}).get("sender")) or {}
    info = {
        "message_id": str(message.get("message_id") or ""),
        "chat_id": str(message.get("chat_id") or ""),
        "open_id": str((sender.get("sender_id") or {}).get("open_id") or ""),
        "message_type": str(message.get("message_type") or message.get("msg_type") or ""),
        "content": str(message.get("content") or ""),
    }
    print(f"  message_id   = {info['message_id']}")
    print(f"  chat_id      = {info['chat_id']}")
    print(f"  open_id      = {info['open_id']}")
    print(f"  message_type = {info['message_type']!r}")
    print(f"  content      = {info['content'][:80]}")
    evidence.record(
        "读取原始帧",
        bool(info["message_id"]),
        "-",
        f"message_type={info['message_type']}",
        "",
    )
    return info


def step_c_reply_text(token: str, info: dict[str, str], evidence: Evidence) -> None:
    """C. 用**文本**回复那条消息（最小可复现）。"""
    print("\n=== C. 回复·文本（最小可复现）===")
    if not info.get("message_id"):
        evidence.record("reply(text)", False, "-", "缺 message_id", "")
        return
    payload = _post(
        REPLY_PATH.format(message_id=info["message_id"]),
        {
            "msg_type": "text",
            "content": json.dumps({"text": "【Recall 排查】文本回复测试"}, ensure_ascii=False),
        },
        token,
    )
    ok = payload.get("code") == 0
    evidence.record(
        "reply(text)", ok, payload.get("code"), payload.get("msg"), payload.get("log_id", "")
    )


def _card_variants(body_text: str) -> list[tuple[str, dict[str, Any]]]:
    """候选卡片形态 —— 逐个试，**用飞书的返回码决定用哪个**。

    2026-10-03 实测：``schema 2.0`` + ``config.update_multi=false`` 会被拒
    （``code=230099`` / ``ErrCode: 300302; ErrMsg: update_multi is false``）。
    所以这里把"去掉 config""改成 true""退回旧版卡片"都摆出来对比。
    """
    elements = [{"tag": "markdown", "content": body_text}]
    title = {"tag": "plain_text", "content": "Recall · 排查"}
    return [
        (
            "schema2.0 + config.update_multi=false（现状）",
            {
                "schema": "2.0",
                "config": {"update_multi": False},
                "header": {"title": title, "template": "blue"},
                "body": {"elements": elements},
            },
        ),
        (
            "schema2.0 + config.update_multi=true",
            {
                "schema": "2.0",
                "config": {"update_multi": True},
                "header": {"title": title, "template": "blue"},
                "body": {"elements": elements},
            },
        ),
        (
            "schema2.0 无 config",
            {
                "schema": "2.0",
                "header": {"title": title, "template": "blue"},
                "body": {"elements": elements},
            },
        ),
        (
            "旧版卡片（无 schema，elements+header）",
            {
                "config": {"wide_screen_mode": True},
                "elements": elements,
                "header": {"title": title},
            },
        ),
    ]


def step_d_reply_card(token: str, info: dict[str, str], evidence: Evidence) -> None:
    """D. **逐个试候选卡片形态**，用飞书返回码决定哪个可用（机器人真正走的那条路）。"""
    print("\n=== D. 回复·卡片（逐个试形态，用返回码决定）===")
    if not info.get("message_id"):
        evidence.record("reply(interactive)", False, "-", "缺 message_id", "")
        return
    for label, card in _card_variants("【Recall 排查】卡片形态测试"):
        payload = _post(
            REPLY_PATH.format(message_id=info["message_id"]),
            {"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
            token,
        )
        ok = payload.get("code") == 0
        evidence.record(
            f"卡片形态：{label}",
            ok,
            payload.get("code"),
            payload.get("msg"),
            payload.get("log_id", ""),
        )


def step_e_send_text(token: str, info: dict[str, str], evidence: Evidence) -> None:
    """E. **主动发**一条文本到那个会话（reply 受限时的退路）。"""
    print("\n=== E. 主动发·文本（reply 不通时的退路）===")
    if not info.get("chat_id"):
        evidence.record("send(text, chat_id)", False, "-", "缺 chat_id", "")
        return
    payload = _post(
        f"{SEND_PATH}?receive_id_type=chat_id",
        {
            "receive_id": info["chat_id"],
            "msg_type": "text",
            "content": json.dumps({"text": "【Recall 排查】主动发消息测试"}, ensure_ascii=False),
        },
        token,
    )
    ok = payload.get("code") == 0
    evidence.record(
        "send(text, chat_id)",
        ok,
        payload.get("code"),
        payload.get("msg"),
        payload.get("log_id", ""),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行。"""
    parser = argparse.ArgumentParser(description="Recall 飞书入口深度排查（roadmap R-49）")
    parser.add_argument("--frame", type=Path, default=DEFAULT_FRAME, help="原始帧 JSON 路径")
    parser.add_argument("--dry-run", action="store_true", help="只做 A/B，不发送任何消息")
    parser.add_argument(
        "--message-id", default=None, help="覆盖原始帧里的 message_id（测**失败的那条**）"
    )
    parser.add_argument("--chat-id", default=None, help="覆盖原始帧里的 chat_id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """跑完 A~F，返回**失败段数**（0 即全通）。"""
    args = parse_args(argv)
    print("Recall 飞书入口深度排查（roadmap R-49）")
    print("每一步都打印飞书返回的 code / msg / log_id —— 用事实说话")

    evidence = Evidence()
    env = load_env()
    token = step_a_token(env, evidence)
    info = step_b_frame(args.frame, evidence)
    if args.message_id:
        info["message_id"] = args.message_id
        print(f"  [override] message_id = {args.message_id}")
    if args.chat_id:
        info["chat_id"] = args.chat_id
        print(f"  [override] chat_id    = {args.chat_id}")

    if args.dry_run:
        print("\n（--dry-run：跳过 C/D/E，不发送任何消息）")
    elif token is None:
        print("\n[跳过] 没有 token，无法测发送")
    else:
        step_c_reply_text(token, info, evidence)
        step_d_reply_card(token, info, evidence)
        step_e_send_text(token, info, evidence)

    evidence.summary()
    failed = sum(1 for _, status, _, _ in evidence.rows if status == "FAIL")
    print(f"\n结果：{len(evidence.rows) - failed} 段通过 / {failed} 段失败")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
