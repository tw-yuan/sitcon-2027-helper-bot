"""重試按鈕：LLM 服務錯誤（llm_unavailable）的回覆掛 🔄 重試，
按下即以原始請求重跑 agent 回合，結果就地更新該則錯誤訊息。"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sitcon_bot.settings import Settings
from sitcon_bot.telegram.gateway import (
    RETRY_GONE,
    RETRY_IN_PROGRESS,
    RETRY_PREFIX,
    BusinessRequest,
    BusinessResult,
    Gateway,
    RetryStore,
)


def _breq(**kw: Any) -> BusinessRequest:
    base: dict[str, Any] = dict(
        chat_id=-1, chat_title="群", thread_id=None, user_id=7, username="tester",
        text="小石 查一下", trigger_message_id=5,
    )
    base.update(kw)
    return BusinessRequest(**base)


# ------------------------------------------------------------------ #
# RetryStore
# ------------------------------------------------------------------ #
def test_retry_store_take_is_one_shot() -> None:
    store = RetryStore(ttl_seconds=10, clock=lambda: 0.0)
    token = store.put(_breq())
    assert store.take(token) is not None
    assert store.take(token) is None  # 一次性：防連點重複觸發


def test_retry_store_ttl_expiry() -> None:
    now = [0.0]
    store = RetryStore(ttl_seconds=10, clock=lambda: now[0])
    token = store.put(_breq())
    now[0] = 11.0
    assert store.take(token) is None


def test_retry_store_max_entries_evicts_oldest() -> None:
    now = [0.0]
    store = RetryStore(ttl_seconds=100, max_entries=2, clock=lambda: now[0])
    t1 = store.put(_breq(trigger_message_id=1))
    now[0] = 1.0
    t2 = store.put(_breq(trigger_message_id=2))
    now[0] = 2.0
    t3 = store.put(_breq(trigger_message_id=3))
    assert store.take(t1) is None  # 最舊者被淘汰
    assert store.take(t2) is not None
    assert store.take(t3) is not None


# ------------------------------------------------------------------ #
# gateway（沿用 test_context_stores 的假件手法）
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
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, **kw: Any) -> None:
        self.records.append(kw)


class _FakeGroups:
    def __init__(self, authorized: bool = True) -> None:
        self.authorized = authorized

    def is_authorized(self, chat_id: int) -> bool:
        return self.authorized


class _FakeBot:
    def __init__(self) -> None:
        self.markup_cleared: list[int] = []

    async def edit_message_reply_markup(self, *, chat_id: int, message_id: int, reply_markup: Any) -> None:
        self.markup_cleared.append(message_id)


class _Scripted:
    """業務處理器替身：依序回傳預先排好的 result。"""

    def __init__(self, results: list[BusinessResult]) -> None:
        self.results = list(results)
        self.requests: list[BusinessRequest] = []

    async def __call__(self, req: BusinessRequest) -> BusinessResult:
        self.requests.append(req)
        return self.results.pop(0)


def _gateway(handler: Any) -> tuple[Gateway, _FakeAudit, dict[str, Any]]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token="123:abc",
        telegram_admin_id=1,
        llm_api_key="k",
        gitlab_token="g",
        hackmd_token="h",
        hackmd_team_path="sitcon",
    )
    audit = _FakeAudit()
    gw = Gateway(settings, groups=_FakeGroups(), audit=audit, commands=None, business_handler=handler)  # type: ignore[arg-type]
    gw._app = SimpleNamespace(bot=_FakeBot())  # type: ignore[assignment]

    seen: dict[str, Any] = {"replies": [], "edits": [], "reactions": []}

    async def _reply(message: Any, text: str, reply_markup: Any = None) -> int | None:
        seen["replies"].append((text, reply_markup))
        return message.message_id + 1000

    async def _edit(chat_id: int, message_id: int, text: str, reply_markup: Any = None) -> bool:
        seen["edits"].append((message_id, text, reply_markup))
        return True

    async def _react(chat_id: int, message_id: int, emoji: str) -> None:
        seen["reactions"].append((message_id, emoji))

    import contextlib

    @contextlib.asynccontextmanager
    async def _null(*_a: Any) -> Any:
        yield

    gw._reply = _reply  # type: ignore[method-assign]
    gw._edit = _edit  # type: ignore[method-assign]
    gw._react = _react  # type: ignore[method-assign]
    gw._typing = _null  # type: ignore[method-assign,assignment]
    return gw, audit, seen


@dataclass
class _FakeQuery:
    data: str
    message: Any
    from_user: _User = field(default_factory=_User)
    answers: list[str | None] = field(default_factory=list)

    async def answer(self, text: str | None = None, **kw: Any) -> None:
        self.answers.append(text)


def _token_from_markup(markup: Any) -> str:
    data = markup.inline_keyboard[0][0].callback_data
    assert data.startswith(RETRY_PREFIX)
    return data


async def test_llm_unavailable_reply_carries_retry_button() -> None:
    handler = _Scripted([BusinessResult(reply="連線問題", status="error", error="llm_unavailable")])
    gw, _audit, seen = _gateway(handler)
    await gw._handle_business(_Msg(chat=_Chat(-1), message_id=5), "小石 查一下")
    _text, markup = seen["replies"][0]
    assert markup is not None  # 錯誤回覆掛重試按鈕
    assert gw._retries.take(_token_from_markup(markup)[len(RETRY_PREFIX) :]) is not None


async def test_ok_reply_has_no_retry_button() -> None:
    handler = _Scripted([BusinessResult(reply="好了")])
    gw, _audit, seen = _gateway(handler)
    await gw._handle_business(_Msg(chat=_Chat(-1), message_id=5), "小石 查一下")
    _text, markup = seen["replies"][0]
    assert markup is None


async def test_retry_button_reruns_and_updates_message() -> None:
    handler = _Scripted(
        [
            BusinessResult(reply="連線問題", status="error", error="llm_unavailable"),
            BusinessResult(reply="這次成功了", action="chat"),
        ]
    )
    gw, audit, seen = _gateway(handler)
    await gw._handle_business(_Msg(chat=_Chat(-1), message_id=5), "小石 查一下")
    token_data = _token_from_markup(seen["replies"][0][1])

    q = _FakeQuery(data=token_data, message=_Msg(chat=_Chat(-1), message_id=1005))
    await gw._on_callback(SimpleNamespace(callback_query=q), None)

    # 先就地改為「重試中…」，完成後就地改為結果（成功 → 無按鈕）
    assert seen["edits"][0][:2] == (1005, RETRY_IN_PROGRESS)
    mid, text, markup = seen["edits"][1]
    assert (mid, text, markup) == (1005, "這次成功了", None)
    # 重跑用的是原始請求；觸發訊息補上 ✅；稽核記 retry
    assert handler.requests[1].trigger_message_id == 5
    assert (5, "✅") in seen["reactions"]
    assert audit.records[-1]["detail"] == {"retry": True}
    # token 一次性：同一顆按鈕再按只會得到「已過期」
    q2 = _FakeQuery(data=token_data, message=_Msg(chat=_Chat(-1), message_id=1005))
    await gw._on_callback(SimpleNamespace(callback_query=q2), None)
    assert q2.answers == [RETRY_GONE]


async def test_retry_failure_reattaches_button() -> None:
    handler = _Scripted(
        [
            BusinessResult(reply="連線問題", status="error", error="llm_unavailable"),
            BusinessResult(reply="還是不行", status="error", error="llm_unavailable"),
        ]
    )
    gw, _audit, seen = _gateway(handler)
    await gw._handle_business(_Msg(chat=_Chat(-1), message_id=5), "小石 查一下")
    token_data = _token_from_markup(seen["replies"][0][1])

    q = _FakeQuery(data=token_data, message=_Msg(chat=_Chat(-1), message_id=1005))
    await gw._on_callback(SimpleNamespace(callback_query=q), None)

    _mid, _text, markup = seen["edits"][1]
    assert markup is not None  # 仍失敗 → 重掛新按鈕可再重試
    assert _token_from_markup(markup) != token_data  # 新 token


async def test_retry_refused_when_group_revoked() -> None:
    handler = _Scripted(
        [
            BusinessResult(reply="連線問題", status="error", error="llm_unavailable"),
        ]
    )
    gw, _audit, seen = _gateway(handler)
    await gw._handle_business(_Msg(chat=_Chat(-1), message_id=5), "小石 查一下")
    token_data = _token_from_markup(seen["replies"][0][1])
    gw._groups.authorized = False  # type: ignore[attr-defined]

    q = _FakeQuery(data=token_data, message=_Msg(chat=_Chat(-1), message_id=1005))
    await gw._on_callback(SimpleNamespace(callback_query=q), None)
    assert len(seen["edits"]) == 0  # 未重跑、未更新訊息
    assert q.answers and "授權" in (q.answers[0] or "")
