"""T4：名冊載入、RO-2 個資白名單、正規化、組長/總召判定、快取與 EC-11。"""

from __future__ import annotations

import dataclasses
import json

import pytest

from sitcon_bot.services.sheets_roster import (
    WHITELIST_HEADERS,
    Member,
    Roster,
    RosterService,
    RosterUnavailableError,
    parse_roster,
)

# 含大量白名單「以外」欄位的假表頭（email、github、本名、電話、匯款帳號…）
FULL_HEADER = [
    "nickname",
    "gitlab_username",
    "gitlab_id",
    "telegram_username",
    "telegram_id",
    "role",
    "position",
    "other_role",
    "email",
    "github_handle",
    "本名",
    "電話",
    "匯款帳號",
]

SENSITIVE_VALUES = ["yuan@example.com", "yuan-gh", "王小元", "0912345678", "1234-5678-9012"]


def _leader_row() -> list[str]:
    return [
        "Yuan", "yuan_tw", "1001", "@Yuan_TW", "555001", "開發組", "組長", "",
        "yuan@example.com", "yuan-gh", "王小元", "0912345678", "1234-5678-9012",
    ]


# ------------------------------------------------------------------ #
# RO-2：個資白名單（安全關鍵）
# ------------------------------------------------------------------ #
def test_member_has_only_whitelist_fields() -> None:
    m = Member(gitlab_id=1)
    assert set(dataclasses.asdict(m).keys()) == set(WHITELIST_HEADERS)


def test_ro2_sensitive_columns_never_loaded() -> None:
    result = parse_roster(FULL_HEADER, [_leader_row()])
    assert len(result.members) == 1
    m = result.members[0]

    # 白名單欄位正確
    assert m.gitlab_id == 1001
    assert m.nickname == "Yuan"
    assert m.role == "開發組"
    assert m.position == "組長"

    # 敏感值不得出現在任何序列化形式（結構、LLM 表）
    roster = Roster(result.members)
    blobs = [
        json.dumps(dataclasses.asdict(m), ensure_ascii=False),
        json.dumps(roster.to_llm_rows(), ensure_ascii=False),
        repr(m),
    ]
    for blob in blobs:
        for secret in SENSITIVE_VALUES:
            assert secret not in blob, f"個資外洩：{secret} 出現在 {blob[:60]}…"


def test_to_llm_rows_only_whitelist_keys() -> None:
    result = parse_roster(FULL_HEADER, [_leader_row()])
    for row in Roster(result.members).to_llm_rows():
        assert set(row.keys()) == set(WHITELIST_HEADERS)


# ------------------------------------------------------------------ #
# RO-3：正規化與跳列
# ------------------------------------------------------------------ #
def test_ro3_telegram_username_normalized() -> None:
    m = parse_roster(FULL_HEADER, [_leader_row()]).members[0]
    assert m.telegram_username == "yuan_tw"  # 去 @、轉小寫


def test_ro3_skip_blank_and_missing_gitlab_id() -> None:
    rows = [
        _leader_row(),
        ["", "", "", "", "", "", "", "", "", "", "", "", ""],  # 全空白
        ["NoId", "noid", "", "@noid", "", "行政組", "組員", "", "", "", "", "", ""],  # 缺 gitlab_id
        ["BadId", "badid", "abc", "@badid", "", "行政組", "組員", "", "", "", "", "", ""],  # 非數字
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
        ["Yuan", "yuan_tw", "1001", "@yuan", "1", "開發組", "組長", "", *[""] * 5],
        ["Leaf", "leaf", "1002", "@leaf", "2", "行政組", "組長", "", *[""] * 5],
        ["Amy", "amy", "1003", "@amy", "3", "", "總召", "", *[""] * 5],
        ["Bob", "bob", "1004", "@bob", "4", "", "總召", "", *[""] * 5],
        ["Cat", "cat", "1005", "@cat", "5", "開發組", "組員", "", *[""] * 5],
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
