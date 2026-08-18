"""T4：名冊載入、完整欄位（RO-2 修訂 2026-08-18）、正規化、組長/總召判定、快取與 EC-11。"""

from __future__ import annotations

import dataclasses
import json

import pytest

from sitcon_bot.services.sheets_roster import (
    LOADED_HEADERS,
    Member,
    Roster,
    RosterService,
    RosterUnavailableError,
    parse_roster,
)

# bot_use 分頁完整表頭（RO-2 修訂：全數載入）＋清單外的未知欄位（本名、電話、匯款帳號…仍丟棄）
FULL_HEADER = [
    "email",
    "github_username",
    "github_id",
    "gitlab_username",
    "gitlab_id",
    "telegram_username",
    "telegram_id",
    "nickname",
    "role",
    "position",
    "other_role",
    "本名",
    "電話",
    "匯款帳號",
]

# 清單外欄位的值：即使出現在表格，也不得進入任何序列化形式
UNKNOWN_COLUMN_VALUES = ["王小元", "0912345678", "1234-5678-9012"]


def _leader_row() -> list[str]:
    return [
        "yuan@example.com", "yuan-gh", "778899", "yuan_tw", "1001", "@Yuan_TW", "555001",
        "Yuan", "開發組", "組長", "",
        "王小元", "0912345678", "1234-5678-9012",
    ]


# ------------------------------------------------------------------ #
# RO-2 修訂（2026-08-18）：完整欄位載入；未知欄位仍丟棄
# ------------------------------------------------------------------ #
def test_member_fields_match_loaded_headers() -> None:
    m = Member(gitlab_id=1)
    assert set(dataclasses.asdict(m).keys()) == set(LOADED_HEADERS)


def test_full_columns_loaded_including_email_and_github() -> None:
    result = parse_roster(FULL_HEADER, [_leader_row()])
    assert len(result.members) == 1
    m = result.members[0]

    assert m.gitlab_id == 1001
    assert m.nickname == "Yuan"
    assert m.role == "開發組"
    assert m.position == "組長"
    # RO-2 修訂：email／github 欄位完整載入
    assert m.email == "yuan@example.com"
    assert m.github_username == "yuan-gh"
    assert m.github_id == 778899

    # 清單外的未知欄位不得出現在任何序列化形式（結構、LLM 表）
    roster = Roster(result.members)
    blobs = [
        json.dumps(dataclasses.asdict(m), ensure_ascii=False),
        json.dumps(roster.to_llm_rows(), ensure_ascii=False),
        repr(m),
    ]
    for blob in blobs:
        for value in UNKNOWN_COLUMN_VALUES:
            assert value not in blob, f"未知欄位外洩：{value} 出現在 {blob[:60]}…"


def test_to_llm_rows_carries_full_fields() -> None:
    result = parse_roster(FULL_HEADER, [_leader_row()])
    for row in Roster(result.members).to_llm_rows():
        assert set(row.keys()) == set(LOADED_HEADERS)
        assert row["email"] == "yuan@example.com"  # LLM 對照表含 email（RO-7 修訂）


# ------------------------------------------------------------------ #
# RO-3：正規化與跳列
# ------------------------------------------------------------------ #
def test_ro3_telegram_username_normalized() -> None:
    m = parse_roster(FULL_HEADER, [_leader_row()]).members[0]
    assert m.telegram_username == "yuan_tw"  # 去 @、轉小寫


def _row(
    nickname: str = "",
    gitlab_username: str = "",
    gitlab_id: str = "",
    telegram_username: str = "",
    telegram_id: str = "",
    role: str = "",
    position: str = "",
    email: str = "",
    github_username: str = "",
    github_id: str = "",
) -> list[str]:
    """依 FULL_HEADER 欄位順序組一列資料（未列參數者留空）。"""
    return [
        email, github_username, github_id, gitlab_username, gitlab_id,
        telegram_username, telegram_id, nickname, role, position, "", "", "", "",
    ]


def test_ro3_skip_blank_and_missing_gitlab_id() -> None:
    rows = [
        _leader_row(),
        [""] * len(FULL_HEADER),  # 全空白
        _row(nickname="NoId", gitlab_username="noid", telegram_username="@noid", role="行政組"),  # 缺 gitlab_id
        _row(nickname="BadId", gitlab_username="badid", gitlab_id="abc", role="行政組"),  # 非數字
    ]
    result = parse_roster(FULL_HEADER, rows)
    assert len(result.members) == 1  # 只有 leader 列有效
    assert result.skipped == 2  # 缺 id + 非數字 id（全空白列不計入 skipped）
    assert len(result.skipped_reasons) == 2


