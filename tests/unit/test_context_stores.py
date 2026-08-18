"""回覆續接儲存（2026-08-11 修訂）：HistoryStore（peek＋TTL）、transcript 修剪、
gateway 的 reply-chain 優先序（pending > history > 引文後援）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sitcon_bot.agent.context import HistoryStore, Pending, PendingStore, trim_history
from sitcon_bot.services.llm.base import Message, TextBlock, ToolResultBlock, ToolUseBlock
from sitcon_bot.settings import Settings
from sitcon_bot.telegram.gateway import BusinessRequest, BusinessResult, Gateway


def _turn(user_text: str, tool_payload: str, final_text: str) -> list[Message]:
    """一個完整回合：發話 → tool_use → tool_result → 最終回覆。"""
    return [
        Message("user", [TextBlock(user_text)]),
        Message("assistant", [ToolUseBlock("t1", "record", {"v": 1})]),
        Message("user", [ToolResultBlock("t1", tool_payload)]),
        Message("assistant", [TextBlock(final_text)]),
    ]


# ------------------------------------------------------------------ #
# trim_history
# ------------------------------------------------------------------ #
def test_trim_keeps_all_under_budget() -> None:
    msgs = _turn("問", "小結果", "答")
    assert trim_history(msgs, budget_chars=1000) == msgs


def test_trim_drops_oldest_turn_at_safe_boundary() -> None:
    old = _turn("第一問", "巨" * 900, "第一答")
    new = _turn("第二問", "小結果", "第二答")
    trimmed = trim_history([*old, *new], budget_chars=500)
    assert trimmed == new  # 整個舊回合被丟掉
    # 切點必為使用者發話訊息：不會以 tool_result 開頭（tool_use/result 配對不被切斷）
    assert all(isinstance(b, TextBlock) for b in trimmed[0].content)


def test_trim_keeps_last_turn_even_over_budget() -> None:
    huge = _turn("問", "巨" * 5000, "答")
    trimmed = trim_history([*_turn("舊問", "x", "舊答"), *huge], budget_chars=100)
    assert trimmed == huge  # 至少保留最後一個完整回合


# ------------------------------------------------------------------ #
# HistoryStore／PendingStore
# ------------------------------------------------------------------ #
def test_history_store_peek_not_pop() -> None:
    store = HistoryStore(ttl_seconds=1800)
    msgs = _turn("問", "結果", "答")
    store.put(-1, 10, msgs)
    assert store.get(-1, 10) == msgs
    assert store.get(-1, 10) == msgs  # 可重複取（同一則回覆可被多次接續）
    assert store.get(-1, 11) is None


def test_history_store_ttl_expiry() -> None:
    now = [0.0]
    store = HistoryStore(ttl_seconds=100, clock=lambda: now[0])
    store.put(-1, 10, _turn("問", "結果", "答"))
    now[0] = 99.0
    assert store.get(-1, 10) is not None
    now[0] = 101.0
    assert store.get(-1, 10) is None


def test_pending_store_take_pops() -> None:
    store = PendingStore(ttl_seconds=1800)
    store.put(-1, 10, Pending(messages=[], resolved_results=[], ask_user_id="a1"))
    assert store.take(-1, 10) is not None
    assert store.take(-1, 10) is None  # 一次性


# ------------------------------------------------------------------ #
# gateway：reply-chain 優先序與 history 存取（沿用 test_concurrency 的假件手法）
# ------------------------------------------------------------------ #
@dataclass
class _User:
    id: int = 7
    username: str | None = "tester"
    is_bot: bool = False


@dataclass
class _Chat:
    id: int
    title: str | None = "群"
    type: str = "supergroup"


@dataclass
class _Msg:
    chat: _Chat
    message_id: int
    from_user: _User = field(default_factory=_User)
    message_thread_id: int | None = None
    reply_to_message: Any = None
    text: str = "小石 查一下"
    caption: str | None = None


class _FakeAudit:
    async def record(self, **kw: Any) -> None:
        return None


class _Recorder:
    """業務處理器替身：記下收到的 request，回傳可設定的 result。"""

    def __init__(self) -> None:
        self.requests: list[BusinessRequest] = []
        self.next_history: list[Message] | None = None

    async def __call__(self, req: BusinessRequest) -> BusinessResult:
        self.requests.append(req)
        return BusinessResult(reply="ok", history=self.next_history)


def _gateway(handler: Any) -> Gateway:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token="123:abc",
        telegram_admin_id=1,
        llm_api_key="k",
        gitlab_token="g",
        hackmd_token="h",
        hackmd_team_path="sitcon",
    )
    gw = Gateway(settings, groups=None, audit=_FakeAudit(), commands=None, business_handler=handler)  # type: ignore[arg-type]

    async def _react(chat_id: int, message_id: int, emoji: str) -> None:
        return None

    async def _reply(message: Any, text: str, reply_markup: Any = None) -> int | None:
        return message.message_id + 1000  # bot 回覆的 message_id

    import contextlib

    @contextlib.asynccontextmanager
    async def _null(*_a: Any) -> Any:
        yield

    gw._react = _react  # type: ignore[method-assign]
    gw._reply = _reply  # type: ignore[method-assign]
    gw._typing = _null  # type: ignore[method-assign,assignment]
    return gw


async def test_gateway_stores_history_and_resumes_on_reply() -> None:
    handler = _Recorder()
    gw = _gateway(handler)
    transcript = _turn("查一下", "工具結果 A", "查到 A 案")

    handler.next_history = transcript
    first = _Msg(chat=_Chat(-1), message_id=5)
    await gw._handle_business(first, first.text)
    assert handler.requests[0].history is None  # 第一則非回覆，無脈絡

    # 使用者回覆 bot 的回覆（message_id = 5+1000）→ 完整 transcript 續接
    handler.next_history = None
    bot_reply = _Msg(chat=_Chat(-1), message_id=1005, text="查到 A 案")
    followup = _Msg(chat=_Chat(-1), message_id=6, reply_to_message=bot_reply)
    await gw._handle_business(followup, "那日期呢")
    req = handler.requests[1]
    assert req.history == transcript
    assert req.reply_context is None  # 有 transcript 就不用引文後援
    assert req.resume is None


async def test_gateway_reply_without_history_falls_back_to_quote() -> None:
    handler = _Recorder()
    gw = _gateway(handler)
    msg = _Msg(chat=_Chat(-1), message_id=6, reply_to_message=_Msg(chat=_Chat(-1), message_id=99, text="某人的訊息"))
    await gw._handle_business(msg, "這個怎麼辦")
    req = handler.requests[0]
    assert req.history is None
    assert req.reply_context == "某人的訊息"


async def test_gateway_pending_takes_precedence_over_history() -> None:
    handler = _Recorder()
    gw = _gateway(handler)
    pending = Pending(messages=[], resolved_results=[], ask_user_id="a1")
    gw._pending.put(-1, 1005, pending)
    gw._history.put(-1, 1005, _turn("問", "結果", "答"))
    msg = _Msg(chat=_Chat(-1), message_id=6, reply_to_message=_Msg(chat=_Chat(-1), message_id=1005, text="哪一張？"))
    await gw._handle_business(msg, "#42")
    req = handler.requests[0]
    assert req.resume is pending  # 反問續答優先
    assert req.history is None
