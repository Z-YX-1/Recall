"""飞书云文档 blocks → Markdown（纯函数、无 I/O；roadmap R-41d）。

## 为什么必须有这一层

我们的切分器是 **`md-heading-v1`**（按 `#`/`##` 标题切，见 tech.md §4/§5）。
飞书的 `raw_content` 只有**纯文本**、没有结构；而 `blocks` 才是结构化原文。
只拿纯文本 ⇒ 整篇会退化成"按长度盲切" ⇒ 检索质量显著下降（结构感知分块是召回质量的关键，
见 `eval/BASELINE.md`）。而官方**导出任务不支持 Markdown**（`file_type` 仅 docx/pdf/xlsx/csv，
2026-09-30 Context7 核实）⇒ 这层转换**必做、没有捷径**。

## 设计要点（对应 roadmap §四 R-41 复查表）

1. **按"哪个内容字段非空"判型**，不硬编码 `block_type` 数字枚举 —— 枚举会漂移，而内容实体是
   命名字段（`page`/`text`/`heading1..9`/`bullet`/`ordered`/`code`/`quote`/`todo`/`callout`/
   `divider`/`table`/`table_cell`/`image`/…，共 40+ 种）。
2. **不静默丢**：不认识/不映射的块类型**计数**并随结果返回（调用方写日志），不是无声跳过。
3. **确定性**：同一份 blocks 两次转换必须**逐字节一致** —— 幂等依赖 `content_hash`
   （`sha256(归一化正文)`），转换只要有一点不确定，每次 run 都会重灌。
4. **层级**：`blocks` 返回的是**扁平列表 + `children` 引用**（不是嵌套树），本模块按引用重建树，
   用 `parent_id`/`children` 得到前序序列；列表项按树深度缩进。
5. **字段名兼容**：文档里文本字段既有 `text_run.content`（单数）又有 `text_runs[].text`（复数）
   —— 两种都支持（复查表第 ⑩ 条）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

Block = Mapping[str, Any]

_HEADING_FIELDS: dict[str, int] = {f"heading{level}": level for level in range(1, 10)}

_CONTAINER_FIELDS: tuple[str, ...] = (
    "page",
    "callout",
    "quote_container",
    "grid",
    "grid_column",
    "table_cell",
)

_SIMPLE_TEXT_FIELDS: tuple[str, ...] = ("text", "bullet", "ordered", "code", "quote", "todo")

#: 只用于**识别类型**（不求全）：先命中的字段名即该块的类型。
_KNOWN_FIELDS: tuple[str, ...] = (
    "page",
    *_HEADING_FIELDS,
    "text",
    "bullet",
    "ordered",
    "code",
    "quote",
    "todo",
    "callout",
    "quote_container",
    "divider",
    "grid",
    "grid_column",
    "table",
    "table_cell",
    "image",
    "file",
    "sheet",
    "bitable",
    "mindnote",
    "iframe",
    "board",
    "view",
    "task",
    "okr",
    "okr_objective",
    "okr_key_result",
    "okr_progress",
    "jira_issue",
    "link_preview",
    "sub_page_list",
    "wiki_catalog",
    "reference_synced",
    "source_synced",
    "ai_template",
    "add_ons",
    "isv",
    "chat_card",
    "diagram",
    "agenda",
    "agenda_item",
    "agenda_item_title",
    "agenda_item_content",
    "undefined",
)

LIST_INDENT = "  "
"""列表项每层缩进（Markdown 用两个空格是安全的）。"""


@dataclass(frozen=True, slots=True)
class MarkdownResult:
    """一次转换的结果。

    Attributes:
        markdown: 生成的 Markdown 正文。
        block_count: 遍历到的块总数。
        mapped_count: 真正**渲染出内容**的块数（容器块不计）。
        skipped: 未映射的块类型 → 出现次数（**调用方应记日志**，避免"悄悄少内容"）。
    """

    markdown: str
    block_count: int
    mapped_count: int
    skipped: dict[str, int] = field(default_factory=dict)


def _block_kind(block: Block) -> str:
    """判断块的类型：**按内容字段是否非空**（见模块 docstring 第 1 条）。

    Args:
        block: 单个飞书块。

    Returns:
        命名字段名（如 ``"heading1"``/``"text"``）；都不命中时回落到 ``"type:<block_type>"``。
    """
    for name in _KNOWN_FIELDS:
        if block.get(name) is not None:
            return name
    raw = block.get("block_type")
    return f"type:{raw}" if raw is not None else "unknown"


def _run_text(run: Mapping[str, Any]) -> str:
    """把单个 text run 渲染成 Markdown 片段。

    只保留**对检索有意义**的两处标记：行内代码与链接；加粗/斜体/删除线一律**扁平化**
    （它们不改变语义，却会让同一段话在不同编辑风格下 hash 不同 ⇒ 平白触发重灌）。

    Args:
        run: ``text_run`` / ``text_runs[]`` 里的一项。

    Returns:
        Markdown 片段（空串表示这一段没有可渲染内容）。
    """
    text = run.get("content")
    if not isinstance(text, str):
        alt = run.get("text")
        text = alt if isinstance(alt, str) else ""
    if not text:
        return ""
    style = run.get("text_style")
    if isinstance(style, Mapping):
        link = style.get("link")
        url = link.get("url") if isinstance(link, Mapping) else None
        if isinstance(url, str) and url:
            return f"[{text}]({url})"
        if style.get("inline_code") and "`" not in text:
            return f"`{text}`"
    return text


def _elements_text(payload: Mapping[str, Any]) -> str:
    """把块内容里的 ``elements`` 拼成文本（兼容两种字段名，见模块 docstring 第 5 条）。"""
    elements = payload.get("elements")
    if not isinstance(elements, Sequence) or isinstance(elements, (str, bytes)):
        return ""
    parts: list[str] = []
    for element in elements:
        if not isinstance(element, Mapping):
            continue
        single = element.get("text_run")
        if isinstance(single, Mapping):
            parts.append(_run_text(single))
            continue
        multi = element.get("text_runs")
        if isinstance(multi, Sequence) and not isinstance(multi, (str, bytes)):
            parts.extend(_run_text(run) for run in multi if isinstance(run, Mapping))
            continue
        # @文档 / @人 / 公式等：尽力保留可读信息，取不到就跳过
        mentioned = element.get("mention_doc")
        if isinstance(mentioned, Mapping):
            title = mentioned.get("title") or mentioned.get("token")
            if isinstance(title, str) and title:
                parts.append(title)
            continue
        user = element.get("mention_user")
        if isinstance(user, Mapping):
            uid = user.get("user_id") or user.get("open_id")
            if isinstance(uid, str) and uid:
                parts.append(f"@{uid}")
            continue
        equation = element.get("equation")
        if isinstance(equation, Mapping):
            content = equation.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
    return "".join(parts).strip()


def _block_text(block: Block) -> str:
    """取块自身的文本（标题/正文/列表项/代码/引用/待办都用它）。

    ⚠️ **两种结构都认**（复查表第 ⑩ 条，实测踩过）：官方示例里 ``elements`` 有时挂在
    **块顶层**（``{"block_type": 1, "elements": [...]}``），有时嵌在**内容字段里**
    （``{"text": {"elements": [...]}}``）。先看内容字段，取不到再回落到顶层 ——
    不这么写，真实数据一旦是另一种形状，**整篇会转成空字符串**（而空正文的 hash 稳定 ⇒
    库里会留下"空白文档"，静默且难查）。
    """
    kind = _block_kind(block)
    payload = block.get(kind)
    if isinstance(payload, Mapping):
        text = _elements_text(payload)
        if text:
            return text
    return _elements_text(block)


def _ordered_tree(blocks: Sequence[Block]) -> list[tuple[Block, int]]:
    """把扁平 blocks 按 ``children`` 引用重建为**前序**序列（块, 深度）。

    ``blocks`` 接口返回的是扁平列表 + ``children`` 引用（不是嵌套树）。这里用迭代式前序遍历
    （不用递归：避免深文档撞递归上限），并对**循环引用/缺失父块**做防御 —— 仍然会遍历到
    每一个块（只是层级可能不准），绝不静默丢内容。

    Args:
        blocks: 扁平块列表。

    Returns:
        ``[(块, 深度), …]``，顺序确定（取决于输入顺序）。
    """
    by_id: dict[str, Block] = {}
    for block in blocks:
        bid = block.get("block_id")
        if isinstance(bid, str) and bid:
            by_id[bid] = block
    child_ids: dict[str, list[str]] = {}
    for bid, block in by_id.items():
        kids = block.get("children")
        if isinstance(kids, Sequence) and not isinstance(kids, (str, bytes)):
            child_ids[bid] = [k for k in kids if isinstance(k, str) and k in by_id]

    referenced = {kid for kids in child_ids.values() for kid in kids}
    roots = [bid for bid in by_id if bid not in referenced]

    ordered: list[tuple[Block, int]] = []
    visited: set[str] = set()
    stack: list[tuple[str, int]] = [(bid, 0) for bid in reversed(roots)]
    while stack:
        bid, depth = stack.pop()
        if bid in visited:
            continue
        visited.add(bid)
        ordered.append((by_id[bid], depth))
        for kid in reversed(child_ids.get(bid, [])):
            if kid not in visited:
                stack.append((kid, depth + 1))
    # 防御：只被其它块引用、但没有"根"能到达的孤儿子树也补上
    for bid in by_id:
        if bid not in visited:
            stack.append((bid, 0))
            while stack:
                bid2, depth2 = stack.pop()
                if bid2 in visited:
                    continue
                visited.add(bid2)
                ordered.append((by_id[bid2], depth2))
                for kid in reversed(child_ids.get(bid2, [])):
                    if kid not in visited:
                        stack.append((kid, depth2 + 1))
    return ordered


def _subtree_text(
    block_id: str, child_ids: Mapping[str, list[str]], by_id: Mapping[str, Block]
) -> str:
    """取某个块**及其全部后代**的文本（表格单元格用；单元格内容是子块）。"""
    parts: list[str] = []
    stack = [block_id]
    seen: set[str] = set()
    while stack:
        bid = stack.pop()
        if bid in seen:
            continue
        seen.add(bid)
        block = by_id.get(bid)
        if block is None:
            continue
        text = _block_text(block)
        if text:
            parts.append(text)
        for kid in reversed(child_ids.get(bid, [])):
            stack.append(kid)
    return " ".join(parts).strip()


def _fence(text: str) -> str:
    """给代码块选一个**不会被内容提前闭合**的围栏（内容里有 ``` 时加长）。"""
    fence = "```"
    while fence in text:
        fence += "`"
    return fence