# ------------------------------------------------------------------ #
# RO-4：組長 / 總召判定
# ------------------------------------------------------------------ #
def _sample_roster() -> Roster:
    rows = [
        _row("Yuan", "yuan_tw", "1001", "@yuan", "1", "開發組", "組長"),
        _row("Leaf", "leaf", "1002", "@leaf", "2", "行政組", "組長"),
        _row("Amy", "amy", "1003", "@amy", "3", "", "總召"),
        _row("Bob", "bob", "1004", "@bob", "4", "", "總召"),
        _row("Cat", "cat", "1005", "@cat", "5", "開發組", "組員"),
    ]
    return Roster(parse_roster(FULL_HEADER, rows).members)


def test_ro4_leader_lookup_with_team_normalization() -> None:
    r = _sample_roster()
    assert r.leader_of("開發組").nickname == "Yuan"
    assert r.leader_of("開發").nickname == "Yuan"  # 尾綴「組」正規化
    assert r.leader_of("行政組").nickname == "Leaf"


def test_ro4_no_leader_returns_none() -> None:
    r = _sample_roster()
    assert r.leader_of("設計組") is None  # 名冊中無此組組長 → fallback 由上層處理


def test_ro4_chiefs_returns_all() -> None:
    r = _sample_roster()
    chiefs = {m.nickname for m in r.chiefs()}
    assert chiefs == {"Amy", "Bob"}


# ------------------------------------------------------------------ #
# RO-5：人名查找（基礎）
# ------------------------------------------------------------------ #
def test_ro5_search_by_nickname_partial() -> None:
    r = _sample_roster()
    hits = r.search_by_name("yua")
    assert [m.nickname for m in hits] == ["Yuan"]


def test_ro5_search_by_username_and_telegram() -> None:
    r = _sample_roster()
    assert [m.nickname for m in r.search_by_name("leaf")] == ["Leaf"]
    assert [m.nickname for m in r.search_by_name("@amy")] == ["Amy"]


def test_ro5_search_zero_hits() -> None:
    assert _sample_roster().search_by_name("nobody") == []


def test_by_telegram_id() -> None:
    r = _sample_roster()
    assert r.by_telegram_id(1).nickname == "Yuan"
    assert r.by_telegram_id(999) is None


# ------------------------------------------------------------------ #
# RosterService：快取（RO-6）與 EC-11
# ------------------------------------------------------------------ #
class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeFetcher:
    def __init__(self, payload=None, exc: Exception | None = None) -> None:
        self.payload = payload
        self.exc = exc
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.payload


def _payload(rows):
    return ("英文欄名", FULL_HEADER, rows)


async def test_cache_hit_within_ttl() -> None:
    clock = FakeClock()
    fetcher = FakeFetcher(_payload([_leader_row()]))
    svc = RosterService(fetcher, ttl_seconds=60, clock=clock)

    await svc.get()
    await svc.get()
    clock.advance(59)
    await svc.get()
    assert fetcher.calls == 1  # TTL 內只抓一次


async def test_cache_expires_after_ttl() -> None:
    clock = FakeClock()
    fetcher = FakeFetcher(_payload([_leader_row()]))
    svc = RosterService(fetcher, ttl_seconds=60, clock=clock)

    await svc.get()
    clock.advance(61)
    await svc.get()
    assert fetcher.calls == 2


async def test_reload_forces_refetch() -> None:
    fetcher = FakeFetcher(_payload([_leader_row()]))
    svc = RosterService(fetcher, ttl_seconds=3600, clock=FakeClock())
    await svc.get()
    await svc.reload()
    assert fetcher.calls == 2


async def test_ec11_fallback_to_stale_cache_on_failure() -> None:
    clock = FakeClock()
    fetcher = FakeFetcher(_payload([_leader_row()]))
    svc = RosterService(fetcher, ttl_seconds=60, clock=clock)
    await svc.get()  # 成功載入

    fetcher.exc = RuntimeError("Sheets 503")
    clock.advance(61)  # 快取過期，強制重抓 → 失敗
    roster = await svc.get()
    assert len(roster) == 1  # 沿用舊快取（EC-11）


async def test_ec11_raises_when_no_cache() -> None:
    fetcher = FakeFetcher(exc=RuntimeError("Sheets down"))
    svc = RosterService(fetcher, ttl_seconds=60, clock=FakeClock())
    with pytest.raises(RosterUnavailableError):
        await svc.get()
