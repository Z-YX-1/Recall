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
from collections.abc import Mapping
from typing import Any

from openai import AsyncOpenAI

from recall.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_OUTPUT_TOKENS = 3072
"""输出 token 上限。⚠️ 不能太小：实测 2048 时模型复述证据会把 JSON 截断，
整段回答直接作废（见 :data:`recall.assemble.PROMPT_FOOTER` 的说明）。"""

SELF_CHECK_MAX_TOKENS = 200
"""自检调用的输出上限（roadmap R-47 方案 D）：只回 ``answered`` + 一句 ``sentence``，
给足即可 ⇒ 实测单次约 **0.72s**（对比完整生成段约 1.31s）。"""

RETRY_BASE_DELAY = 0.5
"""指数退避基数（秒）：第 n 次失败后等待 ``RETRY_BASE_DELAY * 2**(n-1)``（code_standards §10）。"""

SYSTEM_PROMPT = (
    "你是 Recall 知识库的回答器，只依据用户给出的证据作答，输出必须是 json 对象，"
    "包含 answer 与 citations 两个字段。\n"
    "硬性要求：\n"
    "1. answer 不超过 300 字，直接给结论，不要复述证据原文；写完立即闭合 JSON。\n"
    "2. 证据与问题确实无关时，answer 只写「笔记里没有相关内容」、citations 留空；"
    "只要证据**解释了**问题就必须作答。\n"
    "3. **第 2 条优先**：只要证据里有**直接回答**问题所问那件事的句子，就必须作答，"
    "**不得**使用下面的拒答句。\n"
    "   只有当证据**整体上**都只是提到它、而没有解释它时（例如它只出现在一句话、一个列表、"
    "一张对照表或一道选择题的选项里），answer 才写"
    "「笔记里只提及该术语、没有解释」，**不得**把它展开成原理说明，**不得**用你自己的知识补充；"
    "可以提示用户联网搜索。\n"
    "   ⚠️ 三点必须分清：① 判断时锚定**问题真正在问的那个词或做法**，"
    "不要换成证据里顺带出现的其它术语；② 证据讲的是**同一主题下的另一种做法 / 另一条路线**"
    "（而不是问题问的那个）时，**不算**解释了问题，仍属「只提及」；"
    "③ 没给出某个术语的定义 ≠ 问题没有被回答 —— 若证据解释了问题所问的"
    "影响 / 关系 / 做法，就算作已解释，必须照第 2 条作答。\n"
    "4. citations 只能使用证据中出现过的编号。\n"
    "5. answer 里**不要使用英文双引号**，需要引号时一律用中文「」——"
    "英文双引号会提前结束 JSON 字符串，让整段回答作废。"
)
"""系统提示词。

第 3 条 = roadmap **R-47**（2026-09-26 项目工程师批准）：「提了名没解释」是"笔记里没有"的
**第二种情形** —— 库里**确实有**提到该术语的 chunk，精排**合理地**给它高分
（实测 YaRN 0.902，比 30 道笔记内题里的 28 道都高）⇒ 分数门槛**在原理上拒不掉**它，
只能在生成侧拦住"拿提名当解释"的硬答。判据是"证据**解释了**吗"，不是"证据相不相关"。
对照：`skill/recall-assembly.md` 的「情形 B」（Agent 侧同一规则）。
⚠️ 胖端点**没有联网能力**：它只能声明"笔记只提及、未解释"，联网检索由调用方（DSH Agent）做。

🔴 **2026-10-03 修订（项目工程师拍板 A+B）**：实测 —— 问「切分粒度对召回的影响是什么样的」，
**检索是对的**（最高分证据 score 0.9482 的原文即答案），但胖端点**跳过它**、只引用了
"后厨切菜的类比"与一道"无答案的面试题"，然后套用本条第 3 句拒答；**三次连问给出两种不同
答案**（`temperature=0.0` 也漂）。机制上有三处松动，已按拍板处理：

- **A（本条）**：给第 3 条加**从属关系与两条澄清** —— ① 第 2 条优先，只有证据**整体上**只是提名
  才用拒答句；②「问题里的术语」= **问题真正在问的那个词**，不是证据里顺带出现的其它术语；
  ③ **没给出某个术语的定义 ≠ 问题没有被回答**。原措辞的触发条件太松：模型会把证据里的
  另一个术语（`chunk_size`）当成"问题里的术语"，再以"没给定义"为由整段拒答。
- **B（去重）**：`assemble.py::FIDELITY_RULES` 里那份**已删除** —— 生产代码两条都发，
  等于对模型说两遍，把拒答规则放大到压过第 2 条。现只在本系统提示词里出现一次
  （本条上方"拒答规则必须放系统提示词"的教训本就指向这里）。Agent 侧副本不受影响。
- 回归口径：用 `tools/diagnose_answer.py` 拿**同一批题**验 —— 既要修好"有解释却拒答"，
  也**不能**把 R-47 当初治好的"真·只提及却硬答"（如 YaRN）放回来。

⚠️ 三条经验（2026-09-23 实测，全部会导致"整段回答作废"）：

- JSON 模式要求提示词里出现 "json" 字样；
- **长度与拒答规则必须放系统提示词**：只写在用户提示词末尾时模型会忽略——
  实测出现"复述整段证据 ⇒ 输出被 ``max_tokens`` 截断 ⇒ JSON 不完整"，以及
  "证据明明相关却答'没有相关内容'"；
- **必须禁止 answer 里出现英文双引号**：模型会在 JSON 字符串里塞未转义的 ``"``
  （实测原文：``把不可计算的"意思"变成"坐标"``），字符串被提前截断，
  ``json.loads`` 报 ``Expecting ',' delimiter``，整段回答作废。
"""


