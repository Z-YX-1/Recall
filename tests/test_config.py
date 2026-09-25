"""``recall.config`` 的环境变量契约（roadmap R-43）。

回归点：2026-09-23 真实 DSH 会话里 ``kb_search`` **每一次**都在 ~42s 后
``ConnectTimeout``——根因不是 Qdrant（``kb_stats`` 一直正常），而是
transformers 装载 tokenizer 时回连 HF Hub 拉 ``chat_template.jinja`` 清单。
修法是把 ``HF_HUB_OFFLINE=1`` 提升为配置项，且必须在任何模型加载前写进
``os.environ``（tech.md §12 的"先建配置、后加载模型"顺序）。
"""

from __future__ import annotations

import os

import pytest

from recall.config import (
    DEFAULT_HF_HUB_OFFLINE,
    DEFAULT_MCP_STATELESS,
    Settings,
    parse_api_keys,
)


def _env(name: str) -> str | None:
    """读进程环境变量（断言"已写进 ``os.environ``"用）。"""
    return os.environ.get(name)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """把两个 HF 开关清干净，避免宿主机环境与**同进程前一个用例**串味。

    ⚠️ 必须清：``from_env`` 用 ``os.environ.setdefault`` 落盘，setdefault 只对
    "本次运行尚未设置"的值生效，于是前一个用例留下的 ``HF_HUB_OFFLINE=0``
    会静默污染后一个用例（2026-09-23 实测：不加这行，默认用例会读到 ``0``）。
    """
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)


def test_default_is_offline_and_written_to_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认离线：Settings 与 ``os.environ`` 同时落地，且默认值是显式常量。"""
    assert DEFAULT_HF_HUB_OFFLINE is True

    settings = Settings.from_env()

    assert settings.hf_hub_offline is True
    assert settings.hf_hub_offline is DEFAULT_HF_HUB_OFFLINE
    # 关键：必须在模型加载前就能被 transformers / huggingface_hub 看见
    assert _env("HF_HUB_OFFLINE") == "1"


def test_explicit_env_var_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HF_HUB_OFFLINE=0`` 是下载新模型的逃生口，绝不能被默认值覆盖。"""
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")

    settings = Settings.from_env()

    assert settings.hf_hub_offline is False
    assert _env("HF_HUB_OFFLINE") == "0"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        (" on ", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
    ],
)
def test_boolean_lexicon(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    """词法与 ``log_to_file`` 保持一套：0/false/no/off 为假，1/true/yes/on 为真。"""
    monkeypatch.setenv("HF_HUB_OFFLINE", raw)

    assert Settings.from_env().hf_hub_offline is expected


def test_unrecognized_value_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """无法识别的取值退回默认（离线），不做"看起来像真"的猜测。"""
    monkeypatch.setenv("HF_HUB_OFFLINE", "maybe")

    assert Settings.from_env().hf_hub_offline is DEFAULT_HF_HUB_OFFLINE


def test_hf_endpoint_still_written_for_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """镜像端点与离线开关是同一条"模型加载前生效"的约束，不能被这次改动挤掉。"""
    settings = Settings.from_env()

    assert _env("HF_ENDPOINT") == settings.hf_endpoint


def test_mcp_is_stateless_by_default() -> None:
    """MCP 默认无会话：状态化会话会在空闲 30 分钟 / 服务重启后变成 404（R-44）。"""
    assert DEFAULT_MCP_STATELESS is True

    assert Settings.from_env().mcp_stateless is True


def test_mcp_stateless_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """需要服务端推送（notifications/SSE 续传）时，显式回到状态化模式。"""
    monkeypatch.setenv("RECALL_MCP_STATELESS", "0")

    assert Settings.from_env().mcp_stateless is False


# --------------------------------------------------------------- R-43b：导入顺序


def test_offline_is_patched_into_already_imported_huggingface_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-43b 回归点：库先于配置导入时，``from_env`` 必须把**库里的常量**也改过来。

    ``huggingface_hub.constants.HF_HUB_OFFLINE`` 是 import 时冻结的常量；
    而 ``recall.api`` 的导入顺序恰好是"先 HF 栈、后 from_env"，只设环境变量无效
    （2026-09-24 实测：模型仍回连 Hub，``kb_search`` 42s 超时）。
    """
    hf_constants = pytest.importorskip("huggingface_hub.constants")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setattr(hf_constants, "HF_HUB_OFFLINE", False)

    settings = Settings.from_env()

    assert settings.hf_hub_offline is True
    assert hf_constants.HF_HUB_OFFLINE is True, "库里缓存的常量没被同步 ⇒ 仍会联网"


def test_offline_patch_follows_the_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HF_HUB_OFFLINE=0``（下载新模型的逃生口）同样要同步到库常量。"""
    hf_constants = pytest.importorskip("huggingface_hub.constants")
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setattr(hf_constants, "HF_HUB_OFFLINE", True)

    Settings.from_env()

    assert hf_constants.HF_HUB_OFFLINE is False


def test_importing_recall_bootstraps_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """结构性保证：包一被导入就先建配置，"先配置、后加载模型"由导入顺序兜住。"""
    from recall import _bootstrap_environment

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)

    _bootstrap_environment()

    assert _env("HF_HUB_OFFLINE") == "1"
    assert _env("HF_ENDPOINT")


