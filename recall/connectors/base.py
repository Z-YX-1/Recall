"""Connector 协议与共享实现（code_standards §4.1；roadmap R-12）。

新增来源（飞书 / 语雀 / 网页）只实现 :class:`Connector` 协议，不改管道其余部分
（tech.md §5）。协议本身只有两个方法：:meth:`Connector.list` / :meth:`Connector.hash_of`；
错误缓冲等共享能力由 :class:`BaseConnector` 提供，不算协议成员。
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from recall.models import RawDoc

logger = logging.getLogger(__name__)

_BLANK_RUN_RE = re.compile(r"\n{3,}")
_SPACE_RUN_RE = re.compile(r"[\s_]+")
_NON_WORD_RE = re.compile(r"[^\w-]+", re.UNICODE)
_DASH_RUN_RE = re.compile(r"-{2,}")


@dataclass(frozen=True, slots=True)
class SourceError:
    """单个来源文档的处理失败记录（错误隔离，code_standards §4.1）。"""

    doc_id: str
    source_uri: str
    message: str


class Connector(Protocol):
    """摄取来源协议（tech.md §2 摄取行的 Connector 接口）。"""

    source_type: str

    def list(self) -> Iterator[RawDoc]:
        """全量枚举来源文档（含 ``text``）。"""
        ...

    def hash_of(self, doc: RawDoc) -> str:
        """归一化全文 sha256。"""
        ...


class BaseConnector:
    """Connector 的共享实现：错误缓冲 + 默认 :meth:`hash_of`。

    子类实现 :meth:`list`，遇到单文档失败时调用 :meth:`_record_error` 后继续，
    由摄取管道在 run 结束时 :meth:`drain_errors` 汇总写入 registry 的 ``error`` 字段。
    """

    source_type: str = "base"

    def __init__(self) -> None:
        self._errors: list[SourceError] = []

    def hash_of(self, doc: RawDoc) -> str:
        """默认实现：归一化全文的 sha256（tech.md §3.2 ``content_hash``）。"""
        return document_content_hash(doc.text)

    def drain_errors(self) -> list[SourceError]:
        """取出并清空错误缓冲（摄取 run 结束时调用）。"""
        errors, self._errors = self._errors, []
        return errors

    def _record_error(self, doc_id: str, source_uri: str, message: str) -> None:
        self._errors.append(SourceError(doc_id=doc_id, source_uri=source_uri, message=message))
        logger.warning(
            "connector.doc_error",
            extra={
                "source_type": self.source_type,
                "doc_id": doc_id,
                "source_uri": source_uri,
                "error": message[:500],
            },
        )


def normalize_text(text: str) -> str:
    """归一化全文——内容寻址与切分确定性的共同前提。

    处理：剥 BOM、``CRLF``/``CR`` → ``LF``、清除行尾空白、连续空行压缩为一段空行、
    去掉首尾空行。**幂等**（``normalize(normalize(x)) == normalize(x)``），
    因此 ``sha256(normalize(text))`` 在任意次运行中一致。

    Args:
        text: 原始文本。

    Returns:
        归一化文本。
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip("\n")


def document_content_hash(text: str) -> str:
    """文档级内容哈希：``sha256(归一化全文)``（tech.md §3.2）。"""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def slugify(name: str) -> str:
    """文件名 → 稳定 kebab-case ``doc_id``（code_standards §2）。

    ``NFKC`` 归一化后转小写，把空白/下划线折叠成连字符，剔除标点；ASCII 字母数字与
    中文等 Unicode 词字符原样保留（不引入拼音依赖，见 roadmap §七 变更日志）。
    结果为空时回落到基于名字的短哈希，保证 ``doc_id`` 恒非空且稳定。

    Args:
        name: 通常是文件名词干。

    Returns:
        kebab-case slug。
    """
    normalized = unicodedata.normalize("NFKC", name).strip().lower()
    slug = _SPACE_RUN_RE.sub("-", normalized)
    slug = _NON_WORD_RE.sub("", slug)
    slug = _DASH_RUN_RE.sub("-", slug).strip("-")
    if not slug:
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        return f"doc-{digest}"
    return slug
