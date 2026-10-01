"""飞书消息卡片的 ``lark_md`` 安全转义适配层（roadmap R-49b）。

**为什么必须有这一层**：飞书卡片的 Markdown 组件只支持标准 Markdown 的**子集**，
且明确要求"命中 markdown 语法的特殊字符"先做 **HTML 实体转义**（官方文档
《消息卡片 > Markdown > 支持的语法》）。而我们要塞进卡片的正文来自 DeepSeek
生成的答案 —— 里面出现 ``*`` ``[`` ``]`` ``#`` ``<`` ``>`` 是常态。裸灌的后果分两类：

1. **渲染错乱**：``#`` 变标题、``---`` 变分割线、成对的 ``*`` 变斜体；
2. **语法吞字**：形如 ``[1](x)`` 的片段被当作链接解析，中括号因此消失。

还有一类**安全**问题：``<at id=all></at>`` 在 lark_md 里是"@所有人"语法
（官方对照表），笔记内容一旦含这个片段，机器人就会替用户 @ 全群 ⇒ 必须转义掉。

**保留什么、牺牲什么**（2026-10-01 决策，见 ``tech.md`` §18 决策 19）：

- **保留**：换行、``**加粗**``（官方子集里最通用的一档行内格式）；
- **不保留**：标题 / 列表 / 代码块 / 引用块 —— 官方文档把它们标注为
  **仅飞书 7.6+ 生效**，低版本客户端会渲染成"升级提示占位图"。宁可显示朴素文本，
  也不要让用户看到占位图，故这些**块级标记一律转义**；
- **链接**：答案正文里的链接**不重建**（一律转义成字面量）；只有**我们自己构造**的
  引用列表才用 :func:`link` 生成可点链接 —— URL 合法性必须由我们把关
  （官方只支持 http/https），不能让模型生成的任意 URL 变成可点目标。

**确定性**（code_standards §0.2）：本模块全部是纯函数，同输入恒同输出，
不依赖时间 / 随机 / 环境；有单测钉住这一点。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

__all__ = [
    "MAX_CARD_BODY_CHARS",
    "REFERENCES_HEADING",
    "TRUNCATION_MARKER",
    "escape_lark_md",
    "link",
    "render_card_body",
    "render_reference_line",
    "to_lark_md",
    "to_lark_md_within",
]

# 行内语法字符 → HTML 实体。取自官方对照表里**只在行内起作用**的那些。
# ``&`` 排第一且必须最先替换，否则会二次转义我们刚写出来的实体。
# 刻意**不含** ``-`` ``/`` ``:`` ``+`` ``.`` ``"`` ``'`` ``$``：它们只在特定位置才构成语法，
# 全量转义会把可读文本（URL、日期、编号）打成一堆实体，得不偿失。
LARK_MD_INLINE_ESCAPES: Final[Mapping[str, str]] = {
    "&": "&amp;",
    "<": "&#60;",
    ">": "&#62;",
    "*": "&#42;",
    "~": "&sim;",
    "`": "&#96;",
    "_": "&#95;",
    "[": "&#91;",
    "]": "&#93;",
    "\\": "&#92;",
}

_LITERAL_BOLD: Final[str] = LARK_MD_INLINE_ESCAPES["*"] * 2
"""落单的 ``**`` 转义后的样子（下文重排加粗时用它还原字面量）。"""

_BLOCK_ESCAPES: Final[Mapping[str, str]] = {
    "#": "&#35;",
    "-": "&#45;",
    "+": "&#43;",
}
_ORDERED_DOT: Final[str] = "&#46;"
_ORDERED_PAREN: Final[str] = "&#41;"

_BLOCK_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>"
    r"-{3,}"
    r"|#{1,6}(?=[ \t]|$)"
    r"|[-+](?=[ \t]|$)"
    r"|(?P<ordinal>\d{1,3})[.)](?=[ \t])"
    r")"
)
"""行首的**块级**标记：分割线 / 标题 / 无序列表 / 有序列表。

``*`` 列表与代码块围栏的起始字符已由行内转义处理；引用块（``>``）同理 ——
它在进入本函数前就已经变成 ``&#62;`` 了，所以这里**不重复列 ``>``**（列了也是死分支）。
"""

_LINKABLE_PREFIXES: Final[tuple[str, ...]] = ("http://", "https://")
"""官方限制："超链接必须包含 schema 才能生效，目前仅支持 HTTP 和 HTTPS"。

``source_uri`` 在 Obsidian 来源下是 vault 相对路径（如 ``project/Recall/spec/tech.md``），
不是 URL ⇒ 会走 :func:`link` 的降级分支，渲染成纯文本而不是一个点不开的链接。
"""

REFERENCES_HEADING: Final[str] = "**来源**"
"""引用列表的小标题（加粗，官方子集里最通用的一档）。"""

TRUNCATION_MARKER: Final[str] = "…（已截断）"
"""超预算时的截断标记。**刻意不含任何 markdown 特殊字符**，因此它本身无需转义。"""

MAX_CARD_BODY_CHARS: Final[int] = 1800
"""卡片正文的保守字符预算。

