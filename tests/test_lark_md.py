"""``recall/lark_md.py`` 的用例（roadmap R-49b）。

这一层是**答案正文进入飞书卡片前的唯一防线**，用例分五组钉住：

1. 行内特殊字符必须变成实体（含 ``&`` 不得二次转义）；
2. 块级标记（标题 / 列表 / 分割线）必须被中和 —— 它们**仅飞书 7.6+ 生效**，
   低版本会渲染成"升级提示占位图"；
3. ``**加粗**`` 保留、**落单的 ``**`` 按字面量处理**（不能吃掉后面的内容）；
4. 截断**只切原始字符边界**，绝不切进实体内部（切进去会渲染出 ``&#4`` 这种垃圾）；
5. 确定性：同输入两次转换逐字节一致（code_standards §0.2）。
"""

from __future__ import annotations

import re

import pytest

from recall.lark_md import (
    REFERENCES_HEADING,
    TRUNCATION_MARKER,
    escape_lark_md,
    link,
    render_card_body,
    render_reference_line,
    to_lark_md,
    to_lark_md_within,
)

_ENTITY_RE = re.compile(r"&(?:amp|sim);|&#\d{2,3};")
"""完整的 HTML 实体（本模块只会产出这几种形状）。"""

_NASTY = (
    "**粗** [1] <at id=all></at>\r\n# 标题\n---\n1. 项 `码` ~删~ _斜_ & 转义\n" + "*" * 30 + "\n"
)
"""一坨同时命中所有规则的输入，供确定性与预算用例复用。"""


# ── 1. 行内特殊字符 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("*斜*", "&#42;斜&#42;"),
        ("~删~", "&sim;删&sim;"),
        ("`码`", "&#96;码&#96;"),
        ("_下_", "&#95;下&#95;"),
        ("[1]", "&#91;1&#93;"),
        ("<a>", "&#60;a&#62;"),
        ("a\\b", "a&#92;b"),
    ],
)
def test_inline_specials_become_entities(raw: str, expected: str) -> None:
    assert escape_lark_md(raw) == expected


def test_ampersand_is_escaped_exactly_once() -> None:
    """``&`` 必须最先替换且只替换一次，否则会二次转义刚写出来的实体。"""
    out = escape_lark_md("a & b")
    assert out == "a &amp; b"
    assert "amp;amp" not in out


def test_entity_already_in_the_source_cannot_inject_syntax() -> None:
    """源文本里写死 ``&#42;`` 时，若不再转义 ``&``，渲染端会把它解码成 ``*`` 而变成斜体语法。"""
    assert escape_lark_md("&#42;") == "&amp;#42;"


def test_carriage_returns_are_normalised_to_lf() -> None:
    assert escape_lark_md("甲\r\n乙\r丙") == "甲\n乙\n丙"


def test_at_all_mention_is_neutralised() -> None:
    """``<at id=all></at>`` 是飞书的"@所有人"语法 —— 笔记里出现它不能让机器人替用户 @ 全群。"""
    out = escape_lark_md("注意 <at id=all></at> 全员")
    assert "<at" not in out
    assert "&#60;at id=all&#62;&#60;/at&#62;" in out


# ── 2. 块级标记 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("# 标题", "&#35; 标题"),
        ("## 标题", "&#35;# 标题"),
        ("- 一", "&#45; 一"),
        ("+ 一", "&#43; 一"),
        ("1. 一", "1&#46; 一"),
        ("2) 一", "2&#41; 一"),
        ("---", "&#45;--"),
        ("  - 缩进项", "  &#45; 缩进项"),
    ],
)
def test_block_markers_are_neutralised(raw: str, expected: str) -> None:
    assert escape_lark_md(raw) == expected


def test_marker_characters_mid_line_stay_untouched() -> None:
    """块级标记只在**行首**才是语法；行内的 ``#`` ``-`` 必须原样保留（可读性）。"""
    assert escape_lark_md("见 # 号") == "见 # 号"
    assert escape_lark_md("a - b") == "a - b"
    assert escape_lark_md("甲 --- 乙") == "甲 --- 乙"


def test_no_line_starts_with_a_block_marker_after_escaping() -> None:
    out = escape_lark_md("# 甲\n- 乙\n1. 丙\n---\n## 丁")
    for line in out.split("\n"):
        assert not re.match(r"^[ \t]*(?:#{1,6}|-{3,}|[-+](?=[ \t])|\d{1,3}[.)])", line)


def test_citation_brackets_stop_being_syntax() -> None:
    out = to_lark_md("见 [1](https://example.com) 的结论")
    assert "&#91;1&#93;" in out
    assert "[1]" not in out


# ── 3. 加粗的保留与"落单 **"的处置 ─────────────────────────────────────────────


def test_bold_is_preserved() -> None:
    assert to_lark_md("**重点**") == "**重点**"
    assert to_lark_md("前 **重点** 后") == "前 **重点** 后"


def test_bold_content_is_still_escaped() -> None:
    assert to_lark_md("**重点**：看 *这个*") == "**重点**：看 &#42;这个&#42;"


def test_unpaired_bold_becomes_a_literal_marker() -> None:
    """落单的 ``**`` 不能被当成"加粗开始"，否则会把它后面的内容整段吃掉。"""
    assert to_lark_md("**只开不收") == f"{'&#42;' * 2}只开不收"
    assert to_lark_md("甲 **乙 **丙 **丁") == "甲 **乙 **丙 &#42;&#42;丁"