# --------------------------------------------------------------------------- API key 表（R-40）


def test_api_keys_default_to_empty_meaning_no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认空 key 表 = 不鉴权（S1 语义）；``auth_enabled`` 是中间件的开关。"""
    monkeypatch.setenv("RECALL_API_KEYS", "")

    settings = Settings.from_env()

    assert settings.api_keys == {}
    assert settings.auth_enabled is False


def test_api_keys_are_parsed_from_token_user_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    """``token:user`` 逗号分隔；允许条目间有空白（从 .env 抄过来常带空格）。"""
    monkeypatch.setenv("RECALL_API_KEYS", " tok-a:me , tok-b:alice ")

    settings = Settings.from_env()

    assert settings.api_keys == {"tok-a": "me", "tok-b": "alice"}
    assert settings.auth_enabled is True


def test_api_keys_skip_empty_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """多余逗号（``a:b,,c:d``）不应让整个配置失败。"""
    monkeypatch.setenv("RECALL_API_KEYS", "tok-a:me,,tok-b:alice,")

    assert Settings.from_env().api_keys == {"tok-a": "me", "tok-b": "alice"}


@pytest.mark.parametrize("raw", ["no-colon", ":me", "tok:", "", "  "])
def test_api_keys_reject_malformed_entries(raw: str) -> None:
    """格式错误**立即抛错**——配置问题必须在启动时大声失败。

    注意 ``""`` / 纯空白会被 ``from_env`` 之外的空串语义跳过，这里直接测解析函数，
    因此只有真正不合法的写法才抛。
    """
    if not raw.strip():
        assert parse_api_keys(raw) == {}
        return
    with pytest.raises(ValueError, match="token:user"):
        parse_api_keys(raw)


def test_api_keys_reject_duplicates_without_echoing_the_token() -> None:
    """重复 token 要报错，但错误信息**只回显前 4 位**（错误会进日志）。"""
    with pytest.raises(ValueError) as excinfo:
        parse_api_keys("secret-token-1:me,secret-token-1:alice")

    assert "secr" in str(excinfo.value)
    assert "secret-token-1" not in str(excinfo.value)


def test_audit_log_path_sits_next_to_the_structured_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """审计文件与结构化日志同目录、独立文件名（便于单独轮转）。"""
    monkeypatch.setenv("RECALL_LOG_DIR", "D:/tmp/recall-logs")

    assert Settings.from_env().audit_log_path.name == "audit.jsonl"


def test_watchdog_api_key_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    """未启用鉴权时 watcher 不需要 key ⇒ 默认 ``None``（roadmap R-38）。"""
    monkeypatch.delenv("RECALL_WATCHDOG_API_KEY", raising=False)

    assert Settings.from_env().watchdog_api_key is None


def test_watchdog_api_key_is_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """启用鉴权后，watcher 用这把 token 调 ``POST /kb/ingest``。"""
    monkeypatch.setenv("RECALL_WATCHDOG_API_KEY", "tok-watchdog")

    assert Settings.from_env().watchdog_api_key == "tok-watchdog"


# ----------------------------------------------------------------- 证据门槛（R-42 阶段 1）


def test_evidence_threshold_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 ``0.0`` = **不启用**：门槛会改变可观察行为，默认必须"什么都不改变"。"""
    monkeypatch.delenv("RECALL_EVIDENCE_MIN_SCORE", raising=False)

    assert Settings.from_env().evidence_min_score == 0.0


