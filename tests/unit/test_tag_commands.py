"""快速 tag 指令（/tl /ta）：名單篩選、剔除自己、分批與退化訊息。"""

from __future__ import annotations

from types import SimpleNamespace

from sitcon_bot.auth.groups import GroupStore
from sitcon_bot.services.sheets_roster import Member, Roster, RosterUnavailableError
from sitcon_bot.settings import Settings
from sitcon_bot.storage.db import Database
from sitcon_bot.telegram.commands import (
    ROSTER_UNAVAILABLE,
    TAG_BATCH_SIZE,
    CommandHandlers,
)
from sitcon_bot.telegram.gateway import Gateway
from sitcon_bot.telegram.routing import Action

ADMIN = 42


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token="123:abc",
        telegram_admin_id=ADMIN,
        llm_api_key="k",
        gitlab_token="g",
        hackmd_token="h",
        hackmd_team_path="sitcon",
    )


class _StubRosterService:
    def __init__(self, members: list[Member]) -> None:
        self._roster = Roster(members)

    async def get(self, *, force: bool = False) -> Roster:
        return self._roster


class _FailingRosterService:
    async def get(self, *, force: bool = False) -> Roster:
        raise RosterUnavailableError("boom")


def _member(
    gid: int,
    *,
    nick: str | None = None,
    tg_user: str | None = None,
    tg_id: int | None = None,
    position: str | None = None,
    role: str | None = None,
    other_role: str | None = None,
) -> Member:
    return Member(
        gitlab_id=gid,
        nickname=nick,
        telegram_username=tg_user,
        telegram_id=tg_id,
        position=position,
        role=role,
        other_role=other_role,
    )


def _handlers(db: Database, roster: object) -> CommandHandlers:
    return CommandHandlers(_settings(), GroupStore(db), roster=roster)  # type: ignore[arg-type]


# ------------------------------------------------------------------ #
# /tl：組長＋總召，不含副組長
# ------------------------------------------------------------------ #
async def test_tl_leaders_and_chiefs_only(db: Database) -> None:
    members = [
        _member(1, tg_user="lead_dev", position="組長", role="開發組"),
        _member(2, tg_user="chief", position="總召"),
        _member(3, tg_user="vice", position="副組長", role="開發組"),
        _member(4, tg_user="member", position="組員", role="行政組"),
    ]
    out = await _handlers(db, _StubRosterService(members)).tag_leaders(999, None)
    assert len(out) == 1
    assert "@lead_dev" in out[0] and "@chief" in out[0]
    assert "vice" not in out[0] and "member" not in out[0]
    assert "組長＋總召" in out[0]


async def test_tl_excludes_self_by_telegram_id(db: Database) -> None:
    members = [
        _member(1, tg_user="lead_a", tg_id=100, position="組長", role="開發組"),
        _member(2, tg_user="lead_b", tg_id=200, position="組長", role="行政組"),
    ]
    out = await _handlers(db, _StubRosterService(members)).tag_leaders(100, None)
    assert "@lead_a" not in out[0]
    assert "@lead_b" in out[0]


async def test_tl_excludes_self_by_username_case_insensitive(db: Database) -> None:
    # 名冊未填 telegram_id 時，以 username（不分大小寫）剔除自己
    members = [
        _member(1, tg_user="lead_a", position="組長", role="開發組"),
        _member(2, tg_user="lead_b", position="組長", role="行政組"),
    ]
    out = await _handlers(db, _StubRosterService(members)).tag_leaders(999, "Lead_A")
    assert "@lead_a" not in out[0]
    assert "@lead_b" in out[0]


async def test_tl_nobody_left(db: Database) -> None:
    members = [_member(1, tg_user="only", tg_id=100, position="組長", role="開發組")]
    out = await _handlers(db, _StubRosterService(members)).tag_leaders(100, "only")
    assert out == ["名冊裡沒有其他可以 tag 的全部組長＋總召。"]