def test_plain_double_asterisk_is_escaped() -> None:
    assert to_lark_md("*单个*") == "&#42;单个&#42;"


# ── 4. 截断 ──────────────────────────────────────────────────────────────────


def test_within_returns_the_full_text_when_it_fits() -> None:
    assert to_lark_md_within("短文本", 100) == "短文本"


def test_within_truncates_and_appends_the_marker() -> None:
    out = to_lark_md_within("甲" * 50, 20)
    assert len(out) <= 20
    assert out.startswith("甲")
    assert out.endswith(TRUNCATION_MARKER)


def test_the_marker_itself_contains_no_syntax() -> None:
    """标记若含 markdown 特殊字符，它自身就得再转义一轮 —— 保持它干净。"""
    assert escape_lark_md(TRUNCATION_MARKER) == TRUNCATION_MARKER


@pytest.mark.parametrize("limit", [0, -5])
def test_within_non_positive_limit_yields_empty(limit: int) -> None:
    assert to_lark_md_within("甲" * 10, limit) == ""


def test_within_tiny_budget_clips_the_marker_itself() -> None:
    out = to_lark_md_within("甲" * 100, 3)
    assert len(out) <= 3


def test_within_never_splits_an_entity() -> None:
    """截断必须发生在**原始字符边界**上：切进实体内部会渲染出 ``&#4`` 这种垃圾。"""
    out = to_lark_md_within("*" * 500, 60)
    assert len(out) <= 60
    body = out.removesuffix(TRUNCATION_MARKER)
    assert "&" not in _ENTITY_RE.sub("", body)


def test_within_respects_the_budget_for_adversarial_input() -> None:
    """转义会把字符膨胀最多 5 倍 ⇒ 收缩循环必须在膨胀后仍然守住预算。"""
    out = to_lark_md_within(_NASTY * 10, 200)
    assert len(out) <= 200
    assert "&" not in _ENTITY_RE.sub("", out.removesuffix(TRUNCATION_MARKER))


# ── 5. 链接与引用行 ──────────────────────────────────────────────────────────


def test_link_accepts_http_and_https() -> None:
    assert link("文档", "https://example.com/a") == "[文档](https://example.com/a)"
    assert link("文档", "http://example.com/a") == "[文档](http://example.com/a)"


@pytest.mark.parametrize(
    "bad",
    [
        "project/Recall/spec/tech.md",
        "ftp://example.com/a",
        "javascript:alert(1)",
        "",
        "https://example.com/a b",
        "https://example.com/a)b",
    ],
)
def test_link_degrades_to_plain_text_when_the_target_is_unusable(bad: str) -> None:
    out = link("标签", bad)
    assert out.startswith("标签（")
    assert "](" not in out


def test_reference_line_escapes_the_number_and_links_urls() -> None:
    assert render_reference_line(1, "https://example.com/a") == (
        "&#91;1&#93; [https://example.com/a](https://example.com/a)"
    )


def test_reference_line_does_not_duplicate_a_path_source() -> None:
    """vault 相对路径不是 URL ⇒ 只渲染路径，**不能**退化成 ``a.md（a.md）`` 这种自我重复。"""
    assert render_reference_line(2, "project/Recall/spec/tech.md") == (
        "&#91;2&#93; project/Recall/spec/tech.md"
    )


def test_reference_line_reports_a_missing_source() -> None:
    assert render_reference_line(3, "   ") == "&#91;3&#93; （来源缺失）"


# ── 卡片正文组装 ─────────────────────────────────────────────────────────────


def test_card_body_without_references_is_just_the_answer() -> None:
    assert render_card_body("只有正文", []) == "只有正文"


def test_reference_lines_are_consecutive_not_separate_paragraphs() -> None:
    """引用之间必须是**单换行**（连续成行）；只有正文与引用块之间才空一行。

    回归：早期版本用 ``"\\n\\n".join([body, heading, *lines])``，结果每条引用各自成段、
    卡片里多出一串空行（由 ``render_card_body`` 的 doctest 抓到）。
    """
    refs = [{"source_uri": "a.md"}, {"source_uri": "b.md"}]
    assert render_card_body("正文", refs) == "正文\n\n**来源**\n&#91;1&#93; a.md\n&#91;2&#93; b.md"


def test_card_body_falls_back_when_the_answer_is_empty() -> None:
    out = render_card_body("   ", [])
    assert out
    assert "未生成" in out


def test_card_body_truncates_only_the_answer_and_keeps_every_reference() -> None:
    """引用是"可核对"的落点 —— 截断只能砍正文，一条引用都不许少。"""
    refs = [{"ref_id": str(i), "source_uri": f"notes/{i}.md"} for i in range(1, 6)]
    out = render_card_body("甲" * 5000, refs, max_chars=100)
    assert out.count(REFERENCES_HEADING) == 1
    assert TRUNCATION_MARKER in out
    for i in range(1, 6):
        assert f"&#91;{i}&#93; notes/{i}.md" in out


def test_card_body_handles_a_reference_without_source_uri() -> None:
    out = render_card_body("正文", [{"ref_id": "1"}])
    assert "（来源缺失）" in out


# ── 确定性 ───────────────────────────────────────────────────────────────────


def test_conversion_is_deterministic() -> None:
    """同一输入两次转换必须逐字节一致（code_standards §0.2）。"""
    assert to_lark_md_within(_NASTY, 120) == to_lark_md_within(_NASTY, 120)
    assert escape_lark_md(_NASTY) == escape_lark_md(_NASTY)
    assert to_lark_md(_NASTY) == to_lark_md(_NASTY)
