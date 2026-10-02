"""``recall/feishu_bot.py`` 的用例（roadmap R-49c）。

全部用例**不依赖 GPU、不依赖 Qdrant、不连真实飞书**：HTTP 走
:class:`httpx.MockTransport`，回答来源与回复发送器都注入假件。

重点钉住三条硬约束（见模块 docstring 与 ``tech.md`` §18 决策 19）：

1. **先 ACK、后异步** —— ``handle_event`` 必须在回答完成**之前**就返回
   （否则堵死长连接心跳）。用例用"卡住回答"的假件来**证明**这一点，不靠计时。
2. **``event_id`` 幂等** —— 重连重放不能答两遍。
3. **失败必降级** —— 回答失败也要回一张卡片，而不是静默。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from recall.feishu_bot import (
    EXIT_NOT_CONFIGURED,
    REPLY_PATH_TEMPLATE,
    SERVICE_UNAVAILABLE_TEXT,
    TENANT_TOKEN_PATH,
    EventDeduper,
    FeishuApiError,
    FeishuBot,
    HttpAnswerSource,
    HttpReplySender,
    IncomingMessage,
    TenantTokenCache,
    attach_connection_hooks,
    build_card,
    extract_message,
    main,
    render_card_text,
)
from recall.models import AnswerResult
from recall.ratelimit import RateLimitDecision, SlidingWindowLimiter

_ANSWER = AnswerResult(
    answer="切分粒度决定检索质量 [1]。",
    citations=[1],
    references=[{"ref_id": "1", "source_uri": "notes/split.md"}],
)


# ── 假件 ─────────────────────────────────────────────────────────────────────


class FakeAnswerSource:
    """可注入延迟/失败的回答来源。"""

    def __init__(
        self,
        *,
        result: AnswerResult | None = None,
        error: Exception | None = None,
        block: bool = False,
    ) -> None:
        self.result = result if result is not None else _ANSWER
        self.error = error
        self.started = threading.Event()
        self._release = threading.Event()
        self.block = block
        self.calls: list[str] = []

    async def answer(self, question: str) -> AnswerResult:
        self.calls.append(question)
        self.started.set()
        if self.block:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._release.wait)
        if self.error is not None:
            raise self.error
        return self.result

    def release(self) -> None:
        self._release.set()


class FakeReplySender:
    """记录回复的假发送器。"""

    def __init__(self) -> None:
        self.replies: list[tuple[str, dict[str, object]]] = []

    async def reply_card(self, message_id: str, card: object) -> None:
        assert isinstance(card, dict)
        self.replies.append((message_id, card))


class _DenyingLimiter(SlidingWindowLimiter):
    """永远拒绝的出站限流器（验证"限流不等于丢答案"）。"""

    def __init__(self) -> None:
        super().__init__(1, 1.0)

    def check(self, key: str = "default") -> RateLimitDecision:
        return RateLimitDecision(False, 0, 0)


def _sdk_payload(
    *,
    event_id: str = "evt-1",
    message_id: str = "om-1",
    text: str = "怎么切分？",
    msg_type: str = "text",
) -> dict[str, object]:
    """**真实长连接帧**的形状（照 2026-10-03 `--dump-frame` 抓到的原始帧复刻；ID 换成假值）。

    🔴 关键差异（曾让机器人**静默不回复**）：真实帧里消息类型字段是 **``message_type``**，
    而官方文档示例写的是 ``msg_type``。夹具必须跟**真实**走，否则这条 bug 永远测不出来。
    """
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant-x",
            "app_id": "cli_x",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_x", "union_id": "on_x"},
                "sender_type": "user",
            },
            "message": {
                "message_id": message_id,
                "chat_id": "oc_x",
                "chat_type": "p2p",
                "message_type": msg_type,
                "content": json.dumps({"text": text}),
            },
        },
    }


def test_extract_accepts_the_legacy_msg_type_spelling() -> None:
    """文档示例用的是 ``msg_type``；老负载 / 第三方转发可能还是那个拼法 ⇒ **两种都要认**。"""
    payload = _sdk_payload()
    message = payload["event"]["message"]  # type: ignore[index]
    message["msg_type"] = message.pop("message_type")
    incoming = extract_message(payload)
    assert incoming is not None
    assert incoming.text == "怎么切分？"


def test_extract_rejects_a_non_text_message_type() -> None:
    """真·非文本消息才该被拒（回归：字段名写错会把**所有**消息都拒掉）。"""
    assert extract_message(_sdk_payload(msg_type="image")) is None
    assert extract_message(_sdk_payload(msg_type="")) is None


def _card_markdown(card: dict[str, object]) -> str:
    """取出卡片里的 markdown 正文（把卡片结构断言集中在一处）。

    顺带证明我们要的结构确实存在 —— 比链式下标 + ``type: ignore`` 更能说明意图。
    """
    body = card["body"]
    assert isinstance(body, dict)
    elements = body["elements"]
    assert isinstance(elements, list)
    element = elements[0]
    assert isinstance(element, dict)
    content = element["content"]
    assert isinstance(content, str)
    return content


# ── extract_message ──────────────────────────────────────────────────────────


def test_extract_reads_sdk_shaped_payload() -> None:
    incoming = extract_message(_sdk_payload())
    assert incoming == IncomingMessage(event_id="evt-1", message_id="om-1", text="怎么切分？")


def test_extract_accepts_an_already_parsed_content_dict() -> None:
    payload = _sdk_payload()
    message = payload["event"]["message"]  # type: ignore[index]
    message["content"] = {"text": "已解析的正文"}
    assert extract_message(payload) is not None
    assert extract_message(payload).text == "已解析的正文"  # type: ignore[union-attr]


def test_extract_strips_group_mention_placeholders() -> None:
    """群聊 @ 机器人时正文带 ``@_user_1`` —— 那是占位符，不是问题的一部分。"""
    incoming = extract_message(_sdk_payload(text="@_user_1  Redis 持久化怎么做？"))
    assert incoming is not None
    assert incoming.text == "Redis 持久化怎么做？"


def test_extract_supports_flat_webhook_shape() -> None:
    flat = {
        "event_id": "evt-flat",
        "message": {"message_id": "om-9", "msg_type": "text", "content": {"text": "你好"}},
    }
    incoming = extract_message(flat)
    assert incoming is not None
    assert (incoming.event_id, incoming.message_id) == ("evt-flat", "om-9")


def test_extract_falls_back_to_message_id_when_event_id_is_missing() -> None:
    payload = _sdk_payload()
    payload["header"] = {"event_type": "im.message.receive_v1"}
    incoming = extract_message(payload)
    assert incoming is not None
    assert incoming.event_id == "om-1"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p["event"]["message"].update(message_type="image"), id="非文本"),
        pytest.param(lambda p: p["event"]["message"].update(message_id=""), id="缺 message_id"),
        pytest.param(lambda p: p["event"]["message"].update(content={"text": "   "}), id="空正文"),
        pytest.param(lambda p: p.pop("event"), id="无 event"),
    ],
)
def test_extract_ignores_unusable_events(mutate: object) -> None:
    payload = _sdk_payload()
    mutate(payload)  # type: ignore[operator]
    assert extract_message(payload) is None


# ── 卡片 ─────────────────────────────────────────────────────────────────────


def test_card_is_a_schema_2_markdown_card() -> None:
    card = build_card("正文")
    assert card["schema"] == "2.0"
    assert card["body"]["elements"] == [{"tag": "markdown", "content": "正文"}]
    assert card["header"]["title"]["content"]


def test_card_config_keeps_update_multi_true() -> None:
    """🔴 回归：``update_multi=False`` 会被飞书**拒收整张卡片**。

    实测（2026-10-03，`tools/diagnose_r49.py` D 节逐个试形态）：
    ``code=230099`` / ``ext=ErrCode: 300302; ErrMsg: update_multi is false``；
    而 ``True`` / 不带 ``config`` / 旧版卡片 三种都能发出去。
    外部现象是"消息收到了、答案也生成了，但回复发不出去"，只留一条 `reply_failed`。
    """
    assert build_card("正文")["config"]["update_multi"] is True


def test_card_text_is_escaped_and_carries_references() -> None:
    text = render_card_text(_ANSWER)
    assert "&#91;1&#93;" in text  # 引用编号已转义，不会变成链接语法
    assert "notes/split.md" in text


# ── 去重 ─────────────────────────────────────────────────────────────────────


def test_deduper_remembers_and_rejects_replays() -> None:
    deduper = EventDeduper(capacity=4)
    assert deduper.first_sight("a") is True
    assert deduper.first_sight("a") is False
    assert deduper.first_sight("b") is True


def test_deduper_is_bounded_and_evicts_oldest() -> None:
    deduper = EventDeduper(capacity=2)
    assert deduper.first_sight("a") is True
    assert deduper.first_sight("b") is True
    assert deduper.first_sight("c") is True
    # "a" 已被挤出记忆 ⇒ 再见到算首次（有界性的代价，容量 1024 下不会触发）
    assert deduper.first_sight("a") is True
    assert deduper.first_sight("c") is False


def test_deduper_capacity_is_at_least_one() -> None:
    deduper = EventDeduper(capacity=0)
    assert deduper.first_sight("x") is True
    assert deduper.first_sight("x") is False


# ── token 缓存 ───────────────────────────────────────────────────────────────


def test_token_cache_is_empty_initially() -> None:
    assert TenantTokenCache().get() is None


def test_token_cache_returns_value_before_the_margin() -> None:
    now = [1000.0]
    cache = TenantTokenCache(margin_s=60.0, clock=lambda: now[0])
    cache.put("t-1", 7200.0)
    assert cache.get() == "t-1"


def test_token_cache_expires_by_margin() -> None:
    now = [1000.0]
    cache = TenantTokenCache(margin_s=60.0, clock=lambda: now[0])
    cache.put("t-1", 100.0)
    now[0] += 50.0  # 距过期 50s < margin 60s ⇒ 视为过期
    assert cache.get() is None


# ── HTTP：回答来源 ───────────────────────────────────────────────────────────


async def test_answer_source_posts_query_with_api_key() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_ANSWER.model_dump())

    source = HttpAnswerSource(
        "http://127.0.0.1:8000", "secret-key", transport=httpx.MockTransport(handler)
    )
    result = await source.answer("怎么切分？")

    assert result == _ANSWER
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/kb/answer"
    assert request.headers["X-API-Key"] == "secret-key"
    assert json.loads(request.content) == {"query": "怎么切分？"}


async def test_answer_source_omits_api_key_when_not_configured() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_ANSWER.model_dump())

    source = HttpAnswerSource(transport=httpx.MockTransport(handler))
    await source.answer("q")
    assert "X-API-Key" not in captured[0].headers


# ── HTTP：回复发送器 ─────────────────────────────────────────────────────────


def _reply_transport(
    *,
    token_code: int = 0,
    reply_code: int = 0,
    seen: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    log = seen if seen is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if request.url.path == TENANT_TOKEN_PATH:
            body = {"code": token_code, "msg": "x"}
            if token_code == 0:
                body |= {"tenant_access_token": "t-1", "expire": 7200}
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"code": reply_code, "msg": "y"})

    return httpx.MockTransport(handler)


async def test_reply_sender_fetches_token_then_replies_with_a_card() -> None:
    seen: list[httpx.Request] = []
    sender = HttpReplySender("cli_x", "secret", transport=_reply_transport(seen=seen))
    await sender.reply_card("om-1", build_card("正文"))

    assert [r.url.path for r in seen] == [
        TENANT_TOKEN_PATH,
        REPLY_PATH_TEMPLATE.format(message_id="om-1"),
    ]
    reply = seen[1]
    assert reply.headers["Authorization"] == "Bearer t-1"
    payload = json.loads(reply.content)
    assert payload["msg_type"] == "interactive"
    # content 必须是**字符串**（卡片 JSON 序列化后），不是嵌套对象
    assert isinstance(payload["content"], str)
    assert json.loads(payload["content"])["schema"] == "2.0"


async def test_reply_sender_caches_the_token_across_replies() -> None:
    seen: list[httpx.Request] = []
    sender = HttpReplySender("cli_x", "secret", transport=_reply_transport(seen=seen))
    await sender.reply_card("om-1", build_card("一"))
    await sender.reply_card("om-2", build_card("二"))

    token_calls = [r for r in seen if r.url.path == TENANT_TOKEN_PATH]
    assert len(token_calls) == 1  # 第二次不再取 token


async def test_reply_sender_raises_on_business_error_inside_http_200() -> None:
    """飞书是 **HTTP 200 + code != 0**；不检查就会"静默不回复"。"""
    sender = HttpReplySender("cli_x", "secret", transport=_reply_transport(reply_code=99991672))
    with pytest.raises(FeishuApiError, match="99991672"):
        await sender.reply_card("om-1", build_card("正文"))


async def test_reply_sender_raises_when_the_token_call_fails() -> None:
    sender = HttpReplySender("cli_x", "secret", transport=_reply_transport(token_code=99991663))
    with pytest.raises(FeishuApiError, match="tenant_access_token"):
        await sender.reply_card("om-1", build_card("正文"))


# ── 编排：先 ACK、后异步 ─────────────────────────────────────────────────────


async def test_process_replies_with_the_escaped_answer() -> None:
    answers = FakeAnswerSource()
    replies = FakeReplySender()
    bot = FeishuBot(answer_source=answers, reply_sender=replies)

    await bot.process(IncomingMessage(event_id="e", message_id="om-1", text="问"))
    bot.close()

    assert answers.calls == ["问"]
    message_id, card = replies.replies[0]
    assert message_id == "om-1"
    assert "&#91;1&#93;" in _card_markdown(card)


async def test_process_degrades_to_a_card_when_answering_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """回答失败也必须回一张卡片 —— 用户提问了就该拿到回复，哪怕是降级说明。

    顺带钉住：失败**原因**要出现在日志的 message 里（`extra` 会被格式器丢掉）。
    """
    answers = FakeAnswerSource(error=RuntimeError("api down"))
    replies = FakeReplySender()
    bot = FeishuBot(answer_source=answers, reply_sender=replies)

    with caplog.at_level(logging.WARNING, logger="recall.feishu_bot"):
        await bot.process(IncomingMessage(event_id="e", message_id="om-1", text="问"))
    bot.close()

    card = replies.replies[0][1]
    assert SERVICE_UNAVAILABLE_TEXT in _card_markdown(card)
    assert "feishu_bot.answer_failed" in caplog.text
    assert "RuntimeError" in caplog.text and "api down" in caplog.text


async def test_a_failing_reply_sender_does_not_raise() -> None:
    class BrokenSender:
        async def reply_card(self, message_id: str, card: object) -> None:
            raise RuntimeError("network")

    bot = FeishuBot(answer_source=FakeAnswerSource(), reply_sender=BrokenSender())
    await bot.process(IncomingMessage(event_id="e", message_id="om-1", text="问"))
    bot.close()


async def test_reply_failure_logs_the_error_detail_in_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """诊断信息必须进 **message**：`LOG_FORMAT` 丢弃 `extra`。

    2026-10-03 就是因为 `reply_failed` 只打了事件名、详情在 extra 里被丢掉，
    "卡片被飞书拒收"完全看不出来，白多排查一轮。
    """

    class BrokenSender:
        async def reply_card(self, message_id: str, card: object) -> None:
            raise RuntimeError("Failed to create card content")

    bot = FeishuBot(answer_source=FakeAnswerSource(), reply_sender=BrokenSender())
    try:
        with caplog.at_level(logging.WARNING, logger="recall.feishu_bot"):
            await bot.process(IncomingMessage(event_id="e", message_id="om-1", text="问"))
        assert "feishu_bot.reply_failed" in caplog.text
        assert "RuntimeError" in caplog.text
        assert "Failed to create card content" in caplog.text
    finally:
        bot.close()


def test_handle_event_acks_before_the_answer_completes() -> None:
    """**核心契约**：``handle_event`` 必须在回答完成前返回，否则长连接心跳停摆。

    用例刻意**不靠计时**：假件会一直卡在 ``answer`` 里，直到测试放行。
    因此"``handle_event`` 已返回 + 回复还没发生"是确定性证据。
    """
    answers = FakeAnswerSource(block=True)
    replies = FakeReplySender()
    bot = FeishuBot(answer_source=answers, reply_sender=replies)
    try:
        bot.handle_event(_sdk_payload())
        assert answers.started.wait(timeout=5.0), "工作线程没启动"
        assert replies.replies == [], "handle_event 同步等到了回答 —— ACK 契约被破坏"

        answers.release()
        deadline = threading.Event()
        for _ in range(200):  # 最多等 2s
            if replies.replies:
                break
            deadline.wait(0.01)
        assert replies.replies, "放行后仍未回复"
    finally:
        answers.release()
        bot.close()


def test_handle_event_ignores_duplicate_events() -> None:
    answers = FakeAnswerSource()
    replies = FakeReplySender()
    bot = FeishuBot(answer_source=answers, reply_sender=replies)
    try:
        bot.handle_event(_sdk_payload(event_id="evt-same"))
        bot.handle_event(_sdk_payload(event_id="evt-same"))
        for _ in range(300):
            if replies.replies:
                break
            threading.Event().wait(0.01)
        assert len(replies.replies) == 1  # 重放只答一次
    finally:
        bot.close()


def test_handle_event_ignores_non_text_messages() -> None:
    answers = FakeAnswerSource()
    replies = FakeReplySender()
    bot = FeishuBot(answer_source=answers, reply_sender=replies)
    try:
        bot.handle_event(_sdk_payload(msg_type="image"))
        threading.Event().wait(0.1)
        assert answers.calls == []
        assert replies.replies == []
    finally:
        bot.close()


async def test_rate_limited_outbound_still_sends_the_answer() -> None:
    """限流**不是丢答案**：被限时等一个窗口再发，而不是把用户的回答扔掉。"""
    replies = FakeReplySender()
    bot = FeishuBot(
        answer_source=FakeAnswerSource(),
        reply_sender=replies,
        outbound_limiter=_DenyingLimiter(),
    )
    await bot.process(IncomingMessage(event_id="e", message_id="om-1", text="问"))
    bot.close()
    assert len(replies.replies) == 1


# ── SDK 接线与入口 ───────────────────────────────────────────────────────────


def test_sdk_surface_matches_our_wiring() -> None:
    """对着**真实安装的 SDK** 验证我们用到的方法名与关键字参数都存在。

    ⚠️ **本用例约 26 秒**，全部花在 ``import lark_oapi`` 上（实测冷启动 **8.28s**，
    累计 9.3s —— SDK 会急切导入它全部生成的 API，见 ``-X importtime`` 里成片的
    ``lark_oapi.api.*.resource``）。之所以仍然保留：它是**唯一**能自动发现
    "SDK 方法名/签名写错"的检查（例如 ``register_p2_im_message_receive_v1``），
    否则这类错误只会在真实飞书会话里才暴露。

    ⚠️ **刻意不构造 ``lark.ws.Client``**：构造它会在 SDK 的 ``ExpiringCache.__init__``
    里 `loop.create_task(...)` 起一个清理 cron，而 ``ws.Client`` **没有**
    ``stop()`` / ``close()`` ⇒ 单元测试里构造会留下
    "Task was destroyed but it is pending" 的 asyncio ERROR 噪音。
    改为校验表面（名字 + 签名），覆盖面相同而不产生副作用。
    """
    import inspect

    import lark_oapi as lark

    builder = lark.EventDispatcherHandler.builder("", "")
    assert hasattr(builder, "register_p2_im_message_receive_v1")

    params = inspect.signature(lark.ws.Client.__init__).parameters
    assert "event_handler" in params
    assert "log_level" in params
    assert "auto_reconnect" in params


def test_connection_hooks_are_attached_and_are_visible_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """掉线必须**看得见**。

    2026-10-02 实测事故：本机出网瞬时不可达（`getaddrinfo failed`）让长连接断了 **1 分 48 秒**，
    而**这期间飞书事件不落地 —— 用户消息直接丢**；当时项目工程师正发消息，表现就是"机器人不回复"。
    SDK 自己会重连，但它的 `[Lark]` 日志不是我们的结构化事件、不好 grep ⇒ 接上钩子。
    """

    class FakeClient:
        def __init__(self) -> None:
            self.on_reconnecting: Callable[[], None] | None = None
            self.on_reconnected: Callable[[], None] | None = None

    client = attach_connection_hooks(FakeClient())
    assert client.on_reconnecting is not None
    assert client.on_reconnected is not None

    # ⚠️ 必须用 INFO：恢复那条是 INFO 级，用 WARNING 会把它过滤掉（实测踩过）
    with caplog.at_level(logging.INFO, logger="recall.feishu_bot"):
        client.on_reconnecting()
        client.on_reconnected()

    assert "feishu_bot.reconnecting" in caplog.text
    assert "feishu_bot.reconnected" in caplog.text


def test_any_incoming_frame_is_visible_at_info_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**诊断分界线**：帧一到就记 INFO —— 日志里没有 `frame_received` 就一定是飞书侧没推。

    2026-10-02 排查时吃过这个亏：日志只有"连上了"（9 次），却分不清"事件没来"还是
    "来了但没认出来"（`event_ignored` 当时打在 DEBUG，INFO 下看不见）。

    ⚠️ 诊断值必须出现在 **message** 里 —— `LOG_FORMAT` 只打 `%(message)s`，
    `extra` 会被整个丢弃（实测：打了 extra 却在日志里一个字都看不到）。
    """
    replies = FakeReplySender()
    bot = FeishuBot(answer_source=FakeAnswerSource(), reply_sender=replies)
    try:
        with caplog.at_level(logging.INFO, logger="recall.feishu_bot"):
            bot.handle_event(_sdk_payload(msg_type="image"))

        names = [record.getMessage() for record in caplog.records]
        received = next(m for m in names if m.startswith("feishu_bot.frame_received"))
        ignored = next(m for m in names if m.startswith("feishu_bot.event_ignored"))
        assert "msg_type='image'" in received  # 形状入 message，才看得见
        assert "event_type='im.message.receive_v1'" in received
        assert "message_type='image'" in ignored  # 拒收原因也入 message
        assert replies.replies == []
    finally:
        bot.close()