def test_evidence_threshold_is_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """测量给出的建议值是 0.58（空档中点），必须能被配置进去。"""
    monkeypatch.setenv("RECALL_EVIDENCE_MIN_SCORE", "0.58")

    assert Settings.from_env().evidence_min_score == pytest.approx(0.58)


@pytest.mark.parametrize("raw", ["abc", "-0.1", "1.5", "2"])
def test_evidence_threshold_rejects_invalid_values(raw: str) -> None:
    """非法值**启动即失败**，不静默退化成"没有门槛"（那会让人以为门槛开着）。"""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("RECALL_EVIDENCE_MIN_SCORE", raw)
    try:
        with pytest.raises(ValueError, match="RECALL_EVIDENCE_MIN_SCORE"):
            Settings.from_env()
    finally:
        monkeypatch.undo()


# ------------------------------------------------------------- 写端点限流（R-40 补记）


def test_ingest_rate_limit_defaults_to_ten_per_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 ``10/60``：远高于任何合理本地用量，但挡得住跑飞的脚本。"""
    monkeypatch.delenv("RECALL_INGEST_RATE_LIMIT", raising=False)

    settings = Settings.from_env()

    assert (settings.ingest_rate_limit, settings.ingest_rate_window_s) == (10, 60.0)


def test_ingest_rate_limit_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """支持 ``N/W`` 形式（次数/秒数）。"""
    monkeypatch.setenv("RECALL_INGEST_RATE_LIMIT", "3/15")

    settings = Settings.from_env()

    assert (settings.ingest_rate_limit, settings.ingest_rate_window_s) == (3, 15.0)


@pytest.mark.parametrize("raw", ["0", "off", "OFF", "none", "disable"])
def test_ingest_rate_limit_can_be_disabled(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式关闭的几种写法都要认（运维手写配置时不该踩坑）。"""
    monkeypatch.setenv("RECALL_INGEST_RATE_LIMIT", raw)

    assert Settings.from_env().ingest_rate_limit == 0


@pytest.mark.parametrize("raw", ["abc", "10", "10/0", "0/60", "-1/5", "10/x"])
def test_ingest_rate_limit_rejects_invalid_values(raw: str) -> None:
    """格式错误**启动即失败**——静默不限制比报错危险得多。"""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("RECALL_INGEST_RATE_LIMIT", raw)
    try:
        with pytest.raises(ValueError, match="RECALL_INGEST_RATE_LIMIT"):
            Settings.from_env()
    finally:
        monkeypatch.undo()


# --------------------------------------------------------- MCP 工具白名单（R-39 前置件）


def test_mcp_tool_policy_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认空 = 不启用（所有身份都能用全部工具），默认值必须"什么都不改变"。"""
    monkeypatch.delenv("RECALL_MCP_TOOL_POLICY", raising=False)

    assert Settings.from_env().mcp_tool_policy == {}


def test_mcp_tool_policy_is_parsed_for_multiple_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """``用户:工具1|工具2``，逗号分隔多条 —— 公网身份与本机身份可以各拿各的工具。"""
    monkeypatch.setenv(
        "RECALL_MCP_TOOL_POLICY", " coze:kb_search|kb_answer , me:kb_stats "
    )

    rules = Settings.from_env().mcp_tool_policy

    assert rules == {
        "coze": frozenset({"kb_search", "kb_answer"}),
        "me": frozenset({"kb_stats"}),
    }


@pytest.mark.parametrize("raw", ["no-colon", ":kb_search", "coze:", "coze:||"])
def test_mcp_tool_policy_rejects_malformed_entries(raw: str) -> None:
    """格式错误启动即抛 —— 白名单写错却静默不生效，会让人以为"已经挡住了"。"""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("RECALL_MCP_TOOL_POLICY", raw)
    try:
        with pytest.raises(ValueError, match="RECALL_MCP_TOOL_POLICY"):
            Settings.from_env()
    finally:
        monkeypatch.undo()


def test_mcp_tool_policy_rejects_duplicate_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一用户写两遍 ⇒ 报错（否则后者覆盖前者，与人的直觉不符）。"""
    monkeypatch.setenv("RECALL_MCP_TOOL_POLICY", "coze:kb_search,coze:kb_stats")

    with pytest.raises(ValueError, match="重复用户"):
        Settings.from_env()
