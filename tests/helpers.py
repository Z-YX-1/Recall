"""测试辅助函数（不放进 conftest，便于显式导入与类型检查）。"""

from __future__ import annotations

from pathlib import Path


def write_note(vault_root: Path, name: str, body: str) -> Path:
    """在 vault 中写入一篇笔记。

    Args:
        vault_root: vault 根目录。
        name: 相对路径（可含子目录）。
        body: Markdown 正文。

    Returns:
        写入的文件路径。
    """
    path = vault_root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path