def _render_table(
    block: Block,
    *,
    child_ids: Mapping[str, list[str]],
    by_id: Mapping[str, Block],
) -> str:
    """渲染表格块为 Markdown 表格。

    单元格来自 ``table`` 的 ``children``（``table_cell`` 块，按行优先排列）；
    行列数取 ``table.property.row_size`` / ``column_size``。合并单元格（``merge_info``）
    **不处理**，按下标平均填充；结构对不上时退化为"每格一段文本"的单行表示，避免丢内容。

    Args:
        block: ``table`` 块。
        child_ids: ``block_id → 子块 id`` 映射。
        by_id: ``block_id → 块`` 映射。

    Returns:
        Markdown 表格（末尾含空行）；无内容时返回空串。
    """
    table_id = block.get("block_id")
    if not isinstance(table_id, str):
        return ""
    cells = [
        _subtree_text(cell_id, child_ids, by_id) for cell_id in child_ids.get(table_id, [])
    ]
    payload = block.get("table")
    prop = payload.get("property") if isinstance(payload, Mapping) else None
    rows = prop.get("row_size") if isinstance(prop, Mapping) else None
    cols = prop.get("column_size") if isinstance(prop, Mapping) else None
    if not isinstance(rows, int) or not isinstance(cols, int) or rows <= 0 or cols <= 0:
        rows, cols = (1, max(len(cells), 1))
    width = rows * cols
    padded = (cells + [""] * width)[:width]
    if not any(padded):
        return ""
    lines: list[str] = []
    header = padded[:cols]
    lines.append("| " + " | ".join(cell or " " for cell in header) + " |")
    lines.append("| " + " | ".join("---" for _ in range(cols)) + " |")
    for index in range(1, rows):
        row = padded[index * cols : (index + 1) * cols]
        lines.append("| " + " | ".join(cell or " " for cell in row) + " |")
    return "\n".join(lines) + "\n\n"


