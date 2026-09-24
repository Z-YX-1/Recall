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
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"
"""HuggingFace 镜像（tech.md §12 国内下载镜像）。"""

DEFAULT_HF_HUB_OFFLINE = True
"""默认**离线**加载 HuggingFace 模型（``HF_HUB_OFFLINE=1``）。

为什么默认离线：transformers 装载 tokenizer 时会调
``list_repo_templates`` 去 Hub 拉 ``chat_template.jinja`` 清单，
**即便权重已在本地缓存**也要走一次网络；本机出网间歇性不可达
（roadmap §七 2026-09-23 R-32 环境记录），该请求会挂到 httpx 连接超时，
把整条 ``kb_search`` 拖死（实测 6 次调用全部 42s 后 ConnectTimeout）。
两个模型（bge-m3 / bge-reranker-v2-m3）都已完整落盘 HF cache，
离线加载实测正常；需要**下载新模型**时显式设 ``HF_HUB_OFFLINE=0``。
"""

DEFAULT_MCP_STATELESS = True
"""MCP Streamable HTTP 默认走**无会话（stateless）**模式（roadmap R-44）。

为什么默认无会话：状态化模式下会话 id 由服务进程生成、**存在内存里**，
于是两条与代码无关的路径都会把它弄丢，而客户端拿到 404 后**不会**
重新握手（MCP SDK 只是把 404 翻成 "Session terminated"，DSH 的
mcp-client 只在 transport 关闭时才重连）：

1. **空闲过期**：SDK 默认 30 分钟没请求就回收会话（实测日志
   ``Session … idle timeout``），隔半小时再问一次 = 一次 "Session not found"；
2. **服务进程重启**：内存里的会话表随之消失，此后**每一次**调用都是
   HTTP 404 ``{"code":-32600,"message":"Session not found"}``。

无会话模式下每个请求自带一次握手（本地单客户端，开销可忽略），
服务端不再有"会话"可丢 ⇒ 上面两条路径同时消失。代价是服务端主动
推送（``notifications/tools/list_changed``、SSE 续传）不可用，本项目
不需要；需要时设 ``RECALL_MCP_STATELESS=0`` 回到状态化。
"""

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def _read_bool(name: str, *, default: bool) -> bool:
    """读布尔型环境变量（未设置时用 ``default``）。

    与 :meth:`Settings.from_env` 里 ``log_to_file`` 的写法保持同一套词法：
    ``0`` / ``false`` / ``no`` / ``off``（忽略大小写与首尾空白）为假，
    ``1`` / ``true`` / ``yes`` / ``on`` 为真，其余值按 ``default`` 处理。

    Args:
        name: 环境变量名，如 ``"HF_HUB_OFFLINE"``。
        default: 变量未设置或值无法识别时的取值。

    Returns:
        解析后的布尔值。
    """
    raw = os.getenv(name, "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return default


def _sync_hf_offline(offline: bool) -> None:
    """把离线开关同步给**已导入**的 ``huggingface_hub``（兜底，见模块 docstring）。

    ⚠️ 为什么需要：``huggingface_hub.constants.HF_HUB_OFFLINE`` 在 **import 时**
    就从环境变量读出并固定（源码：``_is_true(os.environ.get("HF_HUB_OFFLINE"))``）。
    而 ``recall.api`` 会先 import ``recall.embedder`` → FlagEmbedding → transformers
    → huggingface_hub，**再**调用 :meth:`Settings.from_env`；此时再设环境变量已经晚了，
    库仍按"在线"处理 ⇒ 模型装载照样回连 Hub（2026-09-24 实测复现：**R-43 的修复在
    这个导入顺序下完全失效**，`kb_search` 依旧 42s 超时）。

    根治手段是让 ``recall/__init__.py`` 在包导入时就把环境变量设好（顺序正确时
    本函数会直接返回）；这里改常量只是**双保险**，覆盖"先 import transformers
    再 import recall"的用法。

    Args:
        offline: 目标离线状态。
    """
    hub_constants = sys.modules.get("huggingface_hub.constants")
    if hub_constants is None:
        return  # 库还没导入 ⇒ 稍后它自然会读到正确的环境变量
    if bool(getattr(hub_constants, "HF_HUB_OFFLINE", offline)) == offline:
        return
    setattr(hub_constants, "HF_HUB_OFFLINE", offline)  # noqa: B010 - 动态补丁第三方模块常量
    logging.getLogger(__name__).info(
        "config.hf_offline_patched", extra={"offline": offline, "reason": "库已先于配置导入"}
    )


@dataclass(frozen=True, slots=True)
class Settings:
    """进程级配置快照。

    Attributes:
        qdrant_url: Qdrant 单机服务地址（tech.md §2）。
        vault_path: Obsidian vault 根目录（摄取源，未配置时为 ``None``）。
        registry_db: SQLite 文档注册表文件路径。
        log_dir: 结构化日志输出目录。
        hf_endpoint: HuggingFace 镜像端点（tech.md §12 国内下载镜像）。
        hf_hub_offline: 是否强制 HuggingFace 库**只读本地缓存**
            （``HF_HUB_OFFLINE=1``）；默认 ``True``，见
            :data:`DEFAULT_HF_HUB_OFFLINE`。
        mcp_stateless: MCP Streamable HTTP 是否走**无会话**模式；默认
            ``True``，见 :data:`DEFAULT_MCP_STATELESS`。
        deepseek_api_key: DeepSeek API key（仅胖端点使用；绝不入日志/库/payload）。
        deepseek_base_url: DeepSeek OpenAI 兼容接口地址。
        deepseek_model: 生成用模型名。
        host: API 监听地址（默认仅本机，code_standards §12）。
        port: API 监听端口（REST 与 MCP 同端口，tech.md §8）。
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
    hf_hub_offline: bool
    mcp_stateless: bool
    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str
    host: str
    port: int
    collection: str | None
    log_to_file: bool

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> Settings:
        """从 ``.env`` 与环境变量构造配置（环境变量优先）。

        ⚠️ 副作用（有意为之）：把 ``HF_ENDPOINT`` 与 ``HF_HUB_OFFLINE`` 写进
        ``os.environ``——HF 镜像与离线开关必须在**任何模型加载之前**生效
        （tech.md §12），而这是全项目唯一的配置入口，放在这里才能保证
        "先建配置、后加载模型"的顺序绕不过去。两者都用 ``setdefault``：
        **真实环境变量优先**，所以要下载模型时 ``HF_HUB_OFFLINE=0`` 依然管用。

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
        settings = cls(
            qdrant_url=os.getenv("QDRANT_URL", DEFAULT_QDRANT_URL).strip(),
            vault_path=Path(vault_raw) if vault_raw else None,
            registry_db=Path(db_raw) if db_raw else DATA_DIR / "registry.db",
            log_dir=Path(log_raw) if log_raw else DATA_DIR / "logs",
            hf_endpoint=os.getenv("HF_ENDPOINT", DEFAULT_HF_ENDPOINT).strip(),
            hf_hub_offline=_read_bool("HF_HUB_OFFLINE", default=DEFAULT_HF_HUB_OFFLINE),
            mcp_stateless=_read_bool("RECALL_MCP_STATELESS", default=DEFAULT_MCP_STATELESS),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip(),
            host=os.getenv("RECALL_HOST", "127.0.0.1").strip(),
            port=int(os.getenv("RECALL_PORT", "8000")),
            collection=collection_raw or None,
            log_to_file=os.getenv("RECALL_LOG_TO_FILE", "1").strip() not in {"0", "false", "False"},
        )
        if settings.hf_endpoint:
            os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
        os.environ.setdefault("HF_HUB_OFFLINE", "1" if settings.hf_hub_offline else "0")
        _sync_hf_offline(settings.hf_hub_offline)
        return settings


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
