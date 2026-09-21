"""两级级联切分（tech.md §2 切分行、§5；roadmap R-09）。

① **标题主切**：按 Markdown ATX 标题 ``#`` / ``##`` 切成"节"，保证"块 = 主题"；
   更深层标题（``###`` 及以下）不切分，作为正文留在节内。
② **递归兜底**：节超 :data:`MAX_CHUNK_TOKENS` 时按分隔符优先级
   （``\\n\\n`` → ``\\n`` → ``。`` → 空格）递归切分，相邻子块 overlap ≈ :data:`OVERLAP_CHARS` 字符。

子块继承父节的 ``heading_path`` 并带 :class:`~recall.models.Chunk.sub_index`。
导航类小节（< :data:`MIN_NAV_TOKENS`）**不合并不重切**，整体成块。

确定性（code_standards §0.2）：切分全程是纯函数，不依赖时间、随机数或字典遍历顺序，
同输入必得同输出——这是评测与内容寻址 id 成立的前提。
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

import tiktoken

from recall.models import Chunk

CHUNKER_NAME = "md-heading-v1"
"""切分器版本号：写入 collection metadata 与摄取日志（tech.md §3.1）。"""

MAX_CHUNK_TOKENS = 800
"""单块 token 上限：超过则触发递归兜底切分（tech.md §2 切分行的 ``MAX=800``）。"""

MIN_NAV_TOKENS = 100
"""导航类小节阈值：低于此值的节不合并不重切（tech.md §2 切分行）。"""

OVERLAP_CHARS = 100
"""递归兜底切分时相邻子块的重叠字符数（tech.md §2 切分行 ``overlap ≈ 100 字符``）。"""

SEPARATORS: tuple[str, ...] = ("\n\n", "\n", "。", " ")
"""递归切分的分隔符优先级，从粗到细（tech.md §2）。"""

SPLIT_HEADING_LEVELS = 2
"""标题主切的层级：仅 ``#`` / ``##`` 是切分点。"""

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

_ENCODING_NAME = "cl100k_base"
_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    """惰性获取 tiktoken 编码器（首次调用会下载并缓存 BPE 词表）。"""
    global _encoder  # noqa: PLW0603 - 进程级缓存，避免重复加载词表
    if _encoder is None:
        _encoder = tiktoken.get_encoding(_ENCODING_NAME)
    return _encoder


def count_tokens(text: str) -> int:
    """估算 token 数——预算协商的货币（tech.md §3.3 ``token_count``）。

    使用 ``cl100k_base`` 作为稳定、可离线复现的估算器（切分器不加载 2GB 的
    embedding 模型）；同一文本在任何一次运行中都得到同一个数字。

    Args:
        text: 待计数文本。

    Returns:
        token 估算值（空串为 0）。
    """
    if not text:
        return 0
    return len(_get_encoder().encode(text, disallowed_special=()))


@dataclass(frozen=True, slots=True)
class _Section:
    """标题主切的中间产物：一个"节"及其标题路径。"""

    heading_path: str
    text: str


def chunk_markdown(text: str, *, max_tokens: int = MAX_CHUNK_TOKENS) -> list[Chunk]:
    """两级级联切分主入口。

    Args:
        text: 归一化后的 Markdown 全文（不含 YAML frontmatter）。
        max_tokens: 单块 token 上限，默认 :data:`MAX_CHUNK_TOKENS`。

    Returns:
        按文档顺序排列的 :class:`~recall.models.Chunk` 列表；空文档返回空列表。
    """
    chunks: list[Chunk] = []
    for section in _split_sections(text):
        body = section.text.strip()
        if not body:
            continue
        for sub_index, piece in enumerate(_split_section(body, max_tokens=max_tokens)):
            stripped = piece.strip()
            if not stripped:
                continue
            chunks.append(
                Chunk(
                    text=stripped,
                    heading_path=section.heading_path,
                    sub_index=sub_index,
                    token_count=count_tokens(stripped),
                )
            )
    return chunks


