"""``extra=`` 渲染（roadmap §七 2026-10-03 项目工程师批准）。

**为什么值得一组专门用例**：``LOG_FORMAT`` 只有 ``%(message)s``，而项目里 **42 处**在用
``logger.info("xxx.done", extra={"trace_id": …})`` —— 那些字段**会被整个丢掉**。
实测代价（2026-10-03）：**两次白排查**（``event_ignored`` 与 ``reply_failed`` 的详情都在
extra 里，日志里一个字看不到），以及 ``trace_id`` 这个"串联一次请求全链路"的唯一钥匙失效。

⚠️ 这组用例的真正重点**不是"能打出来"，而是"打不出来的东西不许打"**：
白名单 + 拒绝子串 + 截断三道防线，以及**默认关闭时输出必须与改动前逐字节一致**。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from recall.config import (
    LOG_EXTRA_ALLOWED_KEYS,
    LOG_EXTRA_MAX_LEN,
    LogExtrasFormatter,
    Settings,
    collect_log_extras,
    configure_logging,
)

LOGGER_NAME = "recall.test.extras"


def _record(**extra: Any) -> logging.LogRecord:
    """造一条带 ``extra`` 的日志记录。"""
    record = logging.LogRecord(
        name=LOGGER_NAME,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="evt",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _settings(tmp_path: Path, *, log_extras: bool) -> Settings:
    """造一份最小可用的 Settings（只关心日志相关字段）。"""
    return Settings(
        qdrant_url="http://127.0.0.1:6333",
        vault_path=None,
        registry_db=tmp_path / "registry.db",
        log_dir=tmp_path,
        hf_endpoint="https://hf-mirror.com",
        hf_hub_offline=True,
        mcp_stateless=True,
        api_keys={},
        watchdog_api_key=None,
        evidence_min_score=0.0,
        ingest_rate_limit=10,
        ingest_rate_window_s=60.0,
        mcp_tool_policy={},
        trusted_proxies=("127.0.0.1", "::1"),
        mcp_auth_mode="app",
        mcp_gateway_user="me",
        deepseek_api_key=None,
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        feishu_app_id=None,
        feishu_app_secret=None,
        host="127.0.0.1",
        port=8000,
        collection=None,
        log_to_file=False,
        log_extras=log_extras,
    )


# ── 白名单：只打登记过的键 ───────────────────────────────────────────────────


def test_only_whitelisted_keys_are_collected() -> None:
    extras = collect_log_extras(_record(trace_id="t1", latency_ms=12.5, 没登记的键="x"))

    assert extras == {"trace_id": "t1", "latency_ms": "12.5"}


def test_standard_record_attributes_are_never_rendered() -> None:
    """``levelname`` / ``pathname`` / ``msg`` 这些是标准属性，**不是** extra。"""
    extras = collect_log_extras(_record(trace_id="t1"))

    assert set(extras) == {"trace_id"}


def test_none_values_are_skipped() -> None:
    assert collect_log_extras(_record(trace_id=None, latency_ms=1.0)) == {"latency_ms": "1.0"}


def test_allowlist_is_not_empty_and_contains_the_keys_we_promised() -> None:
    """空白名单会让整个特性**静默失效**（所有日志都看不出差别），必须有断言。"""
    assert LOG_EXTRA_ALLOWED_KEYS
    for key in ("trace_id", "latency_ms", "stage", "evidence_count"):
        assert key in LOG_EXTRA_ALLOWED_KEYS


# ── 拒绝子串：第二道保险（宁可少打，不可泄露）────────────────────────────────


@pytest.mark.parametrize("key", ["api_key", "password", "openai_token", "client_secret", "cookie"])
def test_deny_substrings_block_even_if_somehow_allowlisted(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**即便某个敏感键误进了白名单，也必须被拒绝子串挡住** —— 这是双重保险的意义。"""
    monkeypatch.setattr("recall.config.LOG_EXTRA_ALLOWED_KEYS", LOG_EXTRA_ALLOWED_KEYS | {key})
    assert collect_log_extras(_record(**{key: "SEKRIT"})) == {}


def test_a_secret_value_never_reaches_the_formatted_line() -> None:
    line = LogExtrasFormatter("%(message)s", enabled=True).format(
        _record(trace_id="t1", api_key="SEKRIT", password="p@ss")
    )

    assert "trace_id=t1" in line
    assert "SEKRIT" not in line
    assert "p@ss" not in line