RETRY_INSTRUCTION = (
    "\n\n【补充】已用裁判核对：上面的证据**确实直接回答了**该问题。"
    "请据此按第 2 条作答，**不得**再使用「只提及 / 没有解释」这类拒答句，"
    "并在 answer 中指出回答该问题的那句话所对应的证据编号。"
)
"""自检判定"证据确实回答了问题"后，追加到原 prompt 末尾的指令（roadmap R-47 方案 D）。"""

DECLINE_MARKERS: tuple[str, ...] = (
    "只提及",
    "仅提及",
    "只提到",
    "仅提到",
    "只是提到",
    "未解释",
    "没有解释",
    "未展开",
    "没有展开",
    "未给出",
    "没有给出",
    "未涉及",
    "只列出",
    "仅列出",
    "只点名",
    "仅点名",
    "笔记里只",
    "笔记中只",
    "笔记里没有相关内容",
)
"""答案里出现任一 ⇒ 判为**拒答**，需要走一次"自检"（roadmap R-47 方案 D，2026-10-03 拍板）。

与 :mod:`tools.verify_r47` 的判据**同源**（该脚本据此表导入），避免两处漂移。
⚠️ 它是**启发式**：覆盖常见措辞、不是分类器 —— 见 `tools/verify_r47.py` 里那次假阴性记录。
⚠️ 判为拒答**不等于**答案错：真·只提及的题**就该**拒答，自检会把它放行回拒答。
"""

SELF_CHECK_SYSTEM_PROMPT = (
    "你是检索质量裁判，只回答一个问题：给定的证据有没有**直接回答**用户的问题。"
    "输出必须是 json 对象，含 answered（true / false）与 sentence"
    "（证据里直接回答该问题的那句话原文；没有就填空字符串）两个字段。\n"
    "判断标准：证据里有没有一句话**直接回答**了问题所问的那件事。"
    "若证据只是**提到**问题里的名词、或讲的是**同一主题下的另一种做法 / 路线**，"
    "answered 取 false。\n"
    "answer 里不要使用英文双引号，需要引号时一律用「」。"
)
"""自检用系统提示词：**只问一件事**（roadmap R-47 方案 D）。

为什么要拆成两次：生成那一次要让模型同时干"理解问题、判断证据、决定答不答"三件事，
任一环节抖一下就误拒（实测同一输入 `temperature=0.0` 也会在"答 / 拒"之间横跳）。
拆开之后只判"这段证据有没有直接回答"，实测判得准 —— 2026-10-03 样例正确挑出了回答句。
"""


def looks_like_decline(answer: str) -> bool:
    """答案是否**像**拒答（命中 :data:`DECLINE_MARKERS` 任一）。

    Args:
        answer: 生成出来的答案正文。

    Returns:
        命中任一标记词为 ``True``。
    """
    return any(marker in answer for marker in DECLINE_MARKERS)


def build_self_check_prompt(question: str, evidence_text: str) -> str:
    """拼自检用的提示词：问题 + 一段证据（通常取最高分那条）。"""
    return f"问题：{question}\n\n证据：\n{evidence_text}"


