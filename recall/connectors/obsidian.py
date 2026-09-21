"""Obsidian vault Connector（tech.md §2 摄取行 v1；roadmap R-12）。

扫描 vault 内全部 ``.md`` → 抽取 YAML frontmatter → :class:`~recall.models.RawDoc`。
**错误隔离**：单文档读取/解析失败只记 :class:`~recall.connectors.base.SourceError`
并继续，绝不中断整个 run（code_standards §4.1）。

``doc_id`` 由文件名派生（code_standards §2）；同名文件跨目录冲突时，所有冲突项统一
追加来源路径短哈希消歧——与枚举顺序无关，保证确定性。

注：协议方法名固定为 ``list``，会在类体内遮蔽内建 ``list``，因此文件枚举与规划逻辑
放在模块级函数中（``_iter_markdown_paths`` / ``_plan``）。
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter

from recall.connectors.base import BaseConnector, normalize_text, slugify
from recall.models import RawDoc

logger = logging.getLogger(__name__)

SOURCE_TYPE = "obsidian"
MARKDOWN_SUFFIX = ".md"

DEFAULT_SKIP_DIRS: tuple[str, ...] = (
    # Obsidian / VCS 内部目录
    ".obsidian",
    ".trash",
    ".git",
    ".smart-env",
    ".stfolder",
    # 工程产物：真实 vault 里混进 node_modules/dist 会把第三方 CHANGELOG/LICENSE
    # 灌进索引、把检索结果冲垮（2026-09-22 实测：268 篇里大半是 node_modules）
    "node_modules",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
)
"""默认跳过的目录名（可按需用 ``--skip-dirs`` 覆盖）。"""


class ObsidianConnector(BaseConnector):
    """把 Obsidian vault 目录当作摄取来源。

    Attributes:
        source_type: 固定为 ``obsidian``，写入 registry 的 ``source_type``。
    """

    source_type = SOURCE_TYPE

    def __init__(self, vault_path: Path, *, skip_dirs: Iterable[str] | None = None) -> None:
        """初始化 Connector。

        Args:
            vault_path: vault 根目录（不存在时 :meth:`list` 返回空并记一条错误）。
            skip_dirs: 额外跳过的目录名集合；``None`` 用 :data:`DEFAULT_SKIP_DIRS`。
        """
        super().__init__()
        self._vault = vault_path
        self._skip_dirs = frozenset(DEFAULT_SKIP_DIRS if skip_dirs is None else skip_dirs)

    @property
    def vault_path(self) -> Path:
        """vault 根目录。"""
        return self._vault

    @property
    def skip_dirs(self) -> frozenset[str]:
        """被跳过的目录名集合。"""
        return self._skip_dirs

    def list(self) -> Iterator[RawDoc]:
        """枚举 vault 内全部 Markdown 文档（按来源相对路径升序，确定性）。

        Yields:
            解析成功的 :class:`~recall.models.RawDoc`；失败文档记录错误后跳过。
        """
        if not self._vault.is_dir():
            self._record_error("", str(self._vault), f"vault 目录不存在或不是目录: {self._vault}")
            return
        for path, doc_id in _plan(self._vault, self._skip_dirs):
            source_uri = path.relative_to(self._vault).as_posix()
            try:
                raw = path.read_text(encoding="utf-8", errors="strict")
                metadata, body = _parse_markdown(raw)
            except Exception as exc:  # noqa: BLE001 - 单文档失败必须隔离并继续
                self._record_error(doc_id, source_uri, f"{type(exc).__name__}: {exc}")
                continue
            yield RawDoc(
                doc_id=doc_id,
                source_uri=source_uri,
                text=normalize_text(body),
                frontmatter=metadata,
                updated_at=_mtime_iso(path),
            )


def _plan(vault: Path, skip_dirs: frozenset[str]) -> list[tuple[Path, str]]:
    """先规划 ``(路径, doc_id)`` 再读取——同名冲突可在此确定性消歧。"""
    paths = _iter_markdown_paths(vault, skip_dirs)
    slugs = {path: slugify(path.stem) for path in paths}
    counts = Counter(slugs.values())
    plan: list[tuple[Path, str]] = []
    for path in paths:
        slug = slugs[path]
        if counts[slug] > 1:
            relative = path.relative_to(vault).as_posix()
            digest = hashlib.sha1(relative.encode("utf-8")).hexdigest()[:8]
            slug = f"{slug}-{digest}"
        plan.append((path, slug))
    return plan


def _iter_markdown_paths(vault: Path, skip_dirs: frozenset[str]) -> list[Path]:
    """列出待摄取的 Markdown 文件（跳过隐藏目录与 :data:`DEFAULT_SKIP_DIRS`）。"""
    paths: list[Path] = []
    for path in vault.rglob(f"*{MARKDOWN_SUFFIX}"):
        if not path.is_file() or path.name.startswith("."):
            continue
        relative_parts = path.relative_to(vault).parts[:-1]
        if any(part in skip_dirs or part.startswith(".") for part in relative_parts):
            continue
        paths.append(path)
    return sorted(paths)


def _parse_markdown(raw: str) -> tuple[dict[str, Any], str]:
    """解析 frontmatter，返回 ``(metadata, 正文)``。

    Args:
        raw: 文件原文。

    Returns:
        元数据字典与去掉 frontmatter 的正文（尚未归一化）。

    Raises:
        Exception: YAML 非法时由 :mod:`frontmatter` 抛出，由调用方隔离为单文档错误。
    """
    post = frontmatter.loads(raw)
    return dict(post.metadata), post.content


def _mtime_iso(path: Path) -> str:
    """文件 mtime → 本地时区 ISO 8601 字符串（tech.md §3.2 ``updated_at``）。"""
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
