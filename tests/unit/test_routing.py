"""T3：觸發判定與路由矩陣（TRIG-1、AUTH-*）。"""

from __future__ import annotations

import pytest

from sitcon_bot.telegram.routing import (
    Action,
    Kind,
    classify_trigger,
    command_args,
    parse_command,
    route,
)

BOT = "sitcon_helper_bot"
TRIGGER = "小石"


def _classify(text: str, *, mention: bool = False, reply: bool = False):
    return classify_trigger(
        text, mentions_bot=mention, reply_to_bot=reply, trigger_name=TRIGGER, bot_username=BOT
    )


# ------------------------------------------------------------------ #
# classify_trigger
# ------------------------------------------------------------------ #
def test_prefix_trigger() -> None:
    assert _classify("小石 幫我開卡") == (Kind.BUSINESS, None)


def test_prefix_with_leading_space() -> None:
    assert _classify("   小石 開卡") == (Kind.BUSINESS, None)


def test_mention_trigger() -> None:
    assert _classify("幫我開卡", mention=True) == (Kind.BUSINESS, None)


def test_reply_trigger() -> None:
    assert _classify("改成 Doing", reply=True) == (Kind.BUSINESS, None)


def test_plain_message_is_none() -> None:
    assert _classify("今天午餐吃什麼") == (Kind.NONE, None)


def test_command_trigger() -> None:
    assert _classify("/authorize") == (Kind.COMMAND, "authorize")


def test_command_with_args() -> None:
    assert _classify("/help 我要開卡") == (Kind.COMMAND, "help")


def test_command_takes_precedence_over_mention() -> None:
    # 以 / 開頭即為指令，即使同時 @提及
    assert _classify("/help", mention=True) == (Kind.COMMAND, "help")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/authorize", "authorize"),
        (f"/authorize@{BOT}", "authorize"),
        (f"/list_groups@{BOT} extra", "list_groups"),
        ("/authorize@other_bot", None),  # 指向其他 bot
        ("/", None),
        ("/   ", None),
        ("not a command", None),
    ],
)
def test_parse_command(text: str, expected: str | None) -> None:
    assert parse_command(text, BOT) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/notify_on 開發組, 行政組", "開發組, 行政組"),
        (f"/notify_on@{BOT}  開發組 ", "開發組"),
        ("/notify_on", ""),
        ("/notify_on   ", ""),
        ("不是指令 開發組", ""),
    ],
)
def test_command_args(text: str, expected: str) -> None:
    assert command_args(text) == expected


# ------------------------------------------------------------------ #
# route 矩陣：聊天類型 × 管理員 × 授權 × 觸發類型
# ------------------------------------------------------------------ #
def _route(chat_type, is_admin, is_authorized, kind, command=None):
    return route(
        chat_type=chat_type,
        is_admin=is_admin,
        is_authorized=is_authorized,
        kind=kind,
        command=command,
    )


# --- 私訊（AUTH-5、EC-18）---
def test_private_any_message_gets_notice() -> None:
    assert _route("private", False, False, Kind.BUSINESS) is Action.PRIVATE_NOTICE
    assert _route("private", True, True, Kind.NONE) is Action.PRIVATE_NOTICE


def test_private_authorize_redirect() -> None:
    assert (
        _route("private", True, False, Kind.COMMAND, "authorize")
        is Action.PRIVATE_AUTHORIZE_REDIRECT
    )


# --- 未授權群組（AUTH-4）---
def test_unauth_group_admin_authorize_works() -> None:
    assert _route("group", True, False, Kind.COMMAND, "authorize") is Action.CMD_AUTHORIZE


def test_unauth_group_nonadmin_authorize_silent() -> None:
    assert _route("group", False, False, Kind.COMMAND, "authorize") is Action.IGNORE


def test_unauth_group_admin_other_command_silent() -> None:
    # AUTH-4 嚴格：未授權群組唯一例外是管理員 /authorize
    assert _route("group", True, False, Kind.COMMAND, "list_groups") is Action.IGNORE
    assert _route("group", True, False, Kind.COMMAND, "help") is Action.IGNORE


def test_unauth_group_business_silent() -> None:
    assert _route("group", True, False, Kind.BUSINESS) is Action.IGNORE
    assert _route("group", False, False, Kind.BUSINESS) is Action.IGNORE


# --- 授權群組 ---
def test_auth_group_business_goes_to_agent() -> None:
    assert _route("supergroup", False, True, Kind.BUSINESS) is Action.BUSINESS


def test_auth_group_help_anyone() -> None:
    assert _route("group", False, True, Kind.COMMAND, "help") is Action.CMD_HELP


def test_auth_group_start_anyone() -> None:
    assert _route("group", False, True, Kind.COMMAND, "start") is Action.CMD_START


@pytest.mark.parametrize(
    "command,expected",
    [
        ("revoke", Action.CMD_REVOKE),
        ("list_groups", Action.CMD_LIST_GROUPS),
        ("reload", Action.CMD_RELOAD),
        ("notify_on", Action.CMD_NOTIFY_ON),
        ("notify_off", Action.CMD_NOTIFY_OFF),
        ("notify_list", Action.CMD_NOTIFY_LIST),
        ("notify_test", Action.CMD_NOTIFY_TEST),
    ],
)
def test_auth_group_admin_commands(command: str, expected: Action) -> None:
    assert _route("group", True, True, Kind.COMMAND, command) is expected


@pytest.mark.parametrize(
    "command", ["revoke", "list_groups", "reload", "notify_on", "notify_off", "notify_list", "notify_test"]
)
def test_auth_group_nonadmin_admin_commands_silent(command: str) -> None:
    assert _route("group", False, True, Kind.COMMAND, command) is Action.IGNORE


def test_auth_group_reauthorize() -> None:
    # 已授權群組再 /authorize → 仍走 CMD_AUTHORIZE（handler 回「已授權」）
    assert _route("group", True, True, Kind.COMMAND, "authorize") is Action.CMD_AUTHORIZE


def test_auth_group_unknown_command_silent() -> None:
    assert _route("group", True, True, Kind.COMMAND, "foobar") is Action.IGNORE


def test_auth_group_plain_message_silent() -> None:
    assert _route("group", False, True, Kind.NONE) is Action.IGNORE
