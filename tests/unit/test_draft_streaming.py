"""串流草稿（sendMessageDraft）：節流、失敗即停用、gateway 每回合配新 streamer。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sitcon_bot.settings import Settings
from sitcon_bot.telegram.gateway import (
    BusinessRequest,
    BusinessResult,
    Gateway,
    _DraftStreamer,
)


class _DraftBot:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict[str, Any]] = []

    async def send_message_draft(self, **kw: Any) -> None:
        if self.fail:
            raise RuntimeError("Bad Request: DRAFTS_UNSUPPORTED")
        self.sent.append(kw)


async def test_streamer_sends_and_throttles() -> None:
    bot = _DraftBot()
    s = _DraftStreamer(bot, chat_id=-1, thread_id=7, draft_id=5)
    await s("第一")
    await s("第一段")  # 間隔內 → 節流略過
    assert len(bot.sent) == 1
    assert bot.sent[0] == {"chat_id": -1, "draft_id": 5, "text": "第一", "message_thread_id": 7}
    s._last = 0.0  # 模擬間隔已過
    await s("第一段後續")
    assert len(bot.sent) == 2 and bot.sent[1]["text"] == "第一段後續"


async def test_streamer_skips_empty_and_disables_on_error() -> None:
    bot = _DraftBot(fail=True)
    s = _DraftStreamer(bot, chat_id=-1, thread_id=None, draft_id=5)
    await s("")  # 空文字不送
    await s("嗨")  # 失敗 → 停用
    assert s._disabled
    bot.fail = False
    s._last = 0.0
    await s("再試")  # 停用後不再送
    assert bot.sent == []


def _settings(**kw: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token="123:abc",
        telegram_admin_id=1,
        llm_api_key="k",
        gitlab_token="g",
        hackmd_token="h",
        hackmd_team_path="sitcon",
        **kw,
    )


def _breq() -> BusinessRequest:
    return BusinessRequest(
        chat_id=-1, chat_title="群", thread_id=3, user_id=7, username="tester",
        text="小石 查一下", trigger_message_id=5,
    )


class _FakeAudit:
    async def record(self, **kw: Any) -> None:
        return None


async def test_run_business_attaches_fresh_streamer() -> None:
    seen: list[Any] = []

    async def handler(req: BusinessRequest) -> BusinessResult:
        seen.append(req.on_partial)
        return BusinessResult(reply="ok")

    gw = Gateway(_settings(), groups=None, audit=_FakeAudit(), commands=None, business_handler=handler)  # type: ignore[arg-type]
    gw._app = SimpleNamespace(bot=_DraftBot())  # type: ignore[assignment]
    req = _breq()
    await gw._run_business(req)
    await gw._run_business(req)
    assert isinstance(seen[0], _DraftStreamer)
    assert seen[1] is not seen[0]  # 每回合（含重試）配新 streamer
    assert seen[0]._draft_id == 5  # draft_id 取觸發訊息 id（非零）


async def test_run_business_no_streamer_when_disabled() -> None:
    seen: list[Any] = []

    async def handler(req: BusinessRequest) -> BusinessResult:
        seen.append(req.on_partial)
        return BusinessResult(reply="ok")

    gw = Gateway(
        _settings(stream_draft_replies=False), groups=None, audit=_FakeAudit(), commands=None, business_handler=handler
    )  # type: ignore[arg-type]
    gw._app = SimpleNamespace(bot=_DraftBot())  # type: ignore[assignment]
    await gw._run_business(_breq())
    assert seen == [None]