def blocks_to_markdown(blocks: Sequence[Block], *, title: str | None = None) -> MarkdownResult:
    """把飞书云文档的 blocks 转成 Markdown（**纯函数**、可复现）。

    Args:
        blocks: 扁平块列表（``GET /docx/v1/documents/{id}/blocks`` 或
            ``…/blocks/{id}/children?with_descendants=true`` 的 ``items``）。
        title: 文档标题。给了就先输出 ``# {title}`` —— 我们精排要吃 `heading_path`，
            而**块里没有文档标题**（它在文档元信息/wiki 节点的 ``title`` 里）；
            少了这一级会让检索丢掉最强的话题信号（见 tech.md §4 的 R-19b 实测）。

    Returns:
        :class:`MarkdownResult`：Markdown + 块计数 + 未映射类型计数。
    """
    ordered = _ordered_tree(blocks)
    # ⚠️ 必须**两遍**：先收齐所有块，再建"父 → 子"映射。边遍历边建索引会让
    #    先处理的块看不到自己的子块（子块在索引里还不存在）⇒ 表格单元格会被整体丢掉。
    by_id: dict[str, Block] = {}
    for block, _depth in ordered:
        bid = block.get("block_id")
        if isinstance(bid, str):
            by_id[bid] = block
    child_ids: dict[str, list[str]] = {}
    for bid, block in by_id.items():
        kids = block.get("children")
        if isinstance(kids, Sequence) and not isinstance(kids, (str, bytes)):
            child_ids[bid] = [k for k in kids if isinstance(k, str) and k in by_id]

    lines: list[str] = []
    if title:
        lines.append(f"# {title.strip()}")
        lines.append("")

    skipped: dict[str, int] = {}
    mapped = 0
    for block, depth in ordered:
        kind = _block_kind(block)

        if kind in _CONTAINER_FIELDS or kind == "table":
            if kind == "table":
                rendered = _render_table(block, child_ids=child_ids, by_id=by_id)
                if rendered:
                    lines.append(rendered.rstrip("\n"))
                    lines.append("")
                    mapped += 1
            continue  # 容器块本身不出内容，子块由遍历负责

        if kind == "divider":
            lines.extend(["---", ""])
            mapped += 1
            continue

        heading_level = _HEADING_FIELDS.get(kind)
        if heading_level is not None:
            text = _block_text(block)
            if text:
                lines.extend([f"{'#' * heading_level} {text}", ""])
                mapped += 1
            continue

        if kind in _SIMPLE_TEXT_FIELDS:
            text = _block_text(block)
            if not text:
                continue
            level = max(depth - 1, 0)
            indent = LIST_INDENT * level
            if kind == "bullet":
                lines.append(f"{indent}- {text}")
            elif kind == "ordered":
                # 统一用 "1."：Markdown 渲染时自动编号，且避免为计数引入状态（保确定性）
                lines.append(f"{indent}1. {text}")
            elif kind == "todo":
                payload = block.get("todo")
                style = payload.get("style") if isinstance(payload, Mapping) else None
                done = bool(style.get("done")) if isinstance(style, Mapping) else False
                lines.append(f"{indent}- [{'x' if done else ' '}] {text}")
            elif kind == "code":
                fence = _fence(text)
                lines.extend([f"{fence}", text, f"{fence}", ""])
            elif kind == "quote":
                quoted = "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))
                lines.extend([quoted, ""])
            else:  # text
                lines.extend([text, ""])
            mapped += 1
            continue

        skipped[kind] = skipped.get(kind, 0) + 1

    markdown = "\n".join(lines).strip("\n")
    return MarkdownResult(
        markdown=markdown,
        block_count=len(ordered),
        mapped_count=mapped,
        skipped={key: skipped[key] for key in sorted(skipped)},
    )
