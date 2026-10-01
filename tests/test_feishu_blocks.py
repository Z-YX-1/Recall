"""飞书 blocks → Markdown 的转换测试（roadmap R-41d；纯函数，不需要网络/GPU/Qdrant）。

覆盖三类风险：

1. **结构**：扁平 blocks + `children` 引用能正确重建层级（标题 `#` 级数、列表缩进、表格行列）；
2. **不静默丢**：不认识的块类型必须**计数**（`skipped`），而不是悄悄消失；
3. **确定性**：同一份输入两次转换**逐字节一致** —— 幂等依赖 `content_hash`，转换一旦不确定，
   每轮 run 都会把整库重灌（复查表第 ⑨ 条）。
"""

from __future__ import annotations

from typing import Any

from recall.connectors.feishu_blocks import blocks_to_markdown


def _plain(
    block_id: str, kind: str, text: str, children: list[str] | None = None
) -> dict[str, Any]:
    """构造一个带文本、可指定 children 的块。"""
    return {
        "block_id": block_id,
        "children": children or [],
        "elements": [{"text_run": {"content": text, "text_style": {}}}],
        kind: {},
    }


def _doc(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """把一堆块挂到根 page 块下（模拟真实文档：page 是根）。"""
    root = {"block_id": "page", "block_type": 1, "children": [b["block_id"] for b in blocks]}
    root["page"] = {}
    return root


def test_headings_become_hash_levels_and_title_is_prepended() -> None:
    """文档标题要变成 `#`（块里没有标题，它来自文档元信息）—— 精排吃 heading_path。"""
    blocks = [
        _doc([]),
        _plain("h1", "heading1", "一级标题"),
        _plain("h2", "heading2", "二级标题"),
        _plain("p", "text", "正文一段"),
    ]
    blocks[0]["children"] = ["h1", "h2", "p"]

    result = blocks_to_markdown(blocks, title="我的笔记")

    assert result.markdown.startswith("# 我的笔记\n\n# 一级标题\n\n## 二级标题")
    assert "正文一段" in result.markdown
    # mapped_count 数的是**块**（容器块不计），文档标题是我们额外加的一行、不算块
    assert result.mapped_count == 3
    assert result.skipped == {}


def test_list_items_are_indented_by_tree_depth() -> None:
    """嵌套列表按树深度缩进（扁平列表 + children 引用要能还原层级）。"""
    blocks = [
        _doc([]),
        _plain("b1", "bullet", "外层项", ["b2"]),
        _plain("b2", "bullet", "内层项"),
        _plain("o1", "ordered", "有序项"),
    ]
    blocks[0]["children"] = ["b1", "o1"]

    markdown = blocks_to_markdown(blocks).markdown

    assert "- 外层项" in markdown
    assert "\n  - 内层项" in markdown  # 深一层 ⇒ 缩进两空格
    assert "1. 有序项" in markdown


def test_code_quote_todo_and_divider() -> None:
    """代码块用围栏、引用逐行加 `>`、待办带勾选框、分割线为 `---`。"""
    todo_payload = {"style": {"done": True}}
    blocks = [
        _doc([]),
        _plain("c", "code", "print(1)"),
        _plain("q", "quote", "引用一\n引用二"),
        {"block_id": "t", "children": [], "todo": todo_payload,
         "elements": [{"text_run": {"content": "已完成的待办", "text_style": {}}}]},
        {"block_id": "t2", "children": [], "todo": {"style": {"done": False}},
         "elements": [{"text_run": {"content": "未完成", "text_style": {}}}]},
        {"block_id": "d", "children": [], "divider": {}},
    ]
    blocks[0]["children"] = ["c", "q", "t", "t2", "d"]

    markdown = blocks_to_markdown(blocks).markdown

    assert "```\nprint(1)\n```" in markdown
    assert "> 引用一\n> 引用二" in markdown
    assert "- [x] 已完成的待办" in markdown
    assert "- [ ] 未完成" in markdown
    assert markdown.endswith("---")  # 分割线（Markdown 已被 strip 掉尾部空行）


def test_text_run_and_text_runs_shapes_are_both_supported() -> None:
    """文档里两种字段名都要认（`text_run.content` 与 `text_runs[].text`）—— 复查表第 ⑩ 条。"""
    blocks = _doc([])
    blocks["children"] = ["a", "b"]
    single = {
        "block_id": "a",
        "children": [],
        "text": {},
        "elements": [{"text_run": {"content": "单数写法", "text_style": {}}}],
    }
    multi = {
        "block_id": "b",
        "children": [],
        "text": {},
        "elements": [{"text_runs": [{"text": "复数写法"}]}],
    }

    markdown = blocks_to_markdown([blocks, single, multi]).markdown

    assert "单数写法" in markdown
    assert "复数写法" in markdown


def test_elements_nested_in_content_field_are_supported() -> None:
    """`elements` 嵌在内容字段里（`text.elements`）也要认 —— 官方两种形状都出现过。

    ⚠️ 这条是**实测踩出来的**：初版只认"嵌在内容字段里"的形状，而另一处示例是"挂在块顶层"，
    两种混用会让整篇转成空 Markdown（静默、且空正文 hash 稳定 ⇒ 库里留下空白文档）。
    """
    root: dict[str, Any] = {"block_id": "page", "page": {}, "children": ["h", "p"]}
    heading = {
        "block_id": "h",
        "children": [],
        "heading1": {"elements": [{"text_run": {"content": "嵌在字段里的标题", "text_style": {}}}]},
    }
    para = {
        "block_id": "p",
        "children": [],
        "text": {"elements": [{"text_run": {"content": "嵌在字段里的正文", "text_style": {}}}]},
    }

    markdown = blocks_to_markdown([root, heading, para]).markdown

    assert "# 嵌在字段里的标题" in markdown
    assert "嵌在字段里的正文" in markdown


def test_inline_code_and_link_are_kept_styles_are_flattened() -> None:
    """只保留行内代码与链接；加粗/斜体扁平化（编辑风格变化不该触发重灌）。"""
    blocks = _doc([])
    blocks["children"] = ["p"]
    para = {
        "block_id": "p",
        "children": [],
        "text": {},
        "elements": [
            {"text_run": {"content": "加粗", "text_style": {"bold": True}}},
            {"text_run": {"content": "代码", "text_style": {"inline_code": True}}},
            {"text_run": {"content": "链接", "text_style": {"link": {"url": "https://x.test"}}}},
        ],
    }

    markdown = blocks_to_markdown([blocks, para]).markdown

    assert "加粗" in markdown and "**加粗**" not in markdown
    assert "`代码`" in markdown
    assert "[链接](https://x.test)" in markdown


def test_table_becomes_markdown_table() -> None:
    """表格块 → Markdown 表格（单元格内容是子块，取整棵子树的文本）。"""
    table = {
        "block_id": "tb",
        "children": ["c1", "c2", "c3", "c4"],
        "table": {"property": {"row_size": 2, "column_size": 2}},
    }
    cells = []
    for index, text in enumerate(["姓名", "年龄", "张三", "30"], start=1):
        cells.append(
            {
                "block_id": f"c{index}",
                "children": [],
                "table_cell": {},
                "elements": [{"text_run": {"content": text, "text_style": {}}}],
            }
        )
    root = _doc([])
    root["children"] = ["tb"]

    markdown = blocks_to_markdown([root, table, *cells]).markdown

    assert "| 姓名 | 年龄 |" in markdown
    assert "| --- | --- |" in markdown
    assert "| 张三 | 30 |" in markdown


def test_containers_keep_their_children_but_emit_nothing_themselves() -> None:
    """容器块（分栏/高亮块/引用容器）自身不出内容，但**子块必须照常渲染**（否则内容丢失）。"""
    root = _doc([])
    root["children"] = ["callout"]
    callout = {"block_id": "callout", "children": ["grid"], "callout": {}}
    grid = {"block_id": "grid", "children": ["col"], "grid": {}}
    col = {"block_id": "col", "children": ["inner"], "grid_column": {}}
    inner = _plain("inner", "text", "分栏里的正文")

    markdown = blocks_to_markdown([root, callout, grid, col, inner]).markdown

    assert "分栏里的正文" in markdown


def test_unmapped_blocks_are_counted_not_silently_dropped() -> None:
    """不认识/不映射的块要**计数**（图片、电子表格、未知类型）—— 复查表第 ②/③ 条。"""
    root = _doc([])
    root["children"] = ["img", "sheet", "weird", "p"]
    blocks = [
        root,
        {"block_id": "img", "children": [], "image": {"token": "x"}},
        {"block_id": "sheet", "children": [], "sheet": {"token": "y"}},
        {"block_id": "weird", "children": [], "block_type": 9999},
        _plain("p", "text", "正常正文"),
    ]

    result = blocks_to_markdown(blocks)

    assert "正常正文" in result.markdown
    assert result.skipped == {"image": 1, "sheet": 1, "type:9999": 1}
    assert result.block_count == 5  # 根 page + img + sheet + weird + p


def test_conversion_is_deterministic() -> None:
    """同一份输入两次转换**逐字节一致** —— 幂等依赖 content_hash（复查表第 ⑨ 条）。"""
    root = _doc([])
    root["children"] = ["h", "b", "c"]
    blocks = [
        root,
        _plain("h", "heading1", "标题"),
        _plain("b", "bullet", "项", ["b2"]),
        _plain("b2", "bullet", "子项"),
        _plain("c", "code", "x = 1"),
    ]

    first = blocks_to_markdown(blocks, title="T")
    second = blocks_to_markdown(blocks, title="T")

    assert first == second
    assert first.markdown == second.markdown


def test_code_fence_grows_when_content_contains_backticks() -> None:
    """内容里带 ``` 时围栏要加长，否则 Markdown 会被提前闭合（内容被截断）。"""
    root = _doc([])
    root["children"] = ["c"]
    code = _plain("c", "code", "```\n内层围栏\n```")

    markdown = blocks_to_markdown([root, code]).markdown

    assert "````" in markdown


def test_orphan_blocks_are_still_rendered() -> None:
    """防御：父块缺失（只被引用、没有可达根）的块也要渲染出来，不静默丢。"""
    blocks = [
        {"block_id": "p", "children": ["child"], "page": {}},
        # child 的父块存在；再造一个"谁都没引用"的游离块
        _plain("lonely", "text", "游离正文"),
    ]

    markdown = blocks_to_markdown(blocks).markdown

    assert "游离正文" in markdown


def test_empty_input_yields_empty_markdown() -> None:
    """空输入不抛异常（文档可能是空白的）。"""
    result = blocks_to_markdown([])

    assert result.markdown == ""
    assert result.block_count == 0
    assert result.mapped_count == 0
    assert result.skipped == {}