def parse_answered(payload: Mapping[str, Any]) -> bool:
    """从自检返回体里取 ``answered``（容忍布尔与字符串两种写法）。

    Args:
        payload: 自检返回的 JSON 对象。

    Returns:
        裁判是否认为证据直接回答了问题。

    Raises:
        ValueError: 缺 ``answered`` 或类型不可识别 —— 调用方须**降级**为"保留原答案"，
            绝不能因为自检出问题就把用户的回答弄丢。
    """
    raw = payload.get("answered")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "yes", "y", "1", "是"}
    raise ValueError(f"自检返回体缺少可识别的 answered 字段：{raw!r}")


class LlmError(RuntimeError):
    """LLM 调用失败（网络、鉴权、返回体不合契约）。"""


class LlmNotConfiguredError(LlmError):
    """未配置 ``DEEPSEEK_API_KEY``，无法生成回答。"""


def parse_json_object(content: str) -> dict[str, Any] | None:
    """从模型返回文本里取出 JSON 对象；取不到返回 ``None``。

    先直接 ``json.loads``；失败则**在文本里扫描第一个配对完整的 ``{...}``** 再解析。
    解析一律用 ``strict=False``——DeepSeek 的 JSON 模式会在字符串里塞**未转义的换行**，
    这对标准解析器是硬错误，却是模型的常见输出（2026-09-23 实测：303 字符的回答体
    看着完整，却因一个裸换行导致整段作废）。

    这一步是必要的容错：JSON 模式不是硬保证——实测 DeepSeek 还会在 JSON 前后附带
    说明文字，直接 ``json.loads`` 会因「多余数据」报错，而内容其实完好可用。

    ⚠️ 只救**完整**对象：扫描要求花括号配对且字符串/转义被正确跳过，
    因此被 ``max_tokens`` 截断的半截 JSON 仍然返回 ``None``（不会把半截答案当好答案）。

    Args:
        content: 模型返回的原始文本。

    Returns:
        解析出的字典；不是对象或无法解析时返回 ``None``。
    """
    stripped = content.strip()
    try:
        payload = json.loads(stripped, strict=False)
        return payload if isinstance(payload, dict) else None
    except ValueError:
        pass

    start = stripped.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(stripped)):
            char = stripped[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(stripped[start : index + 1], strict=False)
                    except ValueError:
                        break
                    return candidate if isinstance(candidate, dict) else None
        start = stripped.find("{", start + 1)
    return None


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

    async def verify_evidence_answers(self, question: str, evidence_text: str) -> bool:
        """自检：这段证据有没有**直接回答**该问题（roadmap R-47 方案 D，2026-10-03 拍板）。

        它是"拒答闸门"的第二道：生成侧误判成"只提及未解释"时，用一次**只问一件事**的
        调用复核；复核说不算回答，才允许把拒答句交给用户。

        Args:
            question: 用户原问题。
            evidence_text: 待核对的证据正文（调用方通常取**最高分那条**）。

        Returns:
            ``True`` = 裁判认为证据直接回答了问题（调用方应带 :data:`RETRY_INSTRUCTION`
            重新生成一次）。

        Raises:
            LlmNotConfiguredError: 未配置 API key。
            LlmError: 调用失败或返回体不可解析。
            ValueError: 返回体缺 ``answered`` —— 调用方须**降级**为"保留原答案"。
        """
        payload = await self.complete_json(
            build_self_check_prompt(question, evidence_text),
            system=SELF_CHECK_SYSTEM_PROMPT,
            max_tokens=SELF_CHECK_MAX_TOKENS,
        )
        if not parse_answered(payload):
            return False
        # **自洽检查**：裁判说"回答了"就必须**引出那句话**；引不出就按"没回答"处理。
        # 动机（2026-10-03 实测）：放到 10 题 `mentioned` 组上跑，裁判宽松放行了一题（GraphRAG），
        # 而它本该拒答 —— 能引原文才算真的"直接回答"，这是一道几乎零成本的兜底。
        return bool(str(payload.get("sentence") or "").strip())

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
        payload = parse_json_object(content)
        if payload is None:
            raise LlmError(
                "DeepSeek 返回体不是合法 JSON "
                f"（{len(content)} 字符，首 120：{content[:120]!r}，"
                f"尾 80：{content[-80:]!r}）"
            )
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
