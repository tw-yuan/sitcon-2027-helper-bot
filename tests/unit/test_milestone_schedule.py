"""NT-1～NT-3：籌備時程表的解析、當日查詢、組別過濾與快取行為。"""

from __future__ import annotations

from datetime import date

import pytest

from sitcon_bot.services.milestone_schedule import (
    KIND_END,
    KIND_SINGLE,
    KIND_START,
    UNASSIGNED_TEAM,
    MilestoneSchedule,
    MilestoneScheduleService,
    MilestoneScheduleUnavailableError,
    norm_team,
    parse_date,
    parse_milestones,
    select_for_teams,
)

# 真實表格的表頭（欄位順序刻意與程式常數不同，驗證以表頭字串定位）
HEADER = ["事件名稱", "開始時間", "結束時間", "耗時 days", "主導組別", "備註", "更新請打勾", "議程組專用"]
ROWS = [
    ["二籌", "2026/09/12", "2026/09/12", "0", "全體", "", "FALSE", "FALSE"],
    ["開發組零籌", "2026/09/05", "2026/09/12", "7", "開發組", "暫定，到時候喬一下時間。", "FALSE", "FALSE"],
    ["開發組招募表單填寫", "2026/07/28", "2026/08/21", "24", "開發組", "", "FALSE", "FALSE"],
    ["人事凍結", "", "", "0", "行政組", "大約是工人相見歡一個月前", "FALSE", "FALSE"],  # 日期未定 → 排除
    ["第一版官網開發 / 上線", "2026/09/05", "2026/09/21", "16", "", "", "FALSE", "FALSE"],  # 無組別
    ["", "", "", "0", "", "", "FALSE", "FALSE"],  # 空列
    ["東華上學期開學", "2026/09/07", "2026/09/07", "0", "重要日期", "", "FALSE", "TRUE"],
]


def _schedule() -> MilestoneSchedule:
    return MilestoneSchedule(parse_milestones(HEADER, ROWS))


# ------------------------------------------------------------------ #
# 解析
# ------------------------------------------------------------------ #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026/09/07", date(2026, 9, 7)),
        ("2026/2/6", date(2026, 2, 6)),  # 來源未補零
        (" 2027-03-13 ", date(2027, 3, 13)),
        ("", None),
        ("待定", None),
        ("2026/13/01", None),  # 不存在的月份
    ],
)
def test_parse_date(raw: str, expected: date | None) -> None:
    assert parse_date(raw) == expected


def test_parse_skips_rows_without_name_or_date() -> None:
    ms = parse_milestones(HEADER, ROWS)
    names = [m.name for m in ms]
    assert "人事凍結" not in names  # 日期未定
    assert "" not in names
    assert len(ms) == 5


def test_parse_defaults_empty_team() -> None:
    ms = {m.name: m for m in parse_milestones(HEADER, ROWS)}
    assert ms["第一版官網開發 / 上線"].team == UNASSIGNED_TEAM
    assert ms["開發組零籌"].note.startswith("暫定")


def test_teams_listing() -> None:
    assert _schedule().teams() == ["全體", "未分組", "重要日期", "開發組"]


@pytest.mark.parametrize("raw,expected", [("開發組", "開發"), ("開發", "開發"), (" 全體 ", "全體"), ("組", "組")])
def test_norm_team(raw: str, expected: str) -> None:
    assert norm_team(raw) == expected


# ------------------------------------------------------------------ #
# 當日查詢
# ------------------------------------------------------------------ #
def test_for_date_single_start_end() -> None:
    hits = _schedule().for_date(date(2026, 9, 12))
    got = {h.milestone.name: h.kind for h in hits}
    assert got == {"二籌": KIND_SINGLE, "開發組零籌": KIND_END}


def test_for_date_start_of_multi_day() -> None:
    hits = _schedule().for_date(date(2026, 9, 5))
    got = {h.milestone.name: h.kind for h in hits}
    assert got == {"開發組零籌": KIND_START, "第一版官網開發 / 上線": KIND_START}


def test_for_date_skips_middle_days() -> None:
    """跨日事件的中間日不重播——否則每天都收到同一件事。"""
    assert _schedule().for_date(date(2026, 9, 8)) == []


def test_for_date_empty_day() -> None:
    assert _schedule().for_date(date(2026, 1, 1)) == []


def test_hits_sorted_single_before_start_before_end() -> None:
    hits = _schedule().for_date(date(2026, 9, 12))
    assert [h.kind for h in hits] == [KIND_SINGLE, KIND_END]


# ------------------------------------------------------------------ #
# 組別過濾
# ------------------------------------------------------------------ #
def test_select_all_teams_when_empty() -> None:
    hits = _schedule().for_date(date(2026, 9, 12))
    assert select_for_teams(hits, ()) == hits


def test_select_specific_team() -> None:
    hits = _schedule().for_date(date(2026, 9, 12))
    picked = select_for_teams(hits, ("開發組",))
    assert [h.milestone.name for h in picked] == ["開發組零籌"]


def test_select_includes_always_teams() -> None:
    """訂閱單一組別時，「全體」「重要日期」仍會收到（NT-3）。"""
    hits = _schedule().for_date(date(2026, 9, 12))
    picked = select_for_teams(hits, ("開發組",), ("全體", "重要日期"))
    assert {h.milestone.name for h in picked} == {"二籌", "開發組零籌"}


def test_select_tolerates_team_suffix() -> None:
    hits = _schedule().for_date(date(2026, 9, 12))
    assert select_for_teams(hits, ("開發",)) == select_for_teams(hits, ("開發組",))


def test_unassigned_only_reaches_all_subscribers() -> None:
    hits = _schedule().for_date(date(2026, 9, 5))
    assert len(select_for_teams(hits, ())) == 2
    assert [h.milestone.name for h in select_for_teams(hits, ("開發組",))] == ["開發組零籌"]


# ------------------------------------------------------------------ #
# 快取服務
# ------------------------------------------------------------------ #
class _Fetcher:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def fetch(self) -> tuple[list[str], list[list[str]]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("sheets down")
        return HEADER, ROWS


async def test_service_caches_within_ttl() -> None:
    fetcher = _Fetcher()
    now = [1000.0]
    svc = MilestoneScheduleService(fetcher, ttl_seconds=600, clock=lambda: now[0])
    await svc.get()
    await svc.get()
    assert fetcher.calls == 1
    now[0] += 601
    await svc.get()
    assert fetcher.calls == 2


async def test_service_reload_forces_refetch() -> None:
    fetcher = _Fetcher()
    svc = MilestoneScheduleService(fetcher, ttl_seconds=600, clock=lambda: 0.0)
    await svc.get()
    await svc.reload()
    assert fetcher.calls == 2


async def test_service_keeps_stale_cache_on_failure() -> None:
    fetcher = _Fetcher()
    now = [0.0]
    svc = MilestoneScheduleService(fetcher, ttl_seconds=10, clock=lambda: now[0])
    first = await svc.get()
    now[0] += 100
    fetcher.fail = True
    assert await svc.get() is first  # 沿用舊快取，不讓預告整個消失


async def test_service_raises_when_no_cache() -> None:
    fetcher = _Fetcher()
    fetcher.fail = True
    svc = MilestoneScheduleService(fetcher, ttl_seconds=10, clock=lambda: 0.0)
    with pytest.raises(MilestoneScheduleUnavailableError):
        await svc.get()