# ------------------------------------------------------------------ #
# /ta：全體、分批、無 TG 對應
# ------------------------------------------------------------------ #
async def test_ta_tags_everyone_except_self(db: Database) -> None:
    members = [
        _member(1, tg_user="a", position="組長", role="開發組"),
        _member(2, tg_user="b", position="組員", role="開發組"),
        _member(3, tg_user="c", tg_id=300),
    ]
    out = await _handlers(db, _StubRosterService(members)).tag_all(300, None)
    assert len(out) == 1
    assert "@a" in out[0] and "@b" in out[0]
    assert "全體工作人員" in out[0]
    assert '"tg://user?id=300"' not in out[0]  # 自己（僅 tg_id 對應）被剔除


async def test_ta_batches_of_40(db: Database) -> None:
    members = [_member(i, tg_user=f"user{i:03d}") for i in range(85)]
    out = await _handlers(db, _StubRosterService(members)).tag_all(999, None)
    assert len(out) == 3
    assert out[0].startswith("🔔 召喚全體工作人員：")
    assert out[0].count("@") == TAG_BATCH_SIZE == 40
    assert out[1].count("@") == 40
    assert out[2].count("@") == 5
    assert "🔔" not in out[1] and "🔔" not in out[2]  # 標頭只在第一則


async def test_ta_tg_id_fallback_mention(db: Database) -> None:
    members = [_member(1, nick="小明", tg_id=111)]
    out = await _handlers(db, _StubRosterService(members)).tag_all(999, None)
    assert '<a href="tg://user?id=111">小明</a>' in out[0]


async def test_ta_unreachable_listed_in_last_message(db: Database) -> None:
    members = [
        _member(1, tg_user="a"),
        _member(2, nick="沒填<b>"),
    ]
    out = await _handlers(db, _StubRosterService(members)).tag_all(999, None)
    assert "@a" in out[0]
    assert "⚠️" in out[-1] and "通知不到" in out[-1]
    assert "沒填&lt;b&gt;" in out[-1]  # 動態內容有 escape


async def test_ta_all_unreachable_still_reports(db: Database) -> None:
    members = [_member(1, nick="甲"), _member(2, nick="乙")]
    out = await _handlers(db, _StubRosterService(members)).tag_all(999, None)
    assert len(out) == 1
    assert "🔔 召喚全體工作人員：" in out[0]
    assert "甲、乙" in out[0]


# ------------------------------------------------------------------ #
# 名冊拿不到
# ------------------------------------------------------------------ #
async def test_roster_unavailable(db: Database) -> None:
    out = await _handlers(db, _FailingRosterService()).tag_all(999, None)
    assert out == [ROSTER_UNAVAILABLE]


async def test_roster_not_wired(db: Database) -> None:
    out = await _handlers(db, None).tag_leaders(999, None)
    assert out == [ROSTER_UNAVAILABLE]


# ------------------------------------------------------------------ #
# gateway：送出 tag 訊息後刪掉指令訊息（防誤觸重複召喚）
# ------------------------------------------------------------------ #
class _FakeGroups:
    def is_authorized(self, chat_id: int) -> bool:
        return True


class _FakeAudit:
    async def write(self, **kw: object) -> None:
        return None


