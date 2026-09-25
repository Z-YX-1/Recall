"""Vault 监听测试（roadmap R-38；tech.md §5）。

覆盖四条设计铁律中的三条**可离线验证**的部分：

1. **只触发该触发的**：``.obsidian``/``.trash``/``node_modules``/非 ``.md`` 一律不触发
   ——过滤规则必须与摄取侧同一套，否则会为 ingest 根本不在乎的文件空跑；
2. **只做增量**：请求体恒为 ``{"mode": "update"}``，**永不出现 ``rebuild``**
   （重建会持有 ``inference_lock`` 数分钟，把检索全堵住）；
3. **去抖**：一连串保存事件只换来**一次**同步；
4. **失败不静默**：API 不可达时重试到上限后返回 ``False``，且**不抛异常**。

第 4 条铁律（watcher 不自己装载模型）由结构保证：本模块不 import embedder/reranker，
只在 :class:`IngestTrigger` 里发一个 HTTP 请求——测试用**真实本地 HTTP 桩**验证报文。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from recall.watchdog import IngestTrigger, VaultWatcher, build_parser, main, should_index
from tests.helpers import write_note

DEBOUNCE_S = 0.5
"""测试用去抖窗口；比生产默认（1s）短，兼顾稳定与快。"""

WAIT_S = 15.0
"""等待触发的上限（Windows 原生事件在负载高时可能迟到）。"""


# --------------------------------------------------------------------------- 路径过滤


def test_should_index_accepts_markdown_anywhere_in_the_vault(vault: Path) -> None:
    """vault 内的 ``.md`` 都算数，含子目录。"""
    assert should_index(vault / "笔记.md", vault) is True
    assert should_index(vault / "子目录" / "更浅" / "笔记.md", vault) is True


def test_should_index_rejects_non_markdown(vault: Path) -> None:
    """只认 ``.md``：Obsidian 的 ``workspace.json`` 之类不该触发摄取。"""
    assert should_index(vault / "workspace.json", vault) is False
    assert should_index(vault / "图片.png", vault) is False


@pytest.mark.parametrize(
    "relative",
    [
        ".obsidian/workspace.md",
        ".obsidian/plugins/x/README.md",
        ".trash/被删掉的笔记.md",
        ".git/COMMIT_EDITMSG.md",
        "project/node_modules/pkg/README.md",
        "dist/note.md",
        "__pycache__/note.md",
    ],
)
def test_should_index_skips_internal_and_build_dirs(vault: Path, relative: str) -> None:
    """``.obsidian`` 等目录高频写，**必须**过滤，否则会无限自触发。

    过滤集合直接取自摄取侧的 ``DEFAULT_SKIP_DIRS``，保证与"ingest 会索引什么"一致。
    """
    assert should_index(vault / relative, vault) is False


def test_should_index_ignores_paths_outside_the_vault(vault: Path, tmp_path: Path) -> None:
    """vault 之外的路径（软链接越界等）不管。"""
    outside = tmp_path / "elsewhere" / "笔记.md"
    outside.parent.mkdir()
    assert should_index(outside, vault) is False


# --------------------------------------------------------------------------- 真实 HTTP 桩


class _StubApi:
    """极小的本地 HTTP 桩：记录收到的请求，按设定状态码作答。"""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = status
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """桩服务的根地址。"""
        assert self._server is not None
        host, port = self._server.server_address[0], int(self._server.server_address[1])
        host_text = host.decode("utf-8") if isinstance(host, bytes) else str(host)
        return f"http://{host_text}:{port}"

    def __enter__(self) -> _StubApi:
        recorder = self.requests
        status = self.status

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server 规定的接口名
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                recorder.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body.decode("utf-8")}
                )
                payload = b"{}"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                """静音（否则每个请求都会打到 stderr）。"""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def test_trigger_posts_incremental_update_with_the_api_key() -> None:
    """触发报文三要素：路径 ``/kb/ingest``、体 ``mode=update``、鉴权头。"""
    with _StubApi() as api:
        trigger = IngestTrigger(api_url=api.url, api_key="tok-123", retries=1)

        assert trigger.trigger() is True

    assert len(api.requests) == 1
    sent = api.requests[0]
    assert sent["path"] == "/kb/ingest"
    assert json.loads(sent["body"]) == {"mode": "update"}  # 永不 rebuild
    # HTTP 头大小写无关（urllib 会把头名 capitalize 成 X-api-key），故归一小写后比对
    headers = {name.lower(): value for name, value in sent["headers"].items()}
    assert headers["x-api-key"] == "tok-123"


def test_trigger_omits_the_key_header_when_auth_is_off() -> None:
    """未启用鉴权时不发空头（免得日志里出现一个没意义的字段）。"""
    with _StubApi() as api:
        IngestTrigger(api_url=api.url, retries=1).trigger()

    headers = {name.lower() for name in api.requests[0]["headers"]}
    assert "x-api-key" not in headers


def test_trigger_returns_false_after_retries_without_raising() -> None:
    """API 不可达 ⇒ 重试到上限后返回 ``False``，**绝不抛异常**。

    watcher 若因一次失败就崩掉，静默过期问题就又回来了——所以失败必须只记日志。
    """
    # 端口 1 上没有服务；conftest 已设 NO_PROXY=* ⇒ 这里是真正的"连接被拒"
    trigger = IngestTrigger(api_url="http://127.0.0.1:1", retries=2, retry_delay=0.01)

    assert trigger.trigger() is False


def test_trigger_returns_false_on_server_error() -> None:
    """5xx 同样只记日志、返回 ``False``（API 可能是刚起来还没就绪）。"""
    with _StubApi(status=500) as api:
        trigger = IngestTrigger(api_url=api.url, retries=2, retry_delay=0.01)

        assert trigger.trigger() is False

    assert len(api.requests) == 2  # 重试确实发生了


# --------------------------------------------------------------------------- 去抖与常驻


class _RecordingTrigger(IngestTrigger):
    """只计数、不发请求的触发器（子类化以保持类型一致）。"""

    def __init__(self) -> None:
        super().__init__(api_url="http://127.0.0.1:1", retries=1)
        self.calls = 0

    def trigger(self) -> bool:
        """记录调用次数并假装成功。"""
        self.calls += 1
        return True


@contextmanager
def _running(watcher: VaultWatcher) -> Iterator[VaultWatcher]:
    """启动 watcher 并保证退出时停掉（Windows 下 observer 线程必须收干净）。"""
    watcher.start()
    try:
        yield watcher
    finally:
        watcher.stop()


def _wait_for(predicate: Any, timeout: float = WAIT_S) -> bool:
    """轮询等待条件成立（避免用固定 sleep 造成偶发失败）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_a_burst_of_saves_collapses_into_one_sync(vault: Path) -> None:
    """编辑器"写临时文件 + 改名 + 多次写"会连发事件，去抖后**只同步一次**。"""
    rec = _RecordingTrigger()
    watcher = VaultWatcher(vault=vault, trigger=rec, debounce_s=DEBOUNCE_S)

    with _running(watcher):
        for index in range(5):
            write_note(vault, f"笔记{index}.md", f"# 标题\n\n第 {index} 次保存\n")
            time.sleep(0.05)
        assert _wait_for(lambda: rec.calls > 0), "去抖后应当触发同步"

    assert rec.calls == 1, f"连发 5 次写应只触发一次，实际 {rec.calls} 次"
    assert watcher.syncs == 1


