"""糾錯觸發：本機預篩、路由、LLM 輸出解讀。"""

from __future__ import annotations

import pytest

from sitcon_bot.agent.prompts import PromptBuilder, PromptData
from sitcon_bot.agent.proofread import Proofreader, build_system, parse_reply
from sitcon_bot.domain.proofread import CHECK_SITCON, CHECK_ZAI, lowercase_sitcon, proofread_targets
from sitcon_bot.services.llm.base import LLMResponse, Usage
from sitcon_bot.telegram.routing import Action, Kind, classify_trigger, route


@pytest.mark.parametrize(
    "text",
    [
        "今年 sitcon 在哪辦",
        "Sitcon 好玩",
        "的sitcon",
        "我愛 sitcon.",
        "sitcon2027 要開始了",
    ],
)
def test_lowercase_sitcon_hits(text: str) -> None:
    assert lowercase_sitcon(text)


@pytest.mark.parametrize(
    "text",
    [
        "SITCON 2027 好耶",
        "看 https://sitcon.org/2027",
        "網域是 sitcon.org",
        "寄到 contact@sitcon.org",
        "追蹤 @sitcon_tw",
        "#sitcon",
        "repo 在 sitcon-tw/2027",
        "sitconer 很多",
    ],
)
def test_lowercase_sitcon_skips_identifiers(text: str) -> None:
    assert not lowercase_sitcon(text)


def test_targets() -> None:
    assert proofread_targets("明天再說") == {CHECK_ZAI}
    assert proofread_targets("sitcon 在哪") == {CHECK_SITCON, CHECK_ZAI}
    assert proofread_targets("好喔") == frozenset()
    assert proofread_targets("sitcon 在哪", zai=False) == {CHECK_SITCON}
    assert proofread_targets("") == frozenset()


def _classify(text: str, *, proofread: bool, mention: bool = False):
    return classify_trigger(
        text,
        mentions_bot=mention,
        reply_to_bot=False,
        trigger_name="小石",
        bot_username="bot",
        proofread=proofread,
    )


def test_classify_proofread() -> None:
    assert _classify("我再會議室", proofread=True) == (Kind.PROOFREAD, None)
    assert _classify("我再會議室", proofread=False) == (Kind.NONE, None)
    # 業務觸發優先：小石本身就會回，不另外校對
    assert _classify("小石 我再會議室", proofread=True) == (Kind.BUSINESS, None)
    assert _classify("/help", proofread=True)[0] is Kind.COMMAND


def test_route_proofread_only_authorized() -> None:
    kw = dict(chat_type="supergroup", is_admin=False, kind=Kind.PROOFREAD, command=None)
    assert route(is_authorized=True, **kw) is Action.PROOFREAD
    assert route(is_authorized=False, **kw) is Action.IGNORE


@pytest.mark.parametrize("text", [None, "", "  ", "PASS", "pass", "PASS。沒問題", "x" * 400])
def test_parse_reply_silent(text: str | None) -> None:
    assert parse_reply(text) is None


def test_parse_reply_correction() -> None:
    assert parse_reply("  「再」→「在」：我在會議室  ") == "「再」→「在」：我在會議室"


def test_build_system_only_requested_checks() -> None:
    s = build_system(frozenset({CHECK_ZAI}))
    assert "在」「再」" in s and "大小寫" not in s


class _FakeLLM:
    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self.text, self.exc, self.calls = text, exc, []

    async def chat(self, *, system, messages, tools, thinking, on_text=None):
        self.calls.append((system, messages, tools, thinking))
        if self.exc:
            raise self.exc
        return LLMResponse(self.text, [], Usage(1, 1), "end_turn", "m", None)


def _prompts() -> PromptBuilder:
    async def provider() -> PromptData:
        # 故意塞 label／名冊：lean 模式下不得出現在糾錯 prompt
        return PromptData(labels=["Team::假組別XYZ"], roster_rows=[{"name": "機密人名"}], knowledge="R0 是大會議室")

    return PromptBuilder(provider)


def _pr(llm: _FakeLLM) -> Proofreader:
    return Proofreader(llm, _prompts())  # type: ignore[arg-type]


async def test_proofreader_returns_correction() -> None:
    llm = _FakeLLM("「再」→「在」")
    got = await _pr(llm)("我再會議室", frozenset({CHECK_ZAI}), 123)
    assert got == "「再」→「在」"
    system, messages, tools, thinking = llm.calls[0]
    assert tools == [] and thinking == "off"
    assert "<message>\n我再會議室\n</message>" in messages[0].content[0].text
    # 沿用小石原本 prompt（人設、背景知識），但扣掉 label 與名冊
    assert "你是「小石」" in system and "R0 是大會議室" in system and "【本輪任務：校對】" in system
    assert "假組別XYZ" not in system and "機密人名" not in system and "label 白名單" not in system


async def test_proofreader_pass_and_errors_are_silent() -> None:
    assert await _pr(_FakeLLM("PASS"))("我在會議室", frozenset({CHECK_ZAI})) is None
    assert await _pr(_FakeLLM(exc=RuntimeError("down")))("我在會議室", frozenset({CHECK_ZAI})) is None
    assert await _pr(_FakeLLM("x"))("hi", frozenset()) is None
