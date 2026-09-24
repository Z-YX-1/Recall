"""Recall（拾忆）—— 个人 RAG 知识库。

本地优先、隐私安全的知识库服务：Obsidian 摄取 → bge-m3 混合检索 → 组装证据包。
规范文档见 ``spec/tech.md``（做什么、为什么）与 ``spec/code_standards.md``（怎么写）。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"


def _bootstrap_environment() -> None:
    """在**任何** HuggingFace 相关 import 之前把环境变量设好（tech.md §12）。

    ⚠️ 顺序是硬要求，不是风格问题：``huggingface_hub`` 在 **import 时**就把
    ``HF_HUB_OFFLINE`` / ``HF_ENDPOINT`` 读成模块常量，之后无论怎么改环境变量都不再
    生效。而 ``recall.api`` / ``recall.embedder`` 一被导入就会连带导入
    transformers → huggingface_hub；若等到 ``Service.create()`` 里才
    ``Settings.from_env()``，**离线开关已经晚了一步**（2026-09-24 实测：
    ``kb_search`` 仍因 ``transformers…list_repo_templates`` 回连 Hub 而 42s 超时）。

    放在包根 ``__init__`` 里，"先建配置、后加载模型"这件事就由**导入顺序**保证，
    而不是靠每个调用方自觉。
    """
    from recall.config import Settings  # 延迟导入：此刻包只初始化到一半

    Settings.from_env()


_bootstrap_environment()
