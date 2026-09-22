"""DeepSeek 客户端必测项（code_standards §10 重试/超时、§8 JSON 模式；roadmap R-29）。

不产生真实 API 调用：把底层 ``AsyncOpenAI`` 换成桩，专测**重试与返回体解析**——
这两块逻辑此前完全没有测试覆盖，而它们是"生成失败"类问题的唯一防线。

测试直接替换 ``DeepSeekClient._client``（用 ``cast`` 让 mypy 通过），
这样生产代码不必为测试留注入口子。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from recall.config import Settings
from recall.llm import DeepSeekClient, LlmError, LlmNotConfiguredError


class _FakeCompletions:
    """按预设序列依次返回结果或抛异常（用完后重复最后一项）。"""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = outcomes
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeOpenAI:
    """只实现 ``chat.completions.create`` 的假客户端。"""

    def __init__(self, outcomes: list[object]) -> None:
        self.completions = _FakeCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)


def _response(content: str | None, *, total_tokens: int = 42) -> Any:
    """构造一个最小可用的 ChatCompletion 形状。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(total_tokens=total_tokens),
    )


def _client(outcomes: list[object], *, max_retries: int = 3) -> tuple[DeepSeekClient, _FakeOpenAI]:
    """装好桩的客户端 + 桩本身（便于断言调用次数与入参）。"""
    client = DeepSeekClient(
        api_key="test-key",
        base_url="http://stub.invalid",
        model="stub-model",
        max_retries=max_retries,
    )
    fake = _FakeOpenAI(outcomes)
    client._client = cast("Any", fake)
    return client, fake


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """把退避基数压到 0，避免用例真的等 0.5s + 1s（生产默认值仍是 0.5s）。"""
    monkeypatch.setattr("recall.llm.RETRY_BASE_DELAY", 0.0)


def test_available_reflects_api_key() -> None:
    assert DeepSeekClient(api_key="k", base_url="u", model="m").available is True
    assert DeepSeekClient(api_key=None, base_url="u", model="m").available is False
    assert DeepSeekClient(api_key="", base_url="u", model="m").available is False


async def test_complete_json_returns_parsed_object() -> None:
    client, fake = _client([_response('{"answer": "结论 [1]", "citations": [1]}')])

    payload = await client.complete_json("提示词")

    assert payload == {"answer": "结论 [1]", "citations": [1]}
    assert fake.completions.calls == 1


async def test_complete_json_uses_json_mode_and_deterministic_temperature() -> None:
    client, fake = _client([_response("{}")])

    await client.complete_json("提示词", max_tokens=512)

    sent = fake.completions.last_kwargs
    assert sent["response_format"] == {"type": "json_object"}  # code_standards §8
    assert sent["temperature"] == 0.0
    assert sent["max_tokens"] == 512
    assert sent["model"] == "stub-model"
    assert sent["messages"][0]["role"] == "system"
    assert sent["messages"][1] == {"role": "user", "content": "提示词"}


async def test_transient_errors_are_retried_then_succeed() -> None:
    client, fake = _client(
        [RuntimeError("连接超时"), RuntimeError("502"), _response('{"answer": "ok"}')]
    )

    payload = await client.complete_json("提示词")

    assert payload == {"answer": "ok"}
    assert fake.completions.calls == 3  # 两次失败 + 一次成功


async def test_retries_are_exhausted_into_llm_error() -> None:
    client, fake = _client([RuntimeError("一直失败")], max_retries=3)

    with pytest.raises(LlmError) as excinfo:
        await client.complete_json("提示词")

    assert "3 次尝试" in str(excinfo.value)
    assert fake.completions.calls == 3


async def test_invalid_json_is_not_retried() -> None:
    """返回体不是合法 JSON 属「契约违约」，快速失败而不是继续烧钱重试。"""
    client, fake = _client([_response("这不是 JSON"), _response('{"answer": "x"}')])

    with pytest.raises(LlmError) as excinfo:
        await client.complete_json("提示词")

    assert "不是合法 JSON" in str(excinfo.value)
    assert fake.completions.calls == 1


async def test_non_object_json_is_rejected() -> None:
    client, _ = _client([_response("[1, 2, 3]")])

    with pytest.raises(LlmError) as excinfo:
        await client.complete_json("提示词")

    assert "不是 JSON 对象" in str(excinfo.value)


async def test_empty_content_is_rejected() -> None:
    client, _ = _client([_response(None)])

    with pytest.raises(LlmError) as excinfo:
        await client.complete_json("提示词")

    assert "空内容" in str(excinfo.value)


async def test_missing_api_key_raises_before_any_call() -> None:
    client = DeepSeekClient(api_key=None, base_url="u", model="m")

    with pytest.raises(LlmNotConfiguredError):
        await client.complete_json("提示词")


def test_from_settings_reads_deepseek_config() -> None:
    settings = Settings(
        qdrant_url="http://127.0.0.1:6333",
        vault_path=None,
        registry_db=Path("registry.db"),
        log_dir=Path("logs"),
        hf_endpoint="https://hf-mirror.com",
        deepseek_api_key="secret",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        host="127.0.0.1",
        port=8000,
        collection=None,
        log_to_file=False,
    )

    client = DeepSeekClient.from_settings(settings)

    assert client.available is True
    assert client.model == "deepseek-chat"
