"""快速 tag 指令（/tl /ta）：名單篩選、剔除自己、分批與退化訊息。"""

from __future__ import annotations

from sitcon_bot.auth.groups import GroupStore
from sitcon_bot.services.sheets_roster import Member, Roster, RosterUnavailableError
from sitcon_bot.settings import Settings
from sitcon_bot.storage.db import Database
from sitcon_bot.telegram.commands import (
    ROSTER_UNAVAILABLE,
    TAG_BATCH_SIZE,
    CommandHandlers,
)

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
) -> Member:
    return Member(
        gitlab_id=gid,
        nickname=nick,
        telegram_username=tg_user,
        telegram_id=tg_id,
        position=position,
        role=role,
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
