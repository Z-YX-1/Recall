r"""把一批笔记批量标成 ``visibility: public``（roadmap R-40 完整版的前置，2026-10-03）。

## 为什么需要它

`visibility` 来自笔记 frontmatter（`tech.md` §3.4）—— 也就是说 **"哪些内容对外可见"这件事，
真相在 vault 里，不在我们的配置里**。这是**有意的**：换台机器用同一份 vault 重建，
权限结果必须一致；把可见范围塞进 `.env` 会让"重建"与"配置"隐式耦合。

代价是**要一篇篇改 frontmatter** ⇒ 本工具把这件事变成一条幂等命令。

## 它做什么 / 不做什么

- ✅ 在 frontmatter 块**首行插入** ``visibility: public``（**只插一行，其余内容一字不动**）；
- ✅ 没有 frontmatter 的文件 ⇒ 补一个最小块（``---`` / ``visibility: public`` / ``---``）；
- ✅ **幂等**：已是 ``public`` 的跳过；**显式写了别的值（如 ``private``）也跳过、不覆盖**
  —— 显式意图优先，且方向是 fail-closed（不会把你想私有的东西变公开）；
- ❌ **不删不改任何别的键**，不做 YAML 重排（用文本级插入，而不是 `frontmatter.dumps`，
  以免把你笔记里 `aliases` / 引号 / 注释的排版整个重写）；
- ❌ 不碰 Qdrant / 注册表 —— 改完要**自己跑一次摄取**（见下面的"用法"）。

⚠️ **默认只列出、不动任何文件**；真正写要显式加 ``--yes``（同 `clean_qdrant_orphans.py` 的约定）。

## 用法

先把一篇笔记标公开、并让它**在知识库里生效**::

    cd /d D:\Project\Recall
    python tools\mark_public.py --prefix AI/            :: 只列出会改哪些（默认）
    python tools\mark_public.py --prefix AI/ --yes      :: 真正写入
    python ingest.py --update                            :: 触发重索引（关键一步！）

⚠️ **改完必须重新摄取**：Qdrant payload 与注册表账本里存的还是旧的 ``private``
⇒ 不重新摄取的话，**外部身份依然什么都看不到**，而你会以为"工具没生效"。
``--update`` 就够（正文哈希未变、但权限三元组变了 ⇒ 账本会放行这几篇重灌）；
``python ingest.py --rebuild`` 是**整篇重灌**（持有模型锁数分钟、期间检索排队），
只有怀疑账本失真时才需要，不是本流程的必需项。

新增 AI 笔记后**重跑一次本工具**即可，已标过的会被跳过。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # 允许 `python tools/mark_public.py` 直接跑
    sys.path.insert(0, str(REPO_ROOT))

from recall.config import Settings  # noqa: E402
from recall.connectors.obsidian import DEFAULT_SKIP_DIRS, MARKDOWN_SUFFIX  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VISIBILITY_KEY = "visibility"
PUBLIC_VALUE = "public"
FRONTMATTER_DELIMITER = "---"
BOM = "\ufeff"


@dataclass(frozen=True, slots=True)
class Decision:
    """对单个文件该做什么。

    Attributes:
        action: ``insert``（块内插一行）/ ``create``（补一个块）/
            ``skip_already``（已是目标值）/ ``skip_explicit``（显式写了别的值）/ ``error``。
        new_text: 新正文；``None`` 表示不写。
        detail: 给人看的说明。
    """

    action: str
    new_text: str | None
    detail: str


def decide(text: str, *, value: str = PUBLIC_VALUE) -> Decision:
    """决定对该文件做什么（**纯函数**，便于用例覆盖所有分支）。

    只做文本级插入：**除新增的那一行，原文件逐字节保留**（含原有的行尾风格）。

    Args:
        text: 文件原文。
        value: 要写入的 ``visibility`` 取值。

    Returns:
        :class:`Decision`。
    """
    eol = "\r\n" if "\r\n" in text else "\n"
    body = text.lstrip(BOM)
    lines = body.splitlines(keepends=True)

    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        block = (
            f"{FRONTMATTER_DELIMITER}{eol}"
            f"{VISIBILITY_KEY}: {value}{eol}"
            f"{FRONTMATTER_DELIMITER}{eol}{eol}"
        )
        return Decision(
            "create",
            block + body,
            f"原本没有 frontmatter ⇒ 补一个并写入 {VISIBILITY_KEY}: {value}",
        )

    for index in range(1, len(lines)):
        if lines[index].strip() != FRONTMATTER_DELIMITER:
            continue
        for line in lines[1:index]:
            stripped = line.strip()
            if not stripped.startswith(f"{VISIBILITY_KEY}:"):
                continue
            existing = stripped.split(":", 1)[1].strip().strip("\"'")
            if existing == value:
                return Decision("skip_already", None, f"已是 {VISIBILITY_KEY}: {value}")
            return Decision(
                "skip_explicit",
                None,
                f"显式写了 {VISIBILITY_KEY}: {existing} ⇒ **不覆盖**"
                "（显式意图优先，方向 fail-closed）",
            )
        inserted = [lines[0], f"{VISIBILITY_KEY}: {value}{eol}", *lines[1:]]
        return Decision(
            "insert", "".join(inserted), f"在 frontmatter 首行插入 {VISIBILITY_KEY}: {value}"
        )

    return Decision("error", None, "frontmatter 没有闭合的 --- ⇒ 跳过（绝不猜、绝不改写）")


def select_files(vault: Path, prefix: str) -> list[Path]:
    """列出 vault 内相对路径以 ``prefix`` 开头、且未被跳过的 Markdown 文件。

    Args:
        vault: vault 根目录。
        prefix: 相对路径前缀（POSIX 风格，如 ``AI/``）。

    Returns:
        按相对路径排序的文件列表。
    """
    selected: list[Path] = []
    for path in vault.rglob(f"*{MARKDOWN_SUFFIX}"):
        relative = path.relative_to(vault)
        if any(part.startswith(".") or part in DEFAULT_SKIP_DIRS for part in relative.parts[:-1]):
            continue
        if relative.as_posix().startswith(prefix):
            selected.append(path)
    return sorted(selected)


def indexed_doc_uris(db: Path) -> set[str] | None:
    """读注册表里**已索引**的 ``source_uri`` 集合；读不到返回 ``None``。

    用途：告诉用户"这次改动里有多少篇**本来就在知识库里**" —— 不在库里的文件标了也不会生效，
    不说明白的话很容易误判成"工具没起作用"。
    """
    if not db.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT source_uri FROM documents").fetchall()
    except sqlite3.Error:
        return None
    return {str(row[0]) for row in rows}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行。"""
    parser = argparse.ArgumentParser(description="批量把笔记标成 visibility: public")
    parser.add_argument("--prefix", required=True, help="相对路径前缀（POSIX 风格），如 AI/")
    parser.add_argument("--vault", default=None, help="vault 根目录；默认取 RECALL_VAULT_PATH")
    parser.add_argument("--yes", action="store_true", help="真正写入（默认只列出）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """跑完：列出（可选写入）会改动的文件，返回失败文件数。"""
    args = parse_args(argv)
    settings = Settings.from_env()
    vault = Path(args.vault) if args.vault else settings.vault_path
    if vault is None or not vault.is_dir():
        print(f"[FAIL] vault 不可用：{vault}（设 RECALL_VAULT_PATH 或传 --vault）")
        return 1

    prefix = args.prefix.replace("\\", "/")
    print()
    print("=" * 66)
    print(" 批量标公开（visibility: public）")
    print(f" vault  ：{vault}")
    print(f" 前缀   ：{prefix}")
    print("=" * 66)
    if not prefix.endswith("/"):
        print(
            f"⚠️ 前缀不以 / 结尾 ⇒ 除了 `{prefix}/` 目录，还会匹配 `{prefix}xxx.md`"
            " 这类**同前缀文件**。"
        )

    files = select_files(vault, prefix)
    if not files:
        print(f"\n没有匹配的文件 ✓（前缀 {prefix!r}）\n")
        return 0

    indexed = indexed_doc_uris(settings.registry_db)
    planned: list[tuple[Path, Decision]] = []
    failed: list[tuple[Path, str]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            failed.append((path, f"{type(exc).__name__}: {exc}"))
            continue
        planned.append((path, decide(text)))

    _print_plan(planned, failed, vault, indexed)

    writable = [
        (path, decision) for path, decision in planned if decision.action in {"insert", "create"}
    ]
    if not writable:
        print("\n没有任何文件需要改动 ✓\n")
        return 0
    if not args.yes:
        print("\n（默认只列出，不写任何文件）确认后重跑并加 --yes：")
        print(f"  {sys.executable} tools\\mark_public.py --prefix {prefix} --yes")
        print("⚠️ 写完之后**必须重新摄取**，否则可见性不会生效：")
        print(f"  {sys.executable} ingest.py --update\n")
        return 0

    written = 0
    for path, decision in writable:
        assert decision.new_text is not None  # insert/create 必有新正文
        try:
            path.write_text(decision.new_text, encoding="utf-8", newline="")
            written += 1
        except OSError as exc:
            failed.append((path, f"写入失败 {type(exc).__name__}: {exc}"))
    print(f"\n已写入 {written}/{len(writable)} 个文件")
    if failed:
        for path, reason in failed:
            print(f"  [FAIL] {path.name}：{reason}")
    print("\n🔴 **下一步（关键）**：重新摄取，否则可见性不生效 ——")
    print(f"  {sys.executable} ingest.py --update")
    print("  （怀疑账本失真时才用整篇重灌：python ingest.py --rebuild）\n")
    return len(failed)


def _print_plan(
    planned: list[tuple[Path, Decision]],
    failed: list[tuple[Path, str]],
    vault: Path,
    indexed: set[str] | None,
) -> None:
    """打印每个文件的判定，并汇总"其中多少篇本来就在知识库里"。"""
    buckets: dict[str, list[tuple[Path, Decision]]] = {}
    for path, decision in planned:
        buckets.setdefault(decision.action, []).append((path, decision))

    for action, title in (
        ("insert", "① 将在 frontmatter 首行插入"),
        ("create", "② 原本没有 frontmatter、将补一个块"),
        ("skip_already", "③ 已是 public（跳过）"),
        ("skip_explicit", "④ 显式写了别的值（跳过、不覆盖）"),
        ("error", "⑤ 有问题（跳过）"),
    ):
        items = buckets.get(action)
        if not items:
            continue
        print(f"\n{title}（{len(items)} 个）：")
        for path, decision in items:
            relative = path.relative_to(vault).as_posix()
            mark = ""
            if indexed is not None:
                mark = "  [已在知识库]" if relative in indexed else "  [不在知识库，标了也不生效]"
            print(f"  {decision.detail} | {relative}{mark}")

    if failed:
        print(f"\n⑥ 读不出来的文件（跳过，{len(failed)} 个）：")
        for path, reason in failed:
            print(f"  {path.relative_to(vault).as_posix()}：{reason}")

    if indexed is None:
        print("\n（读不到注册表 ⇒ 无法判断哪些文件已在知识库里）")
        return
    affected = [
        path.relative_to(vault).as_posix()
        for path, decision in planned
        if decision.action in {"insert", "create"} and path.relative_to(vault).as_posix() in indexed
    ]
    print(f"\n本次改动中**已在知识库里**的：{len(affected)} 篇 ⇒ 重新摄取后生效")


if __name__ == "__main__":
    raise SystemExit(main())