def test_frame_dump_writes_exactly_once(tmp_path: Path) -> None:
    """`--dump-frame` 只落**第一帧**：诊断用，不能变成每帧写盘。"""
    target = tmp_path / "nested" / "frame.json"
    bot = FeishuBot(
        answer_source=FakeAnswerSource(),
        reply_sender=FakeReplySender(),
        frame_dump=target,
    )
    try:
        bot.handle_event(_sdk_payload(event_id="evt-first", text="第一帧"))
        bot.handle_event(_sdk_payload(event_id="evt-second", text="第二帧"))
    finally:
        bot.close()

    dumped = json.loads(target.read_text(encoding="utf-8"))
    message = dumped["event"]["message"]
    assert json.loads(message["content"])["text"] == "第一帧"  # 是**第一帧**


def test_a_usable_frame_logs_both_received_and_accepted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """可处理的帧：`frame_received` → `event_accepted` 两条都要有（排错就按这两条定位）。"""
    replies = FakeReplySender()
    bot = FeishuBot(answer_source=FakeAnswerSource(), reply_sender=replies)
    try:
        with caplog.at_level(logging.INFO, logger="recall.feishu_bot"):
            bot.handle_event(_sdk_payload())
        names = [record.getMessage() for record in caplog.records]
        assert any(m.startswith("feishu_bot.frame_received") for m in names)
        assert any(m.startswith("feishu_bot.event_accepted") for m in names)
    finally:
        bot.close()


def test_main_fails_closed_without_feishu_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    assert main([]) == EXIT_NOT_CONFIGURED