⚠️ **官方未给出卡片 Markdown 内容的上限数值**（R-49 的二轮核查未能取得权威数字，
只知道"长内容需要拆分"是社区反复踩的坑）。所以这里取一个**刻意保守**的值：
答案正文通常在几百字内，1800 字符足够容纳，同时让整张卡片的 JSON 远低于任何
合理上限。它是**可配置参数**（``Settings`` 侧留给 R-49d），不是硬契约。
"""

_EMPTY_ANSWER: Final[str] = "（未生成回答正文）"
_MISSING_SOURCE: Final[str] = "（来源缺失）"


def escape_lark_md(text: str) -> str:
    """把整段文本转义为**纯字面量** lark_md（不保留任何 Markdown 语法）。

    Args:
        text: 任意文本（可含换行）。

    Returns:
        转义后的文本；换行统一为 ``\\n``，其余字符要么原样、要么变成 HTML 实体。

    Example:
        >>> escape_lark_md("看 *这个* [1]")
        '看 &#42;这个&#42; &#91;1&#93;'
    """
    normalized = _normalize_newlines(text)
    # 逐字符映射：因为是在**原串**上遍历，写出的实体不会被再次扫描 ⇒ 无二次转义。
    escaped = "".join(LARK_MD_INLINE_ESCAPES.get(char, char) for char in normalized)
    return _escape_block_markers(escaped)


def to_lark_md(text: str) -> str:
    """转义为 lark_md，但**保留 ``**加粗**``**（官方子集里最通用的一档行内格式）。

    配对规则刻意简单且确定：按 ``**`` 切分，偶数段转义、奇数段套上加粗。
    ``**`` 出现**奇数次**（即最后一个 ``**`` 落单）时，最后一段按**字面量**处理 ——
    与其猜作者意图，不如稳定地少做一件事。

    Args:
        text: 任意文本（可含换行）。

    Returns:
        转义后的 lark_md 文本。

    Example:
        >>> to_lark_md("**重点**：看 *这个*")
        '**重点**：看 &#42;这个&#42;'
    """
    segments = _normalize_newlines(text).split("**")
    unpaired = len(segments) - 1 if len(segments) % 2 == 0 else None
    parts: list[str] = []
    for index, segment in enumerate(segments):
        escaped = escape_lark_md(segment)
        if index == unpaired:
            parts.append(f"{_LITERAL_BOLD}{escaped}")
        elif index % 2 == 1:
            parts.append(f"**{escaped}**")
        else:
            parts.append(escaped)
    return "".join(parts)


def to_lark_md_within(text: str, limit: int, *, marker: str = TRUNCATION_MARKER) -> str:
    """转义并把**渲染后**的长度限制在 ``limit`` 字符以内。

    **为什么要在原始文本上截断**：转义会把一个字符膨胀成最多 5 个字符
    （``&`` → ``&amp;``），在**转义后**的串上做切片会切进实体内部
    （``&#4``）⇒ 渲染出垃圾。所以这里始终在**原始字符边界**上收缩，再整段转义。

    Args:
        text: 任意文本。
        limit: 渲染后允许的最大字符数；``<= 0`` 返回空串。
        marker: 截断标记；预算不足以放下它时，标记自身也会被裁剪。

    Returns:
        长度不超过 ``limit`` 的 lark_md 文本。

    Note:
        循环用"按超出量收缩"而不是二分：``to_lark_md`` 的长度对输入长度**并非单调**
        （``**x`` 落单时是 10 个字符，补成 ``**x**`` 后反而只剩 5 个），二分的前提不成立。
        按超出量收缩保证每轮**至少砍 1 个字符** ⇒ 必然终止。
    """
    if limit <= 0:
        return ""
    normalized = _normalize_newlines(text)
    full = to_lark_md(normalized)
    if len(full) <= limit:
        return full
    if limit <= len(marker):
        return marker[:limit]

    room = limit - len(marker)
    cut = len(normalized)
    while cut > 0:
        candidate = to_lark_md(normalized[:cut])
        if len(candidate) <= room:
            break
        cut = max(0, cut - max(1, len(candidate) - room))
    return to_lark_md(normalized[:cut]) + marker


def link(label: str, url: str) -> str:
    """生成 ``[label](url)``；URL 不可用时**降级为纯文本**。

    降级条件（任一命中即降级）：不是 http/https、含空白、含 ``)``
    （会截断 Markdown 链接语法）或为空。降级后渲染成 ``label（url）``，
    用全角括号以免看起来像链接。

    Args:
        label: 链接文字。
        url: 目标 URL。

    Returns:
        lark_md 片段。

    Example:
        >>> link("文档", "https://example.com/a")
        '[文档](https://example.com/a)'
        >>> link("笔记", "project/Recall/spec/tech.md")
        '笔记（project/Recall/spec/tech.md）'
    """
    escaped_label = to_lark_md(label)
    candidate = url.strip()
    if _is_linkable_url(candidate):
        return f"[{escaped_label}]({candidate})"
    return f"{escaped_label}（{escape_lark_md(candidate)}）"


def render_reference_line(index: int, source_uri: str) -> str:
    """渲染一行引用：``[n] 来源``，且 ``[n]`` 与 ``references`` 一一对应（code_standards §8）。

    编号两侧的中括号**也要转义**：裸写 ``[1]`` 会让后续的 ``[label](url)`` 有被解析成
    引用式链接的风险。

    ⚠️ 来源是 URL 时走 :func:`link`；是 vault 相对路径时**只渲染路径本身**，
    **不能**走 ``link(source, source)`` —— 那会降级成 ``a.md（a.md）`` 这种自我重复。

    Args:
        index: 从 1 开始的引用编号。
        source_uri: 来源标识（URL 或 vault 相对路径）。

    Returns:
        lark_md 片段。

    Example:
        >>> render_reference_line(1, "https://example.com/a")
        '&#91;1&#93; [https://example.com/a](https://example.com/a)'
        >>> render_reference_line(2, "project/Recall/spec/tech.md")
        '&#91;2&#93; project/Recall/spec/tech.md'
    """
    marker = escape_lark_md(f"[{index}]")
    source = source_uri.strip()
    if not source:
        return f"{marker} {_MISSING_SOURCE}"
    if _is_linkable_url(source):
        return f"{marker} {link(source, source)}"
    return f"{marker} {escape_lark_md(source)}"


def render_card_body(
    answer: str,
    references: Sequence[Mapping[str, str]],
    *,
    max_chars: int = MAX_CARD_BODY_CHARS,
) -> str:
    """把答案正文与引用列表拼成卡片可用的 lark_md 文本。

    结构：``<截断后的答案>`` + 空行 + ``**来源**`` + 每行一条引用。
    **只有答案正文参与截断**，引用列表完整保留 —— 引用是"可核对"的落点，
    截掉它就等于把答案变成不可验证的断言。

    Args:
        answer: 答案正文（可为空）。
        references: ``[{ref_id, source_uri}, ...]``，与 ``[n]`` 一一对应。
        max_chars: 答案正文的渲染字符预算。

    Returns:
        卡片 ``content`` 用的 lark_md 文本；引用为空时只返回正文部分。

    Example:
        >>> render_card_body("切分粒度决定检索质量 [1]。", [{"source_uri": "a.md"}])
        '切分粒度决定检索质量 &#91;1&#93;。\\n\\n**来源**\\n&#91;1&#93; a.md'
    """
    body = to_lark_md_within(answer.strip(), max_chars)
    if not body:
        body = _EMPTY_ANSWER
    lines = [
        render_reference_line(index, str(reference.get("source_uri") or ""))
        for index, reference in enumerate(references, start=1)
    ]
    if not lines:
        return body
    # ⚠️ 引用之间用**单换行**（连续成行），只有正文与引用块之间才空一行。
    # 早期版本用 "\n\n".join([body, heading, *lines])，结果每条引用各自成段、中间多出空行。
    block = "\n".join([REFERENCES_HEADING, *lines])
    return f"{body}\n\n{block}"


def _normalize_newlines(text: str) -> str:
    """统一换行为 ``\\n``（保证跨平台确定性）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _escape_block_markers(text: str) -> str:
    """逐行转义行首的块级标记（标题 / 列表 / 引用）。"""
    return "\n".join(_BLOCK_MARKER_RE.sub(_replace_block_marker, line) for line in text.split("\n"))


def _replace_block_marker(match: re.Match[str]) -> str:
    """把行首标记的第一个字符换成实体；有序列表则转义分隔符以保住 ``1.`` 的可读性。"""
    indent = match.group("indent")
    marker = match.group("marker")
    ordinal = match.group("ordinal")
    if ordinal is not None:
        delimiter = _ORDERED_DOT if marker.endswith(".") else _ORDERED_PAREN
        return f"{indent}{ordinal}{delimiter}"
    return f"{indent}{_BLOCK_ESCAPES[marker[0]]}{marker[1:]}"


def _is_linkable_url(url: str) -> bool:
    """URL 是否可以直接放进 ``[label](url)``。"""
    if not url.startswith(_LINKABLE_PREFIXES):
        return False
    return not any(char.isspace() or char == ")" for char in url)
