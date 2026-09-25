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

DEFAULT_API_KEYS = ""
"""API key 表默认值：**空**（roadmap R-40，tech.md §7 的 S2 身份中间件）。

格式 ``token:user``，多个用逗号分隔：``RECALL_API_KEYS="tok1:me,tok2:alice"``。

**空的语义（重要，且是有意的）**：空 ⇒ **不启用鉴权**，退回 S1「无身份」语义。
S1 的防线本来就是"只绑定 ``127.0.0.1``"，而本项目交付时并不存在 key 表；
若把"没配 key"当成"拒绝一切请求"，升级即中断日常使用。因此这里选择
**fail-open**：只有**配置了** key 表才启用强制鉴权。

⚠️ 由此推出一条硬约束：**R-39（Coze 公网接入）的前置条件之一是必须配置 key 表**——
否则公网网关一挂，``POST /kb/ingest`` 这个写端点就是公开的（tech.md §7、code_standards §6.1）。
配置 key 表后，除 ``/health`` 外的**所有**端点（**含回环请求与 ``/mcp``**）都必须携带
``X-API-Key`` 或 ``Authorization: Bearer``。
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


def parse_api_keys(raw: str) -> dict[str, str]:
    """解析 ``RECALL_API_KEYS``（``token:user``，逗号分隔）→ ``{token: user}``。

    空串与纯空白条目都会被跳过；格式错误**立即抛错**——配置错误必须在启动时
    大声失败，而不是悄悄退化成"没有鉴权"（这是 fail-open 默认值之外唯一的兜底）。

    Args:
        raw: 环境变量原文；空串表示不启用鉴权。

    Returns:
        ``{"tok1": "me"}`` 形式的映射；``raw`` 为空时返回空字典。

    Raises:
        ValueError: 条目缺少冒号、token / user 为空，或出现重复 token。
    """
    keys: dict[str, str] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        token, sep, user = entry.partition(":")
        token, user = token.strip(), user.strip()
        if not sep or not token or not user:
            raise ValueError(f"RECALL_API_KEYS 条目格式应为 token:user，实得 {entry!r}")
        if token in keys:
            # ⚠️ 只回显前 4 位：错误信息会进日志，不能把完整密钥写进去
            raise ValueError(f"RECALL_API_KEYS 出现重复 token（前缀 {token[:4]}…）")
        keys[token] = user
    return keys


DEFAULT_EVIDENCE_MIN_SCORE = 0.0
"""证据门槛默认值：**0.0 = 不启用**（roadmap R-42）。

含义：精排后**最高分**低于该阈值 ⇒ 判定"笔记里没有相关内容"，返回**空证据包**。
⚠️ 它是**门槛**而不是**裁剪**：命中时证据带**原样保留**，只决定"答不答"。
（两者的代价天差地别——按分数逐条删证据会在 t=0.20 就把 Recall@10 打到 96.67%，
因为黄金集里有题目的期望来源排在第 7 名。见 `eval/BASELINE.md` §7。）

为什么默认关闭：它会改变**可观察行为**（笔记外问题从"回一堆低相关片段"变成"明确说没有"），
属契约级变更，须项目工程师拍板后由运维显式打开。

实测建议值：**0.58**（笔记内 top1 最低 0.799、笔记外最高 0.354，空档中点；2026-09-25 测量）。
"""


def _read_min_score(name: str, default: float) -> float:
    """读 0~1 之间的浮点配置（未设置时用 ``default``）。

    Args:
        name: 环境变量名，如 ``"RECALL_EVIDENCE_MIN_SCORE"``。
        default: 变量未设置或为空白时的取值。

    Returns:
        解析后的阈值。

    Raises:
        ValueError: 不是数字，或不在 ``[0, 1]`` 内。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是 0~1 之间的数字，实得 {raw!r}") from exc
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} 必须落在 0~1，实得 {value}")
    return value


DEFAULT_INGEST_RATE_LIMIT = "10/60"
"""写端点（``POST /kb/ingest``）的限流默认值：**10 次 / 60 秒**。

code_standards §6.1：写端点"自 S2 起必须挂鉴权 + 限流"。默认值取得**远高于**任何
合理本地用量（watcher 去抖后一次变更只触发一次；偶发的手工 `--update` 也算在内），
所以正常使用不会撞到；但一个跑飞的脚本或未来的公网调用会被挡住。

设 ``RECALL_INGEST_RATE_LIMIT=0``（或 ``off``）可**关闭**限流。
"""


