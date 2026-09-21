"""Connector 实现集合：v1 只有 Obsidian（tech.md §2 摄取行）。"""

from __future__ import annotations

from recall.connectors.base import BaseConnector, Connector, SourceError, slugify

__all__ = ["BaseConnector", "Connector", "SourceError", "slugify"]
