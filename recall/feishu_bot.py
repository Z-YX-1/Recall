"""飞书交互入口：长连接机器人（roadmap R-49c）。

数据流向：飞书用户发消息 → **长连接**事件 → 本进程 → ``POST /kb/answer`` → 消息卡片回复。
**飞书只做入口**，笔记与向量库一概不经过它（与已作废的 R-41"内容源"定义相反）。

三条硬约束（全部来自 R-49 的二轮核查，见 ``tech.md`` §18 决策 19）：

1. **先 ACK、后异步**：SDK 把事件 handler 的**返回值写回 socket 当作确认**，
   异常则回 500。而 ``/kb/answer`` 要调 DeepSeek（数秒~十几秒）⇒ handler 里
   **绝不能同步等待**，否则堵死心跳、被服务端判失联并反复重连。
   实现：:meth:`FeishuBot.handle_event` 只做"解析 → 去重 → 投递线程池"就返回。
2. **``event_id`` 幂等**：服务端下发的 ``ReconnectCount=-1`` 意味着**无限重连**，
   重连会重放历史事件 ⇒ 不去重就会把同一个问题答两遍。
3. **出站限流**：飞书发消息有频控（企业自建应用约 5 QPS）⇒ 复用
   :class:`recall.ratelimit.SlidingWindowLimiter`。

**不自行装载模型**（R-23b 的"不双份 bge-m3"）：本进程只用 httpx 调本机 API，
因此**不占显存** —— 这正是飞书入口与"再加一个摄取来源"的根本差别。

**为什么回复不用 SDK、而用 httpx 直写**：SDK 的价值在长连接
（端点发现 / 帧重组 / ping-pong / 指数退避重连 / 内置鉴权，手写不现实），
而回复只是一个 POST。自己写换来三件事：结构化错误（飞书是 **HTTP 200 + ``code != 0``**，
不检查就会"静默不回复"）、可注入 :class:`httpx.MockTransport` 的测试、
以及不必在异步路径里调用同步的 SDK 方法。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx

from recall.config import Settings, configure_logging
from recall.lark_md import render_card_body, to_lark_md
from recall.models import AnswerResult
from recall.ratelimit import SlidingWindowLimiter

logger = logging.getLogger(__name__)

TENANT_TOKEN_PATH: Final[str] = "/open-apis/auth/v3/tenant_access_token/internal"
REPLY_PATH_TEMPLATE: Final[str] = "/open-apis/im/v1/messages/{message_id}/reply"

DEFAULT_FEISHU_BASE_URL: Final[str] = "https://open.feishu.cn"
DEFAULT_API_URL: Final[str] = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_S: Final[float] = 60.0
"""``/kb/answer`` 要调 DeepSeek，超时给足；飞书接口自身很快，共用同一超时无妨。"""

TOKEN_REFRESH_MARGIN_S: Final[float] = 60.0
"""提前多少秒把 tenant token 视为过期（官方 ``expire`` 为 7200 秒）。"""

DEFAULT_DEDUP_CAPACITY: Final[int] = 1024
"""``event_id`` 记忆条数。人类提问速度下 1024 条足以覆盖任何一次重连重放窗口。"""

DEFAULT_OUTBOUND_RATE_LIMIT: Final[int] = 5
DEFAULT_OUTBOUND_RATE_WINDOW_S: Final[float] = 1.0
"""出站回复限流：5 条 / 秒（企业自建应用发消息的官方量级）。"""

CARD_TITLE: Final[str] = "Recall · 拾忆"
CARD_TEMPLATE: Final[str] = "blue"

SERVICE_UNAVAILABLE_TEXT: Final[str] = "Recall 服务暂时不可用（本机 API 未响应），请稍后再问一次。"

EXIT_OK: Final[int] = 0
EXIT_NOT_CONFIGURED: Final[int] = 2

_MENTION_RE: Final[re.Pattern[str]] = re.compile(r"@_user_\d+")
"""群聊里 @ 机器人时，正文会带 ``@_user_1`` 这类占位符 —— 提问前要剥掉。"""


class FeishuApiError(RuntimeError):
    """飞书接口返回了非 0 业务码（飞书是 **HTTP 200 + code != 0**，必须显式检查）。"""


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """一条可处理的用户消息（纯数据，便于单测）。

    Attributes:
        event_id: 事件唯一标识，用于**幂等去重**（重连会重放事件）。
        message_id: 消息标识，回复时用它（``.../messages/{id}/reply``）。
        text: 已剥掉 @ 占位符并去空白的提问正文。
    """

    event_id: str
    message_id: str
    text: str


class AnswerSource(Protocol):
    """回答来源（``/kb/answer`` 的抽象，测试注入假件）。"""

    async def answer(self, question: str) -> AnswerResult:
        """就 ``question`` 取一份带引用的回答。"""
        ...


class ReplySender(Protocol):
    """把卡片回复到指定消息（飞书 IM 的抽象，测试注入假件）。"""

    async def reply_card(self, message_id: str, card: Mapping[str, Any]) -> None:
        """回复 ``message_id``，内容为 ``card``（interactive 卡片 JSON）。"""
        ...


def extract_message(payload: Mapping[str, Any]) -> IncomingMessage | None:
    """从事件负载里取出可处理的提问；不是文本消息则返回 ``None``。

    兼容两种包法：SDK 的 ``P2ImMessageReceiveV1``（``header`` + ``event``）与
    Webhook 式的扁平结构。``message.content`` 在真实 API 里是**一个 JSON 字符串**
    （如 ``'{"text":"你好"}'``），这里两种形状都认。

    Args:
        payload: 事件负载（已转成 mapping）。

    Returns:
        :class:`IncomingMessage`；非文本消息 / 缺 ``message_id`` / 正文为空时为 ``None``。
    """
    raw_event = payload.get("event")
    event: Mapping[str, Any] = raw_event if isinstance(raw_event, Mapping) else payload
    raw_header = payload.get("header")
    header: Mapping[str, Any] = raw_header if isinstance(raw_header, Mapping) else {}

    raw_message = event.get("message")
    if not isinstance(raw_message, Mapping):
        return None
    if str(raw_message.get("msg_type") or "") != "text":
        return None

    message_id = str(raw_message.get("message_id") or "").strip()
    if not message_id:
        return None

    text = _message_text(raw_message)
    if not text:
        return None

    # ``event_id`` 优先取 header；缺失时退化为按 ``message_id`` 去重（仍能挡住重放）。
    event_id = str(header.get("event_id") or payload.get("event_id") or "").strip() or message_id
    return IncomingMessage(event_id=event_id, message_id=message_id, text=text)


def build_card(body_text: str, *, title: str = CARD_TITLE) -> dict[str, Any]:
    """构造卡片 JSON 2.0（``msg_type="interactive"`` 的 ``content``）。

    Args:
        body_text: **已经过** :func:`~recall.lark_md.render_card_body` 转义的正文。
        title: 卡片标题。

    Returns:
        可直接 ``json.dumps`` 进 ``content`` 字段的卡片字典。
    """
    return {
        "schema": "2.0",
        "config": {"update_multi": False},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": CARD_TEMPLATE,
        },
        "body": {"elements": [{"tag": "markdown", "content": body_text}]},
    }


def render_card_text(result: AnswerResult) -> str:
    """把回答结果渲染成卡片正文（转义 + 引用列表，见 :mod:`recall.lark_md`）。"""
    return render_card_body(result.answer, result.references)


class EventDeduper:
    """按 ``event_id`` 去重的**有界**记忆（线程安全）。

    有界是必须的：常驻进程不能无限增长；FIFO 淘汰在"重放窗口远小于容量"时
    不会误判（:data:`DEFAULT_DEDUP_CAPACITY` = 1024）。

    Args:
        capacity: 记忆条数；``<= 0`` 视为 1。
    """

    def __init__(self, capacity: int = DEFAULT_DEDUP_CAPACITY) -> None:
        self._capacity = max(1, capacity)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def first_sight(self, event_id: str) -> bool:
        """登记并判定：首次见到返回 ``True``，重复返回 ``False``。"""
        with self._lock:
            if event_id in self._seen:
                self._seen.move_to_end(event_id)
                return False
            self._seen[event_id] = None
            while len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return True


class TenantTokenCache:
    """``tenant_access_token`` 的进程内缓存。

    用 :class:`threading.Lock` 而**不是** :class:`asyncio.Lock`：每个事件在
    独立工作线程里跑自己的事件循环，asyncio 原语跨循环绑定会出问题；
    而 HTTP 取 token 刻意放在锁**外面**，避免"持锁跨 await"。

    Args:
        margin_s: 提前多少秒视为过期。
        clock: 单调时钟，测试可注入。
    """

    def __init__(
        self,
        *,
        margin_s: float = TOKEN_REFRESH_MARGIN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._margin_s = margin_s
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def get(self) -> str | None:
        """取仍然有效的 token；没有或即将过期时返回 ``None``。"""
        with self._lock:
            if self._token and self._clock() < self._expires_at - self._margin_s:
                return self._token
            return None

    def put(self, token: str, expires_in_s: float) -> None:
        """写入新 token 与其有效期（秒）。

        Note:
            并发首取时可能有两个请求各取一次 token、后者覆盖前者 ——
            这是**刻意接受**的良性竞态（省掉"持锁跨 await"），两者都有效。
        """
        with self._lock:
            self._token = token
            self._expires_at = self._clock() + expires_in_s


class HttpAnswerSource:
    """通过本机 REST ``POST /kb/answer`` 取回答。

    Args:
        api_url: 本机 API 根地址。
        api_key: ``X-API-Key`` 的值；启用鉴权时必填（回环**不豁免**，见 R-40）。
        timeout_s: 单次请求超时。
        transport: ``httpx`` 传输层（测试注入 :class:`httpx.MockTransport`）。
    """

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        api_key: str | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._transport = transport

    async def answer(self, question: str) -> AnswerResult:
        """就 ``question`` 调本机胖端点。

        Args:
            question: 用户提问原文。

        Returns:
            解析后的 :class:`~recall.models.AnswerResult`。

        Raises:
            httpx.HTTPError: 网络 / 状态码异常（调用方负责降级）。
            ValueError: 响应不是合法 JSON 或字段缺失。
        """
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        async with httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport) as client:
            response = await client.post(
                f"{self._api_url}/kb/answer",
                json={"query": question},
                headers=headers,
            )
            response.raise_for_status()
            return AnswerResult.model_validate(response.json())


class HttpReplySender:
    """通过飞书 IM 接口回复消息卡片（自带 tenant token 缓存）。

    Args:
        app_id: 飞书应用 App ID。
        app_secret: 飞书应用 App Secret（**绝不入日志**）。
        base_url: 开放平台根地址（测试可指向假服务）。
        timeout_s: 单次请求超时。
        transport: ``httpx`` 传输层（测试注入 :class:`httpx.MockTransport`）。
        tokens: token 缓存；``None`` 时自建。
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str = DEFAULT_FEISHU_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
        tokens: TenantTokenCache | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._transport = transport
        self._tokens = tokens if tokens is not None else TenantTokenCache()

    async def reply_card(self, message_id: str, card: Mapping[str, Any]) -> None:
        """以 ``interactive`` 卡片回复指定消息。

        Args:
            message_id: 被回复消息的 ID。
            card: :func:`build_card` 产出的卡片字典。

        Raises:
            FeishuApiError: 飞书返回非 0 业务码（HTTP 仍是 200）。
            httpx.HTTPError: 网络 / 状态码异常。
        """
        async with httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport) as client:
            token = await self._tenant_token(client)
            response = await client.post(
                f"{self._base_url}{REPLY_PATH_TEMPLATE.format(message_id=message_id)}",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
            )
            response.raise_for_status()
            _raise_on_business_error(response.json(), what="reply_card")

    async def _tenant_token(self, client: httpx.AsyncClient) -> str:
        """取（必要时刷新）tenant token。"""
        cached = self._tokens.get()
        if cached is not None:
            return cached
        response = await client.post(
            f"{self._base_url}{TENANT_TOKEN_PATH}",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        response.raise_for_status()
        payload = response.json()
        _raise_on_business_error(payload, what="tenant_access_token")
        token = str(payload.get("tenant_access_token") or "").strip()
        if not token:
            raise FeishuApiError("tenant_access_token 响应缺少 token 字段")
        expires_in = float(payload.get("expire") or 0.0)
        self._tokens.put(token, expires_in)
        return token


class FeishuBot:
    """把长连接事件变成"提问 → 回答 → 卡片回复"的编排器。

    Args:
        answer_source: 回答来源（:class:`AnswerSource`）。
        reply_sender: 回复发送器（:class:`ReplySender`）。
        deduper: 事件去重器；``None`` 时自建。
        outbound_limiter: 出站限流器；``None`` 时用 5 条/秒。
        max_workers: 处理线程数（提问是人类速度，几个足够）。
    """

    def __init__(
        self,
        *,
        answer_source: AnswerSource,
        reply_sender: ReplySender,
        deduper: EventDeduper | None = None,
        outbound_limiter: SlidingWindowLimiter | None = None,
        max_workers: int = 4,
    ) -> None:
        self._answer_source = answer_source
        self._reply_sender = reply_sender
        self._deduper = deduper if deduper is not None else EventDeduper()
        self._limiter = (
            outbound_limiter
            if outbound_limiter is not None
            else SlidingWindowLimiter(DEFAULT_OUTBOUND_RATE_LIMIT, DEFAULT_OUTBOUND_RATE_WINDOW_S)
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="feishu"
        )

    def handle_event(self, payload: Mapping[str, Any]) -> None:
        """SDK 的事件回调入口：解析 → 去重 → 投递线程池，**立刻返回**（ACK）。

        ⚠️ 这里**绝不**做任何耗时工作：SDK 把本函数的返回值写回 socket 当作确认，
        阻塞就等于让长连接心跳停摆。

        Args:
            payload: 事件负载 mapping。
        """
        # ⚠️ **进来就记一条 INFO**（2026-10-02 排查得出的必要观测点）：
        # 事故现场是"9 次成功连接、21 小时、`event_accepted` 一条都没有"，但**分不清**
        # 是"一帧都没到"（飞书侧问题）还是"到了但不是文本消息 / 字段形状不认识"（我们这边）。
        # 这条 `frame_received` 就是那条分界线：**没有它就一定是飞书侧没推**。
        logger.info(
            "feishu_bot.frame_received",
            extra={"event_type": _event_type(payload), "msg_type": _raw_msg_type(payload)},
        )
        incoming = extract_message(payload)
        if incoming is None:
            logger.info(
                "feishu_bot.event_ignored",
                extra={
                    "reason": "非文本消息或缺少 message_id / 正文为空",
                    "event_type": _event_type(payload),
                    "msg_type": _raw_msg_type(payload),
                },
            )
            return
        if not self._deduper.first_sight(incoming.event_id):
            logger.info(
                "feishu_bot.duplicate_event_skipped",
                extra={"event_id": incoming.event_id, "message_id": incoming.message_id},
            )
            return
        logger.info(
            "feishu_bot.event_accepted",
            extra={"event_id": incoming.event_id, "message_id": incoming.message_id},
        )
        self._executor.submit(self._run, incoming)

    async def process(self, incoming: IncomingMessage) -> None:
        """真正的处理：取回答 → 渲染卡片 → 回复（**测试直接 await 这个方法**）。

        回答失败时**不抛给调用方**，而是回一张"服务暂时不可用"的卡片 ——
        用户提问了就该拿到一个回复，哪怕内容是降级说明。

        Args:
            incoming: 已解析、已去重的用户消息。
        """
        try:
            result = await self._answer_source.answer(incoming.text)
            body = render_card_text(result)
        except Exception as exc:  # noqa: BLE001 - 任何失败都必须降级成"有回复"
            logger.warning(
                "feishu_bot.answer_failed",
                extra={"message_id": incoming.message_id, "error": type(exc).__name__},
            )
            body = to_lark_md(SERVICE_UNAVAILABLE_TEXT)
        await self._send(incoming.message_id, build_card(body))

    def close(self) -> None:
        """释放线程池（常驻进程退出时调用；幂等）。"""
        self._executor.shutdown(wait=False)

    async def _send(self, message_id: str, card: Mapping[str, Any]) -> None:
        """带出站限流地回复一张卡片。"""
        decision = self._limiter.check("outbound")
        if not decision.allowed:
            logger.warning(
                "feishu_bot.outbound_rate_limited",
                extra={"message_id": message_id, "retry_after_s": decision.retry_after_s},
            )
            await asyncio.sleep(float(decision.retry_after_s))
        try:
            await self._reply_sender.reply_card(message_id, card)
        except Exception as exc:  # noqa: BLE001 - 回复失败只记日志，不能让工作线程崩
            logger.warning(
                "feishu_bot.reply_failed",
                extra={"message_id": message_id, "error": type(exc).__name__},
            )
            return
        logger.info("feishu_bot.replied", extra={"message_id": message_id})

    def _run(self, incoming: IncomingMessage) -> None:
        """工作线程入口：在自己的事件循环里跑 :meth:`process`。"""
        try:
            asyncio.run(self.process(incoming))
        except Exception:  # noqa: BLE001 - 兜底，异常绝不能逃进线程池变成静默丢失
            logger.exception(
                "feishu_bot.process_crashed", extra={"message_id": incoming.message_id}
            )


def _event_type(payload: Mapping[str, Any]) -> str:
    """取事件类型（兼容 SDK 的 ``header.event_type`` 与扁平 ``event_type``）。"""
    header = payload.get("header")
    if isinstance(header, Mapping):
        value = header.get("event_type")
        if value:
            return str(value)
    return str(payload.get("event_type") or "")


def _raw_msg_type(payload: Mapping[str, Any]) -> str:
    """取消息类型（``text`` / ``image`` …）；取不到返回空串。"""
    raw_event = payload.get("event")
    event: Mapping[str, Any] = raw_event if isinstance(raw_event, Mapping) else payload
    message = event.get("message")
    if isinstance(message, Mapping):
        return str(message.get("msg_type") or "")
    return ""


def _message_text(message: Mapping[str, Any]) -> str:
    """从 ``message`` 里抽出提问正文（剥掉 @ 占位符）。"""
    content = message.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            # 极少数情况下 content 直接就是纯文本（例如自建测试负载）
            return _MENTION_RE.sub("", content).strip()
    if not isinstance(content, Mapping):
        return ""
    text = str(content.get("text") or "")
    return _MENTION_RE.sub("", text).strip()


def _raise_on_business_error(payload: Mapping[str, Any], *, what: str) -> None:
    """飞书是 **HTTP 200 + ``code != 0``**；不检查就会"静默不回复"。"""
    code = payload.get("code")
    if code in (0, None):
        return
    raise FeishuApiError(f"{what} 失败：code={code} msg={payload.get('msg')!r}")


def build_ws_client(settings: Settings, bot: FeishuBot) -> Any:
    """构造 SDK 的长连接客户端（**延迟导入 SDK**，便于单测不依赖它）。

    ``EventDispatcherHandler.builder("", "")`` 的空串是刻意的：官方文档说明
    长连接传输**无需** encrypt key / verification token（事件是明文推送）。

    Args:
        settings: 已校验 ``feishu_enabled`` 的配置。
        bot: 事件接收方。

    Returns:
        已注册 ``im.message.receive_v1`` 的 ``lark.ws.Client``（未启动）。
    """
    import lark_oapi as lark  # 延迟导入：只有真正要连飞书时才需要

    def _on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        """SDK 回调：转成 mapping 后交给 bot（返回值即 ACK）。"""
        bot.handle_event(json.loads(lark.JSON.marshal(data)))

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message)
        .build()
    )
    client = lark.ws.Client(
        settings.feishu_app_id or "",
        settings.feishu_app_secret or "",
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    return attach_connection_hooks(client)


def attach_connection_hooks(client: Any) -> Any:
    """把 SDK 的重连生命周期钩子接进**我们自己的结构化日志**。

    **为什么必须接**（2026-10-02 实测事故）：本机出网会**瞬时不可达**。实测一次
    ``Failed to resolve 'open.feishu.cn' ([Errno 11001] getaddrinfo failed)`` 让长连接断了
    **1 分 48 秒**（00:55:05 → 00:56:53），而**这期间飞书的事件不会落地 —— 用户发的消息直接丢**。
    项目工程师当时正发消息，表现就是"机器人不回复"。

    SDK 自己会重连（``ReconnectCount=-1`` 无限重连），但它的日志只有 ``[Lark]`` 前缀那些行；
    没有我们自己的事件 ⇒ **"掉线了"这件事在 `feishu_bot.log` 里不显眼、也没法 grep**。
    接上这两个钩子后，掉线 = ``feishu_bot.reconnecting``（WARNING）、
    恢复 = ``feishu_bot.reconnected``（INFO）。

    钩子是 SDK 的**实例属性**（不是构造参数），故在构造之后赋值。

    Args:
        client: 已构造的 ``lark.ws.Client``（或测试用的假件）。

    Returns:
        同一个 client（便于链式返回）。
    """
    client.on_reconnecting = _log_reconnecting
    client.on_reconnected = _log_reconnected
    return client


def _log_reconnecting() -> None:
    """长连接断开、正在重连。"""
    logger.warning(
        "feishu_bot.reconnecting",
        extra={"hint": "长连接已断，正在重连；**此期间飞书事件不会推达，用户消息会丢**"},
    )


def _log_reconnected() -> None:
    """长连接已重新建立。"""
    logger.info("feishu_bot.reconnected")


def main(argv: Sequence[str] | None = None) -> int:
    """常驻入口：``python -m recall.feishu_bot``。

    Args:
        argv: 命令行参数（``None`` 取 ``sys.argv[1:]``）。

    Returns:
        退出码：``0`` 正常退出；``2`` 配置缺失或参数非法（**fail-closed**）。
    """
    parser = argparse.ArgumentParser(description="Recall 飞书长连接机器人（roadmap R-49）")
    parser.add_argument("--log-level", default="INFO", help="日志级别，如 INFO / DEBUG")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    configure_logging(settings, level=args.log_level, component="feishu_bot")

    if not settings.feishu_enabled:
        logger.error(
            "feishu_bot.not_configured",
            extra={
                "hint": "请在 .env 配齐 FEISHU_APP_ID 与 FEISHU_APP_SECRET；"
                "并确认开发者后台已开 im:message / im:message:send_as_bot、"
                "订阅 im.message.receive_v1（长连接）、且可用范围包含本人"
            },
        )
        return EXIT_NOT_CONFIGURED

    bot = FeishuBot(
        answer_source=HttpAnswerSource(_api_base(settings), _first_api_key(settings)),
        reply_sender=HttpReplySender(
            settings.feishu_app_id or "", settings.feishu_app_secret or ""
        ),
    )
    try:
        logger.info("feishu_bot.starting", extra={"api": _api_base(settings)})
        build_ws_client(settings, bot).start()
    finally:
        bot.close()
    return EXIT_OK


def _api_base(settings: Settings) -> str:
    """本机 API 根地址（供日志与回答来源共用，避免两处各拼一次）。"""
    return f"http://{settings.host}:{settings.port}"


def _first_api_key(settings: Settings) -> str | None:
    """回环调用本机 API 用的 key：优先 ``RECALL_WATCHDOG_API_KEY``，否则取 key 表首项。"""
    if settings.watchdog_api_key:
        return settings.watchdog_api_key
    return next(iter(settings.api_keys), None)


if __name__ == "__main__":  # pragma: no cover - 仅作进程入口
    raise SystemExit(main())
