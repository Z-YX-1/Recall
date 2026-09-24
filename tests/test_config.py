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

from recall.config import DEFAULT_HF_HUB_OFFLINE, DEFAULT_MCP_STATELESS, Settings


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