# ── 截断与整形 ───────────────────────────────────────────────────────────────


def test_long_values_are_truncated() -> None:
    rendered = collect_log_extras(_record(model="x" * 500))["model"]

    assert len(rendered) == LOG_EXTRA_MAX_LEN + 1  # 截断后补一个省略号
    assert rendered.endswith("…")


def test_newlines_are_collapsed_so_one_event_stays_one_line() -> None:
    """extra 里塞多行文本会让**一条日志变成多行**，破坏按行 grep 的一切。"""
    rendered = collect_log_extras(_record(reason="第一行\n第二行\t第三行"))["reason"]

    assert "\n" not in rendered and "\t" not in rendered
    assert "第一行 第二行 第三行" in rendered


# ── 开关语义：关掉时必须与改动前**逐字节一致** ───────────────────────────────


def test_disabled_formatter_is_byte_identical_to_the_base_formatter() -> None:
    """🔴 默认关闭 ⇒ 输出必须与改动前**一模一样**（否则就是悄悄改了所有日志的格式）。"""
    record = _record(trace_id="t1", latency_ms=1.0)
    plain = logging.Formatter("%(levelname)s %(message)s")
    ours = LogExtrasFormatter("%(levelname)s %(message)s", enabled=False)

    assert ours.format(record) == plain.format(record)


def test_enabled_formatter_appends_after_a_separator() -> None:
    line = LogExtrasFormatter("%(levelname)s %(message)s", enabled=True).format(
        _record(trace_id="t1", latency_ms=1.0)
    )

    assert line == "INFO evt | trace_id=t1 latency_ms=1.0"


def test_enabled_formatter_without_extras_adds_nothing() -> None:
    """没有 extra 的日志行**不该多一个空的分隔符**。"""
    line = LogExtrasFormatter("%(levelname)s %(message)s", enabled=True).format(_record())

    assert line == "INFO evt"


# ── 接线：配置真的装到了 handler 上（防被 basicConfig 的 format= 覆盖）───────


@pytest.mark.parametrize(("enabled", "expected"), [(True, "trace_id=t1"), (False, "trace_id")])
def test_configure_logging_wires_the_formatter(
    tmp_path: Path, enabled: bool, expected: str
) -> None:
    """🔴 **防的是 `basicConfig(format=…)` 会重建 Formatter、把我们装的覆盖掉**。

    所以 `configure_logging` 必须传 `format=None` 并**自己 `setFormatter`**；
    这条用例就是钉住那个细节 —— 否则"开关明明打开了却没效果"。
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging(_settings(tmp_path, log_extras=enabled), component="test_extras")
        handler = root.handlers[0]
        assert isinstance(handler.formatter, LogExtrasFormatter)

        line = handler.formatter.format(_record(trace_id="t1"))

        assert (expected in line) is enabled
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_writes_extras_to_the_file(tmp_path: Path) -> None:
    """端到端：写到磁盘上的那一行也要带 extra（真实消费者读的是文件）。"""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        settings = replace(_settings(tmp_path, log_extras=True), log_to_file=True)
        configure_logging(settings, component="test_extras_file")

        logging.getLogger(LOGGER_NAME).info("kb_answer.done", extra={"trace_id": "abc123"})
        for handler in root.handlers:
            handler.flush()

        content = (tmp_path / "test_extras_file.log").read_text(encoding="utf-8")
        assert "kb_answer.done" in content
        assert "trace_id=abc123" in content
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_from_env_reads_the_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RECALL_SKIP_DOTENV", "1")
    monkeypatch.setenv("RECALL_LOG_EXTRAS", "1")
    monkeypatch.setenv("RECALL_LOG_DIR", str(tmp_path))

    assert Settings.from_env().log_extras is True

    monkeypatch.setenv("RECALL_LOG_EXTRAS", "0")
    assert Settings.from_env().log_extras is False


def test_default_is_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """默认必须是**关**：打开会改变所有日志行格式，不能是默认行为。"""
    monkeypatch.setenv("RECALL_SKIP_DOTENV", "1")
    monkeypatch.setenv("RECALL_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("RECALL_LOG_EXTRAS", raising=False)

    assert Settings.from_env().log_extras is False
