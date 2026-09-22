"""DeepSeek 生成客户端（tech.md §2 LLM 行；roadmap R-29）。

只服务**胖端点**（``kb_answer``）——瘦核心 ``kb_search`` 绝不调 LLM（tech.md §15 决策 2）。

- 走 OpenAI 兼容接口（tech.md §2：依赖 ``openai``）；
- **JSON 模式**约束 ``{"answer", "citations"}``，防引用编号幻觉（code_standards §8）；
- 统一超时 + 指数退避重试（code_standards §10）；
- 密钥只从配置读，**绝不入日志/registry/payload**（code_standards §12）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from openai import AsyncOpenAI

from recall.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_OUTPUT_TOKENS = 2048
RETRY_BASE_DELAY = 0.5
"""指数退避基数（秒）：第 n 次失败后等待 ``RETRY_BASE_DELAY * 2**(n-1)``（code_standards §10）。"""

SYSTEM_PROMPT = (
    "你是 Recall 知识库的回答器，只依据用户给出的证据作答。"
    "输出必须是 json 对象，包含 answer 与 citations 两个字段。"
)
"""系统提示词。⚠️ JSON 模式要求提示词里出现 "json" 字样。"""


class LlmError(RuntimeError):
    """LLM 调用失败（网络、鉴权、返回体不合契约）。"""


class LlmNotConfiguredError(LlmError):
    """未配置 ``DEEPSEEK_API_KEY``，无法生成回答。"""


class DeepSeekClient:
    """DeepSeek Chat Completions 的异步封装（JSON 模式）。"""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        """配置客户端（不发起连接）。

        Args:
            api_key: DeepSeek API key；``None`` 表示未配置。
            base_url: OpenAI 兼容接口地址。
            model: 生成用模型名。
            timeout: 单次请求超时秒数。
            max_retries: 最大尝试次数（含首次）。
        """
        self.model = model
        self._base_url = base_url
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = (
            AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
            if api_key
            else None
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> DeepSeekClient:
        """按运行配置构造客户端。

        Args:
            settings: 运行配置（读 ``deepseek_api_key`` / ``base_url`` / ``model``）。

        Returns:
            配置完成的客户端；无 key 时 :attr:`available` 为 ``False``。
        """
        return cls(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
        )

    @property
    def available(self) -> bool:
        """是否已配置 API key。"""
        return self._client is not None

    async def complete_json(
        self,
        prompt: str,
        *,
        system: str = SYSTEM_PROMPT,
        max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> dict[str, Any]:
        """以 JSON 模式生成结构化回答。

        Args:
            prompt: 组装好的证据 + 问题提示词。
            system: 系统提示词。
            max_tokens: 输出 token 上限。

        Returns:
            解析后的 JSON 对象。

        Raises:
            LlmNotConfiguredError: 未配置 API key。
            LlmError: 重试耗尽仍失败，或返回体不是合法 JSON 对象。
        """
        if self._client is None:
            raise LlmNotConfiguredError("未配置 DEEPSEEK_API_KEY，无法生成回答")

        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await self._call_once(prompt, system=system, max_tokens=max_tokens)
            except LlmError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络/服务端错误统一退避重试
                last_error = exc
                if attempt == self._max_retries:
                    break
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "llm.retry",
                    extra={
                        "model": self.model,
                        "attempt": attempt,
                        "delay_s": delay,
                        "error": str(exc)[:300],
                    },
                )
                await asyncio.sleep(delay)
        raise LlmError(
            f"DeepSeek 调用失败（{self._max_retries} 次尝试）：{last_error}"
        ) from last_error

    async def _call_once(self, prompt: str, *, system: str, max_tokens: int) -> dict[str, Any]:
        assert self._client is not None  # available 已在入口校验
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=max_tokens,
        )
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise LlmError("DeepSeek 返回空内容")
        try:
            payload = json.loads(content)
        except ValueError as exc:
            raise LlmError(f"DeepSeek 返回体不是合法 JSON：{content[:200]}") from exc
        if not isinstance(payload, dict):
            raise LlmError(f"DeepSeek 返回体不是 JSON 对象：{content[:200]}")
        usage = response.usage
        logger.info(
            "llm.completed",
            extra={
                "model": self.model,
                "prompt_chars": len(prompt),
                "completion_chars": len(content),
                "total_tokens": usage.total_tokens if usage is not None else None,
            },
        )
        return payload
