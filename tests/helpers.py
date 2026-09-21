"""测试辅助类型与函数（不放进 conftest，便于显式导入与类型检查）。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from ingest import build_parser
from recall.store import QdrantStore


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


@dataclass(slots=True)
class IngestEnv:
    """一次摄取测试的运行环境：临时 vault + 临时 registry + 一次性 collection。"""

    vault: Path
    collection: str
    registry_db: Path
    store: QdrantStore


def ingest_args(
    env: IngestEnv, *, mode: str = "update", extra: list[str] | None = None
) -> argparse.Namespace:
    """构造 ``run_ingest`` 的命令行参数。

    Args:
        env: 摄取环境。
        mode: ``update`` 或 ``rebuild``。
        extra: 追加的原始命令行片段。

    Returns:
        解析完成的 ``argparse.Namespace``。
    """
    argv = [
        f"--{mode}",
        "--collection",
        env.collection,
        "--vault",
        str(env.vault),
        "--log-level",
        "WARNING",
        *(extra or []),
    ]
    return build_parser().parse_args(argv)
