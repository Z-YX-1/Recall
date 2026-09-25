"""审计日志（tech.md §7 的 S2 要求；roadmap R-40；code_standards §9）。

**一次请求一行 JSON**（JSON lines），落在 ``data/logs/audit.jsonl``——与结构化日志
``api.log`` 分开存放，便于单独轮转、单独检索，也便于将来只把审计文件给运维。

设计约束：

1. **绝不写密钥**：审计行只记 ``user``，任何情况下都不记 token（连前缀都不记）；
2. **不拖累业务**：写审计失败只记一条 WARNING，绝不因此让请求失败；
3. **按大小轮转**：单个文件超过 :data:`MAX_AUDIT_BYTES` 就改名为 ``audit.jsonl.1``，
   避免长跑进程把磁盘写满（与 ``api.log`` 的 RotatingFileHandler 同源考虑）。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_AUDIT_BYTES = 5 * 1024 * 1024
"""审计文件轮转阈值（字节）。"""

OUTCOME_OK = "ok"
"""请求通过鉴权并走完处理。"""

OUTCOME_UNAUTHORIZED = "unauthorized"
"""请求被鉴权中间件拒绝（HTTP 401）。"""

OUTCOME_ERROR = "error"
"""请求已鉴权，但处理过程中返回了 4xx/5xx。"""


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """一条审计记录（字段即 JSON 的键，顺序固定便于人读）。

    Attributes:
        ts: UTC ISO 8601 时间戳。
        user: 身份 ``user``；**被拒的请求为 ``"-"``**（此时还没有身份）。
        groups: 身份 ``groups``。
        method: HTTP 方法。
        path: 请求路径（不含查询串，避免把查询内容写进审计）。
        status: HTTP 状态码。
        duration_ms: 处理耗时（毫秒，保留 1 位小数）。
        outcome: :data:`OUTCOME_OK` / :data:`OUTCOME_UNAUTHORIZED` / :data:`OUTCOME_ERROR`。
        trace_id: 请求级追踪 id（与 core 层写入 ``api.log`` 的 trace_id 相互独立）。
        client: 客户端主机（回环为 ``127.0.0.1``；用于区分本机与远程调用）。
    """

    ts: str
    user: str
    groups: list[str]
    method: str
    path: str
    status: int
    duration_ms: float
    outcome: str
    trace_id: str
    client: str


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串（秒级精度足够审计对账）。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


class AuditLog:
    """JSON lines 审计写入器（线程安全）。

    ``path`` 为 ``None`` 时**完全停写**——测试与 ``RECALL_LOG_TO_FILE=0``
    的场合用它来避免污染 ``data/``。
    """

    def __init__(self, path: Path | None) -> None:
        """构造写入器。

        Args:
            path: 审计文件路径；``None`` 表示停写（只保留内存中的空实现）。
        """
        self._path = path
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """是否真正落盘。"""
        return self._path is not None

    def record(self, record: AuditRecord) -> None:
        """追加一条审计记录。

        Args:
            record: 审计记录。
        """
        if self._path is None:
            return
        line = json.dumps(asdict(record), ensure_ascii=False)
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:  # 审计失败不能反过来打断业务请求
                logger.warning(
                    "audit.write_failed", extra={"path": str(self._path), "error": str(exc)}
                )

    def _rotate_if_needed(self) -> None:
        """超过阈值就把当前文件改名为 ``<name>.1``（覆盖上一份备份）。"""
        path = self._path
        if path is None or not path.exists() or path.stat().st_size < MAX_AUDIT_BYTES:
            return
        backup = path.with_suffix(path.suffix + ".1")
        backup.unlink(missing_ok=True)
        path.rename(backup)