def test_obsidian_internal_writes_do_not_trigger(vault: Path) -> None:
    """``.obsidian/`` 里的写入（Obsidian 自己一直在写）不得触发摄取。

    这是最容易踩的自触发陷阱：不挡的话，每次同步都会引发新一轮同步。
    """
    rec = _RecordingTrigger()
    internal = vault / ".obsidian"
    internal.mkdir()

    with _running(VaultWatcher(vault=vault, trigger=rec, debounce_s=DEBOUNCE_S)):
        for index in range(5):
            (internal / f"workspace{index}.json").write_text("{}", encoding="utf-8")
            write_note(internal, f"cache{index}.md", "# 不该被索引\n")
            time.sleep(0.05)
        time.sleep(DEBOUNCE_S * 3)

    assert rec.calls == 0, "内部目录的写入不应触发同步"


def test_sync_once_returns_false_when_api_is_down(vault: Path) -> None:
    """``--once`` 的返回语义：API 不可达 ⇒ ``False``（CLI 据此返回退出码 1）。"""
    watcher = VaultWatcher(
        vault=vault,
        trigger=IngestTrigger(api_url="http://127.0.0.1:1", retries=1, retry_delay=0.01),
    )

    assert watcher.sync_once() is False


# --------------------------------------------------------------------------- CLI


def test_cli_defaults_match_the_documented_behaviour() -> None:
    """默认值即契约：去抖 1s、增量、不强制轮询。"""
    args = build_parser().parse_args([])

    assert args.debounce == 1.0
    assert args.once is False
    assert args.force_polling is False
    assert args.no_initial_sync is False


def test_cli_returns_2_when_no_vault_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配 vault 不是崩溃，是"用错了" ⇒ 退出码 2 + 明确日志。"""
    monkeypatch.setenv("RECALL_VAULT_PATH", "")

    assert main(["--once"]) == 2


def test_cli_once_reports_failure_via_exit_code(vault: Path, tmp_path: Path) -> None:
    """``--once`` 在 API 不可达时返回退出码 1（供 cron / 任务计划判断）。"""
    code = main(
        [
            "--once",
            "--vault",
            str(vault),
            "--api-url",
            "http://127.0.0.1:1",
            "--retries",
            "1",
            "--retry-delay",
            "0.01",
            "--log-level",
            "WARNING",
        ]
    )

    assert code == 1
