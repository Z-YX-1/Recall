r"""R-40 完整版验收：**跨身份可见性隔离**（roadmap R-40，2026-10-03）。

## 它验什么 / 不验什么

`tools/verify_phase6.py` 只证明"**门锁上了**"（无 key ⇒ 401、有 key ⇒ 200）。
那**不等于**隔离 —— 隔离要证明的是"**换个身份就看不见了，而且客户端绕不过**"。
本脚本补的就是这一段，共六节：

====  ==========================================================================
A     前置：从 **`.env`** 解析 key 表，列出身份（**绝不打印 token**）
B     取材：从注册表挑一篇 `private` + 一篇 `public`，各取一句**原文**当探针查询
C     判据 1：`me` 查私有片段 ⇒ **应命中**
D     判据 2：`stock_user` 查**同一**片段 ⇒ **应空**（这是隔离的核心）
E     判据 3：`stock_user` 查公开片段 ⇒ **应命中**（证明不是"整个库都看不到"）
F     判据 4：**对抗性** —— `stock_user` 手动带 `filter` 索要 `private` ⇒ **仍应空**
G     判据 5：审计里出现 `user=stock_user`
H     判据 6：`/kb/stats` 是**部署级**端点 ⇒ 非所有者**应 403**、所有者仍 200
====  ==========================================================================

## 为什么用"原文片段"当查询

判据 2/4 要排除"恰好没召回"这种**假通过**：查询直接取自那篇私有笔记的**原文**，
若过滤失效，向量检索**几乎必然**命中它。所以"空证据"才有说服力。

## 用法

    python tools/verify_r40.py                 :: 默认本机、从 .env 取 key
    python tools/verify_r40.py --base https://recall.iamzyx.xyz

退出码 = 失败项数（同 `verify_phase6.py` / `verify_r47.py` 的约定）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # 允许 `python tools/verify_r40.py` 直接跑
    sys.path.insert(0, str(REPO_ROOT))

from recall.config import Settings  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TIMEOUT_S = 300.0
"""单次请求超时：`/kb/answer` 要过一次 DeepSeek，比检索端点慢得多。"""

GATE_DECLINE_MARKER = "笔记里没有检索到"
"""门槛拒答的固定措辞（`recall/api.py::kb_answer_core`）—— 证据被拦时返回它。"""

PUBLIC_ROLE = "stock_user"
"""要验收的**外部身份**名（`RECALL_API_KEYS` 里的 `user` 字段）。"""

INSIDER_ROLE = "me"
"""内部身份名。"""


class Report:
    """收集检查结果并即时打印（与 `verify_phase6.py` 同形状）。"""

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
        """打印一条跳过。"""
        self.skipped += 1
        print(f"  [SKIP] {name}  （{reason}）")


def step(title: str) -> None:
    """打印一个步骤标题。"""
    print()
    print("-" * 72)
    print(f" {title}")
    print("-" * 72)


def mask(token: str) -> str:
    """把 token 缩成 `abcd…wxyz`（**任何时候都不打印完整值**）。"""
    return f"{token[:4]}…{token[-4:]}" if len(token) > 10 else "****"


def load_keys(env_file: Path) -> dict[str, str]:
    """从 `.env` 读 `RECALL_API_KEYS` → `{user: token}`（同名取第一条）。

    ⚠️ **只读、只返回、不打印**。本脚本任何输出都不含完整 token。
    """
    if not env_file.is_file():
        return {}
    text = env_file.read_bytes().decode("utf-8")
    for line in text.splitlines():
        if not line.startswith("RECALL_API_KEYS="):
            continue
        out: dict[str, str] = {}
        for entry in line.split("=", 1)[1].split(","):
            token, _, user = entry.partition(":")
            token, user = token.strip(), user.strip()
            if token and user and user not in out:
                out[user] = token
        return out
    return {}


def post(base: str, path: str, body: dict[str, Any], token: str | None) -> tuple[int, Any]:
    """POST 一个 JSON 端点，返回 ``(状态码, 解析后的 JSON 或原文)``。"""
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["X-API-Key"] = token
    request = urllib.request.Request(  # noqa: S310 - 回环或用户显式给出的地址
        f"{base.rstrip('/')}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw[:300]
    except Exception as exc:  # noqa: BLE001 - 探针要报告任何失败
        return 0, f"{type(exc).__name__}: {exc}"


def get(base: str, path: str, token: str | None) -> tuple[int, Any]:
    """GET 一个 JSON 端点。"""
    headers = {"X-API-Key": token} if token else {}
    request = urllib.request.Request(  # noqa: S310
        f"{base.rstrip('/')}{path}", headers=headers, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")[:300]
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def search(base: str, token: str, query: str, flt: dict[str, Any] | None = None) -> tuple[int, Any]:
    """调 `/kb/search`。"""
    body: dict[str, Any] = {"query": query, "top_k": 20, "max_tokens": 3000}
    if flt is not None:
        body["filter"] = flt
    return post(base, "/kb/search", body, token)


def pick_probe_doc(db: Path, visibility: str) -> tuple[str, int] | None:
    """挑一篇该可见性下 **chunk 最多**的文档（返回 ``(source_uri, chunk_count)``）。"""
    if not db.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT source_uri, chunk_count FROM documents "
                "WHERE visibility = ? AND error IS NULL AND chunk_count > 0 "
                "ORDER BY chunk_count DESC LIMIT 1",
                (visibility,),
            ).fetchone()
    except sqlite3.Error:
        return None
    return (str(row[0]), int(row[1])) if row else None


def probe_snippet(vault: Path, source_uri: str) -> str:
    """从该文档正文里取一句**原文**当查询（跳过 frontmatter）。

    用原文而不是自造问题，是为了让判据 2/4 的"空证据"**有说服力**：
    若权限过滤失效，向量检索几乎必然命中这段文字。
    """
    path = vault / source_uri
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                start = index + 1
                break
    best = ""
    for line in lines[start:]:
        stripped = line.strip().lstrip("#").strip()
        if 40 <= len(stripped) <= 90 and len(stripped) > len(best):
            best = stripped
    if not best:  # 兜底：取整篇里最长的一段
        best = max((line.strip() for line in lines[start:]), key=len, default="")[:80]
    return best


@dataclass(frozen=True, slots=True)
class Probe:
    """一次检索的结果摘要。"""

    status: int
    evidence: int
    sources: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == 200


def summarize(status: int, payload: Any) -> Probe:
    """把 `/kb/search` 的响应压成 :class:`Probe`。"""
    if not isinstance(payload, dict):
        return Probe(status, 0, ())
    evidence = payload.get("evidence") or []
    sources = tuple(str(item.get("source_uri") or "") for item in evidence)
    return Probe(status, len(evidence), sources)


def section_keys(report: Report, keys: dict[str, str]) -> None:
    """A. 前置：身份清单（**只显示掩码**）。"""
    step("A. 前置：从 .env 读到的身份（token 只显示掩码）")
    if not keys:
        report.check("`.env` 里有 key 表", False, "没读到 RECALL_API_KEYS ⇒ 无法验收")
        return
    for user, token in keys.items():
        report.info(f"{user} ⇒ {mask(token)}")
    report.check(f"存在外部身份 {PUBLIC_ROLE!r}", PUBLIC_ROLE in keys)
    report.check(f"存在内部身份 {INSIDER_ROLE!r}", INSIDER_ROLE in keys)


def section_probes(report: Report, vault: Path, db: Path) -> tuple[str, str, str, str]:
    """B. 取材：返回 ``(私有 uri, 私有片段, 公开 uri, 公开片段)``。"""
    step("B. 取材：各挑一篇 chunk 最多的文档，取一句原文当查询")
    private = pick_probe_doc(db, "private")
    public = pick_probe_doc(db, "public")
    report.check("注册表里有 private 文档", private is not None)
    report.check("注册表里有 public 文档（R-40 前置）", public is not None)
    if private is None or public is None:
        return "", "", "", ""
    private_uri, private_chunks = private
    public_uri, public_chunks = public
    private_query = probe_snippet(vault, private_uri)
    public_query = probe_snippet(vault, public_uri)
    report.info(f"私有：{private_uri}（{private_chunks} chunk）")
    report.info(f"     探针查询：{private_query[:60]}…")
    report.info(f"公开：{public_uri}（{public_chunks} chunk）")
    report.info(f"     探针查询：{public_query[:60]}…")
    report.check("两篇的探针片段都取到了", bool(private_query) and bool(public_query))
    return private_uri, private_query, public_uri, public_query


def section_isolation(
    report: Report,
    base: str,
    keys: dict[str, str],
    private_uri: str,
    private_query: str,
    public_uri: str,
    public_query: str,
) -> None:
    """C/D/E. 三条主判据：内部看得见、外部看不见私有、外部看得见公开。"""
    me_token = keys.get(INSIDER_ROLE, "")
    stock_token = keys.get(PUBLIC_ROLE, "")

    step("C. 判据 1：`me` 查**私有**片段 ⇒ 应命中")
    insider = summarize(*search(base, me_token, private_query))
    report.check(
        f"`me` 能检索到该私有文档（证据 {insider.evidence} 条）",
        insider.ok and insider.evidence > 0,
        f"status={insider.status} evidence={insider.evidence}",
    )

    step("D. 判据 2：`stock_user` 查**同一**片段 ⇒ 应**空**（隔离核心）")
    outsider = summarize(*search(base, stock_token, private_query))
    report.check(
        f"`stock_user` 检索该私有片段得到 **0 条证据**（实得 {outsider.evidence}）",
        outsider.ok and outsider.evidence == 0,
        f"status={outsider.status} sources={outsider.sources[:3]}",
    )
    report.check(
        "`stock_user` 的结果里**没有**那篇私有文档",
        private_uri not in outsider.sources,
        f"sources={outsider.sources[:5]}",
    )
    status, payload = post(base, "/kb/answer", {"query": private_query}, stock_token)
    answer = str(payload.get("answer", "")) if isinstance(payload, dict) else ""
    report.check(
        "`/kb/answer` 对 `stock_user` 走**门槛拒答**（不是硬答）",
        status == 200 and GATE_DECLINE_MARKER in answer,
        f"status={status} answer={answer[:80]}",
    )

    step("E. 判据 3：`stock_user` 查**公开**片段 ⇒ 应命中（证明不是整个库都看不到）")
    visible = summarize(*search(base, stock_token, public_query))
    report.check(
        f"`stock_user` 能检索到公开文档（证据 {visible.evidence} 条）",
        visible.ok and visible.evidence > 0,
        f"status={visible.status} evidence={visible.evidence}",
    )
    report.check(
        "命中的就是那篇公开文档",
        public_uri in visible.sources,
        f"sources={visible.sources[:5]}",
    )


def section_adversarial(
    report: Report, base: str, keys: dict[str, str], private_uri: str, private_query: str
) -> None:
    """F. 判据 4：客户端 filter **只能收窄不能放宽**（对抗性）。"""
    step("F. 判据 4：`stock_user` 手动带 filter **索要 private** ⇒ 仍应空")
    stock_token = keys.get(PUBLIC_ROLE, "")
    widening = {"must": [{"key": "visibility", "match": {"value": "private"}}]}
    status, payload = search(base, stock_token, private_query, widening)
    probe = summarize(status, payload)
    report.check(
        f"带 `visibility=private` 的 filter 仍是 **0 条证据**（实得 {probe.evidence}）",
        probe.ok and probe.evidence == 0,
        f"status={probe.status} sources={probe.sources[:3]}",
    )

    step("F2. 直接索要**全部**（宽 filter）也不该漏出私有文档")
    broad = {"should": [{"key": "visibility", "match": {"value": "private"}}]}
    status, payload = search(base, stock_token, private_query, broad)
    probe = summarize(status, payload)
    report.check(
        "宽 filter 下私有文档仍不出现",
        probe.ok and private_uri not in probe.sources,
        f"status={probe.status} evidence={probe.evidence} sources={probe.sources[:5]}",
    )
    report.info(
        "（`effective_filter` 取 `must=[scope, client]` **交集** ⇒ 客户端无法放宽可见范围）"
    )


def section_audit(report: Report, settings: Settings) -> None:
    """G. 判据 5：审计留痕能归因到外部身份。"""
    step("G. 判据 5：审计里出现 `user=stock_user`")
    path = settings.log_dir / "audit.jsonl"
    if not path.is_file():
        report.check("审计文件存在", False, f"未找到 {path}（受 RECALL_LOG_TO_FILE 控制）")
        return
    hits = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("user") == PUBLIC_ROLE:
            hits += 1
    report.check(f"审计里有 {hits} 条 `{PUBLIC_ROLE}` 的请求记录", hits > 0)
    report.info("（审计**绝不写密钥**；可归因是公网接入时唯一能区分远程调用者的字段）")


def section_stats(report: Report, base: str, keys: dict[str, str]) -> None:
    """H. 判据 6：**部署级端点只对所有者开放**（非所有者 ⇒ 403）。

    `/kb/stats` 返回的是**部署级**信息（collection 名、模型/切分器版本、**全局**文档数），
    外部身份读它没有正当用途 ⇒ 2026-10-03 项目工程师批准收紧为**只对所有者开放**
    （隧道层本就已在公网挡死它，这里补上**身份维度**，两道一致）。
    """
    step("H. 判据 6：`/kb/stats` 对非所有者**应 403**（部署级信息不外泄）")
    status, payload = get(base, "/kb/stats", keys.get(PUBLIC_ROLE, ""))
    body = payload if isinstance(payload, dict) else {}
    raw_error = body.get("error")
    code = raw_error.get("code") if isinstance(raw_error, dict) else None
    report.check(
        f"`{PUBLIC_ROLE}` 取 `/kb/stats` ⇒ **403 forbidden**（实得 {status}）",
        status == 403 and code == "forbidden",
        f"status={status} code={code!r}",
    )
    if status == 200:
        report.info(
            f"⚠️ 仍读到了全局统计：documents={body.get('documents')}、"
            f"points={body.get('points_count')}、collection={body.get('collection')}"
        )
        report.info(
            "   ⇒ 先怀疑 **API 进程比代码旧**（收紧后没重启）——"
            "`tools/diagnose_answer.py` 的 A 节能直接判出来，别急着改代码"
        )
    insider_status, _ = get(base, "/kb/stats", keys.get(INSIDER_ROLE, ""))
    report.check(
        f"`{INSIDER_ROLE}` 取 `/kb/stats` 仍应 **200**（别把运维挡在门外）",
        insider_status == 200,
        f"status={insider_status}",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行。"""
    parser = argparse.ArgumentParser(description="R-40 完整版验收：跨身份可见性隔离")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="服务地址")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"), help="key 表所在文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """跑完 A~H，返回失败项数。"""
    args = parse_args(argv)
    settings = Settings.from_env()
    report = Report()
    print("=" * 72)
    print(" R-40 完整版验收：跨身份可见性隔离")
    print(f" 服务：{args.base}")
    print(" token 一律从 .env 读、只显示掩码")
    print("=" * 72)

    keys = load_keys(Path(args.env_file))
    section_keys(report, keys)
    if PUBLIC_ROLE not in keys or INSIDER_ROLE not in keys:
        print("\n缺身份 ⇒ 无法继续（配 RECALL_API_KEYS 后**重启 API** 再跑）")
        return report.failed

    vault = settings.vault_path
    if vault is None or not vault.is_dir():
        report.check("vault 可用", False, f"RECALL_VAULT_PATH={vault}")
        return report.failed

    private_uri, private_query, public_uri, public_query = section_probes(
        report, vault, Path(settings.registry_db)
    )
    if not (private_query and public_query):
        return report.failed

    section_isolation(report, args.base, keys, private_uri, private_query, public_uri, public_query)
    section_adversarial(report, args.base, keys, private_uri, private_query)
    section_audit(report, settings)
    section_stats(report, args.base, keys)

    print()
    print("=" * 72)
    verdict = "通过" if report.failed == 0 else "不通过"
    print(
        f" 结论：{verdict} （通过 {report.passed} / 失败 {report.failed} / 跳过 {report.skipped}）"
    )
    print("=" * 72)
    return report.failed


if __name__ == "__main__":
    raise SystemExit(main())
