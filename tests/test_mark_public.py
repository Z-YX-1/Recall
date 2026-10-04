"""``tools/mark_public.py``：批量把笔记标成 ``visibility: public``（roadmap R-40 完整版前置）。

**为什么值得一组专门用例**：这个工具**直接改写用户的手写笔记** —— 项目里少有比它后果更重的
操作。而且 ``visibility`` 决定**对外可见范围**，改错方向就是把私有内容公开。
所以用例重点不在"能插进去"，而在四条**不该动**的保证：

1. **除新增的一行，其余逐字节不变**（文本级插入，不做 YAML 重排）；
2. **行尾风格保持原样**（Windows 上把 CRLF 改成 LF 会在 vault 里造成满屏 diff）；
3. **显式写了别的值就不覆盖**（fail-closed：绝不把你标成 ``private`` 的东西变公开）；
4. **拿不准就跳过**（frontmatter 没闭合 ⇒ 报 error，绝不猜、绝不改写）。

用例全部**不依赖 GPU / Qdrant / 网络**。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tools.mark_public import Decision, decide, indexed_doc_uris, select_files

_PLAIN = '---\ntitle: "标题"\naliases:\n  - 别名\n---\n\n正文第一行\n正文第二行\n'


def _ok(decision: Decision) -> str:
    """取出必定存在的新正文（避免每条用例都写 assert）。"""
    assert decision.new_text is not None
    return decision.new_text


# ── 插入：只加一行，其余一字不动 ─────────────────────────────────────────────


def test_insert_adds_exactly_one_line_right_after_the_opening_delimiter() -> None:
    decision = decide(_PLAIN)

    assert decision.action == "insert"
    new_text = _ok(decision)
    assert new_text.startswith('---\nvisibility: public\ntitle: "标题"\n')
    assert new_text.count("visibility: public") == 1


def test_insert_changes_nothing_else() -> None:
    """🔴 **除新增那一行，原文件必须逐字节保留** —— 这条一破，用户的排版就被重写了。"""
    new_text = _ok(decide(_PLAIN))

    original_lines = _PLAIN.splitlines()
    new_lines = new_text.splitlines()
    assert len(new_lines) == len(original_lines) + 1
    assert new_lines[0] == original_lines[0]
    assert new_lines[2:] == original_lines[1:]


def test_insert_keeps_crlf_line_endings() -> None:
    """🔴 Windows：把 CRLF 写成 LF 会在 vault 里造成满屏 diff。"""
    crlf = _PLAIN.replace("\n", "\r\n")

    new_text = _ok(decide(crlf))

    assert "\r\n" in new_text
    assert "\n" not in new_text.replace("\r\n", ""), "不得混入裸 LF"
    assert new_text.startswith("---\r\nvisibility: public\r\n")


def test_insert_works_when_the_file_starts_with_a_bom() -> None:
    """带 BOM 的文件也要认出来（否则会被当成"没有 frontmatter"而补一个块）。"""
    decision = decide("\ufeff" + _PLAIN)

    assert decision.action == "insert"


# ── 幂等：跑第二次不该再改 ───────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["public", '"public"', "'public'"])
def test_already_public_is_skipped(value: str) -> None:
    """带引号也算已标（YAML 允许 ``visibility: "public"``）—— 幂等是"可反复复跑"的前提。"""
    text = f"---\nvisibility: {value}\ntitle: t\n---\n\n正文\n"

    decision = decide(text)

    assert decision.action == "skip_already"
    assert decision.new_text is None


# ── fail-closed：显式意图优先，绝不把私有的变公开 ────────────────────────────


@pytest.mark.parametrize("value", ["private", "internal", "secret"])
def test_explicit_other_value_is_never_overwritten(value: str) -> None:
    """🔴 **这是本工具最重要的安全保证**：显式写了别的值就跳过。

    写错的可见性只会**更私有**（`ingest` 侧同样 fail-closed），**绝不意外公开**。
    """
    text = f"---\nvisibility: {value}\ntitle: t\n---\n\n正文\n"

    decision = decide(text)

    assert decision.action == "skip_explicit"
    assert decision.new_text is None


def test_visibility_mentioned_in_the_body_is_not_treated_as_frontmatter() -> None:
    """正文里出现 ``visibility: private`` **不算**显式设置 —— 否则会误判成"用户不想公开"。"""
    text = "---\ntitle: t\n---\n\n讨论一下 visibility: private 这个写法的坑\n"

    decision = decide(text)

    assert decision.action == "insert"


# ── 没有 frontmatter / 坏 frontmatter ───────────────────────────────────────


def test_missing_frontmatter_gets_a_minimal_block_and_keeps_the_body() -> None:
    decision = decide("# 只有正文\n\n没有 frontmatter\n")

    assert decision.action == "create"
    new_text = _ok(decision)
    assert new_text.startswith("---\nvisibility: public\n---\n\n# 只有正文")
    assert new_text.endswith("没有 frontmatter\n")


def test_unclosed_frontmatter_is_skipped_instead_of_guessed() -> None:
    """🔴 frontmatter 没闭合 ⇒ **报 error 跳过**。宁可少改一个文件，绝不改坏一个文件。"""
    decision = decide("---\ntitle: t\n\n正文（分隔符丢了）\n")

    assert decision.action == "error"
    assert decision.new_text is None


# ── 选文件：前缀必须带目录边界 ───────────────────────────────────────────────


def _note(root: Path, relative: str, text: str = "---\ntitle: t\n---\n\n正文\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_prefix_matches_the_directory_but_not_same_prefixed_files(tmp_path: Path) -> None:
    """🔴 **`AI/` 不该命中 `AI大模型开发架构大纲MOC.md`** —— 一字之差，后果严重：
    "以为没公开的东西反而公开了"。

    真实 vault 里就同时存在 `AI/` 目录与 `AI大模型开发架构大纲MOC.md`。
    """
    _note(tmp_path, "AI/一篇.md")
    _note(tmp_path, "AI/子目录/两篇.md")
    _note(tmp_path, "AI大模型开发架构大纲MOC.md")
    _note(tmp_path, "project/别的.md")

    selected = [path.relative_to(tmp_path).as_posix() for path in select_files(tmp_path, "AI/")]

    assert set(selected) == {"AI/一篇.md", "AI/子目录/两篇.md"}


def test_prefix_without_slash_does_match_same_prefixed_files(tmp_path: Path) -> None:
    """不加斜杠时**确实会**多命中 —— 这正是工具要警告"前缀不以 / 结尾"的原因。"""
    _note(tmp_path, "AI/一篇.md")
    _note(tmp_path, "AI大模型开发架构大纲MOC.md")

    selected = [path.relative_to(tmp_path).as_posix() for path in select_files(tmp_path, "AI")]

    assert set(selected) == {"AI/一篇.md", "AI大模型开发架构大纲MOC.md"}


def test_hidden_and_skipped_dirs_are_ignored(tmp_path: Path) -> None:
    """``.obsidian`` / ``node_modules`` 之类不该被扫（与 Connector 用同一份跳过表）。"""
    _note(tmp_path, "AI/一篇.md")
    _note(tmp_path, ".obsidian/AI/内部.md")
    _note(tmp_path, "node_modules/AI/三方.md")

    selected = [path.relative_to(tmp_path).as_posix() for path in select_files(tmp_path, "AI/")]

    assert selected == ["AI/一篇.md"]


def test_select_files_returns_sorted_paths(tmp_path: Path) -> None:
    """顺序稳定 ⇒ 输出可复现、便于人工核对与 diff。"""
    for name in ("c", "a", "b"):
        _note(tmp_path, f"AI/{name}.md")

    selected = [path.stem for path in select_files(tmp_path, "AI/")]

    assert selected == ["a", "b", "c"]


# ── 注册表交叉核对：告诉用户"这次改动会不会生效" ─────────────────────────────


def test_indexed_doc_uris_reads_the_registry_readonly(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE documents (source_uri TEXT)")
        connection.execute("INSERT INTO documents VALUES ('AI/a.md')")

    assert indexed_doc_uris(db) == {"AI/a.md"}


def test_indexed_doc_uris_degrades_quietly_when_unavailable(tmp_path: Path) -> None:
    """读不到注册表 ⇒ 返回 ``None``（工具照常列文件，只是少一行提示），**不能因此崩**。"""
    assert indexed_doc_uris(tmp_path / "不存在.db") is None
    assert indexed_doc_uris(tmp_path) is None, "传目录（不是合法 DB）也必须降级而不是抛"
