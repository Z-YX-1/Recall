"""运行配置：环境变量 / ``.env`` 单一入口（code_standards §12）。

密钥与路径**一律不硬编码**在代码里；``.env`` 已被 ``.gitignore`` 忽略。
模型名、切分器名等"规范注入项"留在各自模块的常量中，此处只负责环境与路径。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
"""项目根目录（``ingest.py`` / ``spec/`` 所在目录）。"""

DATA_DIR: Path = PROJECT_ROOT / "data"
"""运行期数据目录（Qdrant 存储、registry.db、日志；已 gitignore）。"""

DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


@dataclass(frozen=True, slots=True)
class Settings:
    """进程级配置快照。

    Attributes:
        qdrant_url: Qdrant 单机服务地址（tech.md §2）。
        vault_path: Obsidian vault 根目录（摄取源，未配置时为 ``None``）。
        registry_db: SQLite 文档注册表文件路径。
        log_dir: 结构化日志输出目录。
        hf_endpoint: HuggingFace 镜像端点（tech.md §12 国内下载镜像）。
        deepseek_api_key: DeepSeek API key（仅胖端点使用；绝不入日志/库/payload）。
        deepseek_base_url: DeepSeek OpenAI 兼容接口地址。
        deepseek_model: 生成用模型名。
        host: API 监听地址（默认仅本机，code_standards §12）。
        port: API 监听端口（REST 与 MCP 同端口，tech.md §8）。
        ingest_workers: 摄取时嵌入推理的并发批次数。
        collection: 检索目标 collection；``None`` 表示用契约默认名
            （``recall__bge-m3@v1__md``）。A/B 评测时用 ``RECALL_COLLECTION`` 切库。
        log_to_file: 是否把结构化日志同时写进 ``log_dir``（``RECALL_LOG_TO_FILE=0`` 可关；
            测试关掉以免污染 ``data/``）。
    """

    qdrant_url: str
    vault_path: Path | None
    registry_db: Path
    log_dir: Path
    hf_endpoint: str
    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str
    host: str
    port: int
    ingest_workers: int
    collection: str | None
    log_to_file: bool

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> Settings:
        """从 ``.env`` 与环境变量构造配置（环境变量优先）。

        Args:
            dotenv_path: 显式指定的 ``.env`` 路径；默认读取项目根目录下的 ``.env``。

        Returns:
            填充完成的 :class:`Settings`。
        """
        load_dotenv(
            dotenv_path if dotenv_path is not None else PROJECT_ROOT / ".env", override=False
        )
        vault_raw = os.getenv("RECALL_VAULT_PATH", "").strip()
        db_raw = os.getenv("RECALL_REGISTRY_DB", "").strip()
        log_raw = os.getenv("RECALL_LOG_DIR", "").strip()
        collection_raw = os.getenv("RECALL_COLLECTION", "").strip()
        return cls(
            qdrant_url=os.getenv("QDRANT_URL", DEFAULT_QDRANT_URL).strip(),
            vault_path=Path(vault_raw) if vault_raw else None,
            registry_db=Path(db_raw) if db_raw else DATA_DIR / "registry.db",
            log_dir=Path(log_raw) if log_raw else DATA_DIR / "logs",
            hf_endpoint=os.getenv("HF_ENDPOINT", "https://hf-mirror.com").strip(),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip(),
            host=os.getenv("RECALL_HOST", "127.0.0.1").strip(),
            port=int(os.getenv("RECALL_PORT", "8000")),
            ingest_workers=int(os.getenv("RECALL_INGEST_WORKERS", "1")),
            collection=collection_raw or None,
            log_to_file=os.getenv("RECALL_LOG_TO_FILE", "1").strip() not in {"0", "false", "False"},
        )


def configure_logging(
    settings: Settings, *, level: str = "INFO", component: str = "recall"
) -> None:
    """配置结构化日志：控制台 + ``data/logs/<component>.log``（tech.md §11）。

    两件在 Windows 上必须做的事：

    1. stdout/stderr 切到 UTF-8（``errors="replace"``）——控制台默认 GBK，
       笔记标题里的 emoji 会让收尾打印抛 ``UnicodeEncodeError``；
    2. 文件日志用 ``RotatingFileHandler``，避免长跑进程把磁盘写满。

    幂等：重复调用只会复用已装好的 handler（uvicorn reload / 多次入口调用安全）。

    Args:
        settings: 运行配置（读 ``log_dir`` / ``log_to_file``）。
        level: 日志级别名，如 ``INFO`` / ``WARNING``。
        component: 日志文件名（不含扩展名），如 ``ingest`` / ``api``。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if settings.log_to_file:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                settings.log_dir / f"{component}.log",
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