class _FakeCommands:
    """只提供 /tl /ta 兩個進入點，記錄收到的呼叫。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []

    async def tag_leaders(self, sender_id: int, sender_username: str | None) -> list[str]:
        self.calls.append((sender_id, sender_username))
        return ["🔔 召喚全部組長＋總召：@a @b", "第二批 @c"]

    async def tag_all(self, sender_id: int, sender_username: str | None) -> list[str]:
        self.calls.append((sender_id, sender_username))
        return ["🔔 召喚全體工作人員：@a"]


class _DeleteRecordingBot:
    def __init__(self, fail: bool = False) -> None:
        self.deleted: list[tuple[int, int]] = []
        self._fail = fail

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        if self._fail:
            raise RuntimeError("bot 不是管理員")
        self.deleted.append((chat_id, message_id))


def _tag_message() -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100, title="群"),
        message_id=555,
        message_thread_id=None,
        from_user=SimpleNamespace(id=7, username="yoru"),
    )


def _tag_gateway(bot: _DeleteRecordingBot) -> tuple[Gateway, _FakeCommands, list[str]]:
    commands = _FakeCommands()
    gw = Gateway(_settings(), groups=_FakeGroups(), audit=_FakeAudit(), commands=commands)  # type: ignore[arg-type]
    gw._app = SimpleNamespace(bot=bot)  # type: ignore[assignment]
    sent: list[str] = []

    async def _reply(message: object, text: str, reply_markup: object = None) -> int:
        sent.append(text)
        return 1

    gw._reply = _reply  # type: ignore[assignment]
    return gw, commands, sent


async def test_tl_deletes_the_command_message_after_replying() -> None:
    bot = _DeleteRecordingBot()
    gw, commands, sent = _tag_gateway(bot)
    message = _tag_message()

    await gw._dispatch(Action.CMD_TAG_LEADERS, message, "/tl")  # type: ignore[arg-type]

    assert len(sent) == 2  # 兩批照常送出
    assert commands.calls == [(7, "yoru")]
    assert bot.deleted == [(-100, 555)]  # 指令訊息被刪掉


async def test_ta_deletes_the_command_message() -> None:
    bot = _DeleteRecordingBot()
    gw, _, sent = _tag_gateway(bot)

    await gw._dispatch(Action.CMD_TAG_ALL, _tag_message(), "/ta")  # type: ignore[arg-type]

    assert len(sent) == 1
    assert bot.deleted == [(-100, 555)]


async def test_delete_failure_does_not_break_the_command() -> None:
    """bot 沒有「刪除訊息」權限時只記 warning，tag 訊息照常送出。"""
    bot = _DeleteRecordingBot(fail=True)
    gw, _, sent = _tag_gateway(bot)

    await gw._dispatch(Action.CMD_TAG_LEADERS, _tag_message(), "/tl")  # type: ignore[arg-type]

    assert len(sent) == 2
    assert bot.deleted == []


# ------------------------------------------------------------------ #
# 跨組兼任：主欄位不是組長，但 other_role 寫明在別組當組長
# ------------------------------------------------------------------ #
async def test_tl_includes_cross_team_leader(db: Database) -> None:
    """實例：編輯組副組長同時是行銷組組長；只看 position 會讓行銷組整組沒有組長。"""
    members = [
        _member(1, tg_user="lead_dev", position="組長", role="開發組"),
        _member(2, tg_user="ahliu", position="副組長", role="編輯組", other_role="行銷組組長"),
    ]
    out = await _handlers(db, _StubRosterService(members)).tag_leaders(999, None)
    body = "\n".join(out)
    assert "ahliu" in body
    assert "lead_dev" in body


async def test_tl_excludes_cross_team_deputy(db: Database) -> None:
    """「行銷組 副組長」含有「組長」兩字，不得被誤判為正職組長。"""
    members = [
        _member(1, tg_user="lead_dev", position="組長", role="開發組"),
        _member(2, tg_user="qiqi", position="副組長", role="活動組", other_role="行銷組 副組長"),
    ]
    out = await _handlers(db, _StubRosterService(members)).tag_leaders(999, None)
    assert "qiqi" not in "\n".join(out)


async def test_tl_ignores_cross_team_plain_members(db: Database) -> None:
    members = [
        _member(1, tg_user="lead_dev", position="組長", role="開發組"),
        _member(2, tg_user="ya", position="組員", role="編輯組", other_role="行銷組 組員、活動組 組員"),
        _member(3, tg_user="nocross", position="組員", role="行銷組", other_role="否，我沒有跨組"),
    ]
    out = await _handlers(db, _StubRosterService(members)).tag_leaders(999, None)
    body = "\n".join(out)
    assert "ya" not in body
    assert "nocross" not in body
