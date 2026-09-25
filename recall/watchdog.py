"""Vault 变更监听：watchdog 常驻**增量**同步（tech.md §5；roadmap R-38）。

它堵的是这样一条**静默失败**：写完新笔记后，检索看到的还是旧内容，而且不报错。

三条设计铁律，每条都对应一个踩过的坑：

1. **watcher 不自己装载模型**。它只负责"发现变化"，然后 ``POST /kb/ingest`` 给
   **已经在跑的 API**。若自己跑 ``ingest.py``，进程里会出现**第二份 bge-m3**
   （~2.2GB），正是 R-23b 那类 ``Windows fatal exception: access violation`` 的土壤，
   还要与 API 抢显存。
2. **只做增量**（``{"mode": "update"}``）。``--rebuild`` 会持有 ``inference_lock``
   （进程级单锁，``embedder.py:129`` / ``rerank.py:134`` 共用）数分钟，期间检索全部排队；
   重建仍须人工触发。
3. **必须过滤 ``.obsidian/``、``.trash/`` 等目录**。Obsidian 自己的索引目录**高频写**，
   不过滤会无限自触发。过滤规则直接复用摄取侧的
   :data:`~recall.connectors.obsidian.DEFAULT_SKIP_DIRS`，保证"watcher 触发的"
   与"ingest 会索引的"是同一套标准——否则会为 ingest 根本不在乎的文件空跑。

去抖用 watchdog 官方 :class:`~watchdog.utils.event_debouncer.EventDebouncer`：
编辑器保存**往往是"写临时文件 + 改名"甚至多次写**，事件会成串到达，
必须等目录安静下来再触发一次。

用法::

    python -m recall.watchdog                    # 常驻，默认监听 .env 里的 vault
    python -m recall.watchdog --once             # 同步一次就退出（cron / 排错）
    python -m recall.watchdog --force-polling    # 原生事件异常时的逃生口
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver
from watchdog.utils.event_debouncer import EventDebouncer

from recall.config import Settings, configure_logging
from recall.connectors.obsidian import DEFAULT_SKIP_DIRS, MARKDOWN_SUFFIX

logger = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_S = 1.0
"""目录安静多久才触发同步（秒）。编辑器保存常连发多个事件，需要攒。"""

DEFAULT_RETRIES = 3
"""单次同步的 HTTP 重试次数（含首次）。"""

DEFAULT_RETRY_DELAY_S = 2.0
"""重试间隔（秒）；退避倍数固定，脚本够用又不至于把日志刷爆。"""

DEFAULT_TIMEOUT_S = 600.0
"""单次 ``POST /kb/ingest`` 的超时（秒）。增量通常数秒；给足余量以免误判失败。"""

MAX_COALESCED_ROUNDS = 5
"""一次触发期间若又有新变化，最多连做几轮（防止持续写入把循环饿死）。"""


def _event_path(event: FileSystemEvent) -> str:
    """取事件路径并统一成 ``str``。

    watchdog 的 ``src_path`` 类型是 ``bytes | str``（用 bytes 路径的调用方才拿到
    ``bytes``）。本项目只用 ``str``，这里做一次显式归一，免得下游到处判类型。

    Args:
        event: 文件系统事件。

    Returns:
        文件路径字符串。
    """
    raw = event.src_path
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def should_index(path: str | Path, vault: Path) -> bool:
    """判断某个路径的变化是否值得触发一次同步。

    规则与摄取侧保持一致（见模块 docstring 第 3 条）：

    - 只认 ``.md``；
    - 相对路径中任何一段以 ``.`` 开头（``.obsidian`` / ``.trash`` / ``.git`` …）⇒ 跳过；
    - 相对路径中出现 :data:`~recall.connectors.obsidian.DEFAULT_SKIP_DIRS` 里的目录名 ⇒ 跳过。

    Args:
        path: 被改动的文件路径（watchdog 给出的是绝对路径）。
        vault: vault 根目录。

    Returns:
        值得触发同步为 ``True``。
    """
    candidate = Path(path)
    if candidate.suffix.lower() != MARKDOWN_SUFFIX:
        return False
    try:
        relative_parts = candidate.resolve().relative_to(vault.resolve()).parts
    except ValueError:  # 不在 vault 内（软链接越界等）⇒ 不管
        return False
    for part in relative_parts[:-1]:  # 只看目录部分（文件名允许以点开头）
        if part.startswith(".") or part in DEFAULT_SKIP_DIRS:
            return False
    return True


@dataclass(slots=True)
class IngestTrigger:
    """向运行中的 API 发起一次增量摄取（**唯一**的副作用出口）。

    Attributes:
        api_url: API 根地址，如 ``http://127.0.0.1:8000``。
        api_key: 启用鉴权（``RECALL_API_KEYS`` 非空）时必填。
        timeout: 单次请求超时（秒）。
        retries: 重试次数（含首次）。
        retry_delay: 重试间隔（秒）。
    """

    api_url: str
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT_S
    retries: int = DEFAULT_RETRIES
    retry_delay: float = DEFAULT_RETRY_DELAY_S

    def trigger(self) -> bool:
        """发起一次 ``POST /kb/ingest {"mode": "update"}``（失败重试）。

        Returns:
            成功（HTTP 2xx）为 ``True``；重试耗尽后为 ``False``（调用方只记日志，
            **绝不因此退出进程**——watcher 死了就再也没人提醒你笔记过期了）。
        """
        body = json.dumps({"mode": "update"}).encode("utf-8")
        for attempt in range(1, self.retries + 1):
            try:
                status = self._post(body)
            except (urllib.error.URLError, OSError) as exc:
                logger.warning(
                    "watchdog.ingest_unreachable 第 %d/%d 次：%s",
                    attempt,
                    self.retries,
                    exc,
                    extra={"attempt": attempt},
                )
            except urllib.error.HTTPError as exc:  # 4xx/5xx 也走重试（API 可能刚起来）
                logger.warning(
                    "watchdog.ingest_http_error 第 %d/%d 次：HTTP %s",
                    attempt,
                    self.retries,
                    exc.code,
                    extra={"attempt": attempt, "status": exc.code},
                )
            else:
                if 200 <= status < 300:
                    logger.info("watchdog.ingest_ok", extra={"status": status})
                    return True
                logger.warning(
                    "watchdog.ingest_failed 第 %d/%d 次：HTTP %s",
                    attempt,
                    self.retries,
                    status,
                    extra={"attempt": attempt, "status": status},
                )
            if attempt < self.retries:
                time.sleep(self.retry_delay)
        logger.error(
            "watchdog.ingest_giving_up 已重试 %d 次仍未成功——"
            "检查 API 是否在跑（python -m recall.api）",
            self.retries,
        )
        return False

    def _post(self, body: bytes) -> int:
        """发一次请求并返回状态码（异常交由 :meth:`trigger` 归类重试）。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        request = urllib.request.Request(
            f"{self.api_url.rstrip('/')}/kb/ingest",
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return int(response.status)


class _VaultEventHandler(FileSystemEventHandler):
    """把 watchdog 事件喂给去抖器（只做筛选，不做任何耗时操作）。"""

    def __init__(self, vault: Path, debouncer: EventDebouncer) -> None:
        """记录 vault 与去抖器。

        Args:
            vault: vault 根目录。
            debouncer: watchdog 官方去抖器。
        """
        self._vault = vault
        self._debouncer = debouncer

    def on_any_event(self, event: FileSystemEvent) -> None:
        """目录事件与非 ``.md`` / 被排除目录的变化都不进入去抖器。"""
        if event.is_directory:
            return
        if not should_index(_event_path(event), self._vault):
            return
        self._debouncer.handle_event(event)


@dataclass(slots=True)
class VaultWatcher:
    """常驻监听 vault，安静下来后触发一次增量同步。

    Attributes:
        vault: vault 根目录。
        trigger: 增量摄取出口。
        debounce_s: 去抖窗口（秒）。
        polling: 用轮询式 observer（原生事件异常时的逃生口）。
    """

    vault: Path
    trigger: IngestTrigger
    debounce_s: float = DEFAULT_DEBOUNCE_S
    polling: bool = False
    _observer: BaseObserver | None = field(default=None, init=False, repr=False)
    _debouncer: EventDebouncer | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _busy: bool = field(default=False, init=False, repr=False)
    _pending: bool = field(default=False, init=False, repr=False)
    _syncs: int = field(default=0, init=False, repr=False)
    """已完成的同步轮数（测试与日志用）。"""

    @property
    def syncs(self) -> int:
        """已完成的同步轮数。"""
        return self._syncs

    def start(self) -> None:
        """启动 observer 与去抖器（不阻塞）。"""
        # ⚠️ watchdog 把 debounce_interval_seconds 注解成 int，内部却按秒直接用浮点
        # （`time` 比较），传 0.5 这类小数完全有效 —— 注解偏窄，故此处窄化忽略。
        debouncer: EventDebouncer = EventDebouncer(
            debounce_interval_seconds=self.debounce_s,  # type: ignore[arg-type]
            events_callback=self._on_debounced,
        )
        observer: BaseObserver = PollingObserver() if self.polling else Observer()
        observer.schedule(
            _VaultEventHandler(self.vault, debouncer), str(self.vault), recursive=True
        )
        self._debouncer = debouncer
        self._observer = observer
        debouncer.start()
        observer.start()
        logger.info(
            "watchdog.started",
            extra={
                "vault": str(self.vault),
                "debounce_s": self.debounce_s,
                "polling": self.polling,
                "api_url": self.trigger.api_url,
            },
        )

    def stop(self) -> None:
        """停止 observer 与去抖器（幂等，可重复调用）。"""
        observer, self._observer = self._observer, None
        debouncer, self._debouncer = self._debouncer, None
        if debouncer is not None:
            debouncer.stop()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
        if debouncer is not None:
            debouncer.join(timeout=5)
        logger.info("watchdog.stopped", extra={"syncs": self._syncs})

    def sync_once(self) -> bool:
        """立刻同步一次（``--once`` 与启动时补一次用它）。

        Returns:
            :meth:`IngestTrigger.trigger` 的结果。
        """
        return self.trigger.trigger()

    def _on_debounced(self, events: list[FileSystemEvent]) -> None:
        """去抖回调：目录已安静，触发同步（在去抖器线程里执行）。"""
        paths = sorted({_event_path(event) for event in events})
        logger.info("watchdog.change_detected", extra={"count": len(paths), "sample": paths[:5]})
        with self._lock:
            if self._busy:
                # 上一次同步还没跑完，别并发再发一个（也避免把 API 的模型锁挤爆）
                self._pending = True
                logger.info("watchdog.sync_coalesced")
                return
            self._busy = True
        try:
            self._drain()
        finally:
            with self._lock:
                self._busy = False
                self._pending = False

    def _drain(self) -> None:
        """跑同步；期间又有新变化就再来一轮（有上限，避免被持续写入饿死）。"""
        for _ in range(MAX_COALESCED_ROUNDS):
            with self._lock:
                self._pending = False
            if not self.trigger.trigger():
                # 失败不重排队：下一次事件或下一次启动补同步会兜住
                return
            self._syncs += 1
            with self._lock:
                if not self._pending:
                    return


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（风格与 ``ingest.py`` 保持一致）。"""
    parser = argparse.ArgumentParser(
        prog="recall.watchdog",
        description="监听 Obsidian vault 变化并触发增量摄取（roadmap R-38）。",
    )
    parser.add_argument("--vault", help="vault 根目录；默认取 RECALL_VAULT_PATH")
    parser.add_argument("--api-url", help="API 根地址；默认 http://<RECALL_HOST>:<RECALL_PORT>")
    parser.add_argument("--api-key", help="启用鉴权时的 API key；默认取 RECALL_WATCHDOG_API_KEY")
    parser.add_argument(
        "--debounce",
        type=float,
        default=DEFAULT_DEBOUNCE_S,
        help=f"去抖窗口秒数（默认 {DEFAULT_DEBOUNCE_S}）",
    )
    parser.add_argument(
        "--once", action="store_true", help="只同步一次就退出（cron / 排错用）"
    )
    parser.add_argument(
        "--no-initial-sync",
        action="store_true",
        help="启动时先补一次同步（默认补，用来抓住 watcher 没跑时发生的改动）",
    )
    parser.add_argument(
        "--force-polling", action="store_true", help="改用轮询式 observer（原生事件异常时的逃生口）"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="单次请求超时秒数")
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"失败重试次数（默认 {DEFAULT_RETRIES}）",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY_S,
        help=f"重试间隔秒数（默认 {DEFAULT_RETRY_DELAY_S}）",
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别（默认 INFO）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m recall.watchdog`` 入口。

    Args:
        argv: 命令行参数；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        进程退出码：常驻模式正常退出为 0；``--once`` 失败为 1；缺 vault 为 2。
    """
    args = build_parser().parse_args(argv)
    settings: Settings = Settings.from_env()
    configure_logging(settings, level=args.log_level, component="watchdog")

    vault_raw = args.vault or (str(settings.vault_path) if settings.vault_path else "")
    if not vault_raw:
        logger.error("watchdog.no_vault 未指定 vault：请设 RECALL_VAULT_PATH 或传 --vault")
        return 2
    vault = Path(vault_raw)
    if not vault.is_dir():
        logger.error("watchdog.vault_missing", extra={"vault": str(vault)})
        return 2

    api_url = args.api_url or f"http://{settings.host}:{settings.port}"
    api_key = args.api_key or settings.watchdog_api_key
    if settings.auth_enabled and not api_key:
        logger.warning(
            "watchdog.no_api_key 服务已启用鉴权（RECALL_API_KEYS 非空）但未提供 key："
            "请设 RECALL_WATCHDOG_API_KEY 或传 --api-key，否则同步会一直 401。"
        )

    watcher = VaultWatcher(
        vault=vault,
        trigger=IngestTrigger(
            api_url=api_url,
            api_key=api_key,
            timeout=args.timeout,
            retries=args.retries,
            retry_delay=args.retry_delay,
        ),
        debounce_s=args.debounce,
        polling=args.force_polling,
    )

    if args.once:
        return 0 if watcher.sync_once() else 1

    if not args.no_initial_sync:
        # 补一次：watcher 没跑的时候写的笔记也要进库（幂等，代价只有一次 hash 扫描）
        watcher.sync_once()

    watcher.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("watchdog.interrupted")
    finally:
        watcher.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