def _read_rate_limit(name: str, default: str) -> tuple[int, float]:
    """解析限流配置 ``"N/W"``（N 次 / W 秒）。

    Args:
        name: 环境变量名。
        default: 未设置时的取值。

    Returns:
        ``(limit, window_s)``；``limit == 0`` 表示关闭。

    Raises:
        ValueError: 格式不是 ``N/W``，或 N/W 非正数。
    """
    raw = (os.getenv(name) or default).strip()
    text = raw.lower()
    if text in {"", "0", "off", "none", "disable", "disabled"}:
        return 0, 0.0
    limit_raw, separator, window_raw = text.partition("/")
    if not separator:
        raise ValueError(f"{name} 格式应为「次数/秒数」（如 10/60）或用 0 关闭，实得 {raw!r}")
    try:
        limit = int(limit_raw)
        window = float(window_raw)
    except ValueError as exc:
        raise ValueError(f"{name} 的次数与秒数都必须是数字，实得 {raw!r}") from exc
    if limit <= 0 or window <= 0:
        raise ValueError(f"{name} 的次数与秒数都必须为正，实得 {raw!r}")
    return limit, window


LOCAL_HOSTS_NO_PROXY: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
"""必须**绕过 HTTP 代理**的回环地址（roadmap R-46）。"""


def _ensure_localhost_bypasses_proxy() -> None:
    """把回环地址并入 ``NO_PROXY``（幂等，保留用户已有条目）。

    为什么必须做：Windows 的**系统代理**会被 httpx 的 ``trust_env=True`` 读到，
    于是连**本机**服务（Qdrant @127.0.0.1:6333）的请求也被发给代理；代理对不可达端口
    返回 **HTTP 502 空体**，把"连接被拒"变成"网关错误"，qdrant-client 抛
    ``UnexpectedResponse`` —— `with_retry` 认不出这是"Qdrant 没起来"，
    用户看到 500 而不是 503「请启动 qdrant.exe」（2026-09-25 实测，
    本机 karingService @127.0.0.1:3067）。

    ⚠️ **只加回环，绝不设 ``*``**：出网请求（DeepSeek）可能正需要这个代理，
    全局禁用会连它一起打断。实测：加回环后 localhost 恢复"连接被拒"，
    而 ``https://api.deepseek.com`` 仍可达。
    """
    existing = [part.strip() for part in os.environ.get("NO_PROXY", "").split(",") if part.strip()]
    merged = list(existing)
    for host in LOCAL_HOSTS_NO_PROXY:
        if host not in merged:
            merged.append(host)
    value = ",".join(merged)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value  # 大小写两版都设：不同库读的键名不一致


DEFAULT_MCP_TOOL_POLICY = ""
"""MCP 工具白名单（``RECALL_MCP_TOOL_POLICY``），格式 ``用户:工具1|工具2``，逗号分隔多条。

**空 = 不启用**（所有身份都能用全部工具）——默认必须是"什么都不改变"。

为什么需要它（roadmap R-39）：扣子官方 MCP 文档指出，MCP 的工具名/说明/参数会**占用
Agent 上下文**并增加 Token 与积分消耗；而本项目对外暴露的 ``kb_ingest`` 是**写端点**。
更关键的是本机只有一份模型（≈4.5GB 显存），"另起一个公网实例 + server-wide 白名单"
会双份占显存（R-23b 那类崩溃的土壤）⇒ 让"本机拿全套、公网只拿读工具"同时成立，
只能**按身份**区分。示例：``RECALL_MCP_TOOL_POLICY="coze:kb_search|kb_answer"``。

语义：**未列出的用户不受限**（拿全部工具）——漏配用户只会"多给"，不会把人锁死。
"""