def _split_section(body: str, *, max_tokens: int) -> list[str]:
    """对单个节做第二级判定：整体成块 or 递归兜底切分。"""
    tokens = count_tokens(body)
    if tokens < MIN_NAV_TOKENS:
        # 导航类小节：不合并不重切，整体成块
        return [body]
    if tokens <= max_tokens:
        # 未超限的主题节：标题主切已保证"块 = 主题"，不再二次切分
        return [body]
    return _apply_overlap(_split_oversized(body, max_tokens))


def _split_sections(text: str) -> list[_Section]:
    """① 标题主切：按 ``#`` / ``##`` 把全文切成节，并维护标题路径。"""
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    heading_path = ""
    in_fence = False

    def flush() -> None:
        body = "\n".join(buffer).strip("\n")
        if body.strip():
            sections.append(_Section(heading_path=heading_path, text=body))
        buffer.clear()

    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            buffer.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(line)
        if match is not None:
            level = len(match.group(1))
            title = match.group(2).strip()
            if level <= SPLIT_HEADING_LEVELS and title:
                flush()
                stack = [item for item in stack if item[0] < level]
                stack.append((level, title))
                heading_path = " > ".join(item[1] for item in stack)
                continue
            # 更深层标题：保留为正文，其标题路径已由上层节承载
            buffer.append(line)
            continue
        buffer.append(line)

    flush()
    return sections


def _split_oversized(
    text: str, max_tokens: int, separators: tuple[str, ...] = SEPARATORS
) -> list[str]:
    """② 递归兜底：按分隔符优先级切分超长节。

    优先用最粗的分隔符（段落）；某一片段仍超限时，才降级用更细的分隔符继续切。
    内容零丢失：分隔符保留在前一片段尾部，拼接结果恒等于原文本（overlap 施加前）。

    Args:
        text: 超长节正文。
        max_tokens: 单块 token 上限。
        separators: 剩余可用分隔符（按优先级从粗到细）。

    Returns:
        切分后的片段列表，每片 token 数不超过 ``max_tokens``（除非已无分隔符可用）。
    """
    if count_tokens(text) <= max_tokens or not separators:
        return [text]

    head, rest = separators[0], separators[1:]
    pieces = _split_keep(text, head)
    if len(pieces) <= 1:
        # 该分隔符在本段不存在，直接降级
        return _split_oversized(text, max_tokens, rest)

    expanded: list[str] = []
    for piece in pieces:
        if count_tokens(piece) > max_tokens:
            expanded.extend(_split_oversized(piece, max_tokens, rest))
        else:
            expanded.append(piece)
    return _merge_pieces(expanded, max_tokens)


def _split_keep(text: str, separator: str) -> list[str]:
    """按 ``separator`` 切分，并把分隔符保留在前一片段尾部（内容零丢失）。"""
    if separator not in text:
        return [text]
    pieces: list[str] = []
    start = 0
    while (index := text.find(separator, start)) != -1:
        end = index + len(separator)
        pieces.append(text[start:end])
        start = end
    if start < len(text):
        pieces.append(text[start:])
    return pieces


def _merge_pieces(pieces: list[str], max_tokens: int) -> list[str]:
    """贪心合并相邻小片段，尽量贴近但不超过 ``max_tokens``。"""
    merged: list[str] = []
    buffer = ""
    for piece in pieces:
        candidate = f"{buffer}{piece}"
        if buffer and count_tokens(candidate) > max_tokens:
            merged.append(buffer)
            buffer = piece
        else:
            buffer = candidate
    if buffer:
        merged.append(buffer)
    return merged


def _apply_overlap(pieces: list[str]) -> list[str]:
    """给相邻子块加上前一块尾部的重叠上下文（``overlap ≈ 100 字符``）。"""
    if len(pieces) <= 1:
        return list(pieces)
    result = [pieces[0]]
    for previous, current in itertools.pairwise(pieces):
        tail = _overlap_tail(previous)
        result.append(f"{tail}\n{current}" if tail else current)
    return result


def _overlap_tail(text: str) -> str:
    """取上一块尾部不超过 :data:`OVERLAP_CHARS` 个字符作为重叠上下文。

    若窗口内存在换行，则从首个换行之后起算，避免把半行截断的残句带进下一块。
    """
    tail = text[-OVERLAP_CHARS:]
    newline = tail.find("\n")
    if newline != -1:
        tail = tail[newline + 1 :]
    return tail.strip()