def _read_tool_policy(name: str, default: str) -> dict[str, frozenset[str]]:
    """解析 MCP 工具白名单 ``用户:工具1|工具2``（逗号分隔多条）。

    Args:
        name: 环境变量名。
        default: 未设置时的取值。

    Returns:
        ``{user: frozenset(tool)}``；空配置返回空字典（= 不启用）。

    Raises:
        ValueError: 条目缺冒号、用户或工具为空，或同一用户重复出现。
    """
    raw = (os.getenv(name) or default).strip()
    rules: dict[str, frozenset[str]] = {}
    if not raw:
        return rules
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        user, separator, tools_raw = entry.partition(":")
        user = user.strip()
        tools = frozenset(tool.strip() for tool in tools_raw.split("|") if tool.strip())
        if not separator or not user or not tools:
            raise ValueError(f"{name} 条目格式应为 user:tool1|tool2，实得 {entry!r}")
        if user in rules:
            raise ValueError(f"{name} 出现重复用户 {user!r}")
        rules[user] = tools
    return rules


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
        api_keys: API key 表 ``{token: user}``（``RECALL_API_KEYS``）；
            **空表示不启用鉴权**（S1 语义），见 :data:`DEFAULT_API_KEYS`。
            非空时除 ``/health`` 外所有端点强制携带 ``X-API-Key`` /
            ``Authorization: Bearer``。绝不入日志（错误信息只回显前 4 位）。
        watchdog_api_key: vault 监听进程（roadmap R-38）调 ``POST /kb/ingest`` 时
            使用的 token（``RECALL_WATCHDOG_API_KEY``）。取 :attr:`api_keys` 里
            任意一把即可；**未启用鉴权时留空**。
        evidence_min_score: 证据门槛（``RECALL_EVIDENCE_MIN_SCORE``）：精排最高分低于它
            即判"笔记里没有"，返回空证据包；**0.0 = 不启用**（默认），见
            :data:`DEFAULT_EVIDENCE_MIN_SCORE`。
        ingest_rate_limit: 写端点限流次数（``RECALL_INGEST_RATE_LIMIT``，格式 ``N/W``）；
            **0 = 关闭**。默认见 :data:`DEFAULT_INGEST_RATE_LIMIT`。
        ingest_rate_window_s: 写端点限流窗口秒数（与 ``ingest_rate_limit`` 同源）。
        mcp_tool_policy: MCP 工具白名单 ``{user: {tool}}``（``RECALL_MCP_TOOL_POLICY``）；
            **空表示不启用**（所有身份都能用全部工具），见 :data:`DEFAULT_MCP_TOOL_POLICY`。
            未列出的用户不受限。
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
    api_keys: dict[str, str]
    watchdog_api_key: str | None
    evidence_min_score: float
    ingest_rate_limit: int
    ingest_rate_window_s: float
    mcp_tool_policy: dict[str, frozenset[str]]
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

        同理会把回环地址并入 ``NO_PROXY``（见 :func:`_ensure_localhost_bypasses_proxy`）：
        本机服务不该走系统代理。

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
        rate_limit, rate_window = _read_rate_limit(
            "RECALL_INGEST_RATE_LIMIT", DEFAULT_INGEST_RATE_LIMIT
        )
        settings = cls(
            qdrant_url=os.getenv("QDRANT_URL", DEFAULT_QDRANT_URL).strip(),
            vault_path=Path(vault_raw) if vault_raw else None,
            registry_db=Path(db_raw) if db_raw else DATA_DIR / "registry.db",
            log_dir=Path(log_raw) if log_raw else DATA_DIR / "logs",
            hf_endpoint=os.getenv("HF_ENDPOINT", DEFAULT_HF_ENDPOINT).strip(),
            hf_hub_offline=_read_bool("HF_HUB_OFFLINE", default=DEFAULT_HF_HUB_OFFLINE),
            mcp_stateless=_read_bool("RECALL_MCP_STATELESS", default=DEFAULT_MCP_STATELESS),
            api_keys=parse_api_keys(os.getenv("RECALL_API_KEYS", DEFAULT_API_KEYS)),
            watchdog_api_key=os.getenv("RECALL_WATCHDOG_API_KEY") or None,
            evidence_min_score=_read_min_score(
                "RECALL_EVIDENCE_MIN_SCORE", DEFAULT_EVIDENCE_MIN_SCORE
            ),
            ingest_rate_limit=rate_limit,
            ingest_rate_window_s=rate_window,
            mcp_tool_policy=_read_tool_policy(
                "RECALL_MCP_TOOL_POLICY", DEFAULT_MCP_TOOL_POLICY
            ),
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
        _ensure_localhost_bypasses_proxy()
        return settings

    @property
    def auth_enabled(self) -> bool:
        """是否启用强制鉴权（即是否配置了 key 表）。

        Returns:
            配置了至少一个 key 时为 ``True``；空 key 表表示 S1 语义、不鉴权。
        """
        return bool(self.api_keys)

    @property
    def audit_log_path(self) -> Path:
        """审计日志文件路径（JSON lines，与结构化日志同目录但独立文件）。

        独立成文件是为了便于单独轮转与审计检索（``data/logs/audit.jsonl``）。

        Returns:
            审计日志的绝对路径。
        """
        return self.log_dir / "audit.jsonl"


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
