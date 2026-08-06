"""籌備時程表（里程碑）載入與查詢（NT-1～NT-3）。

以 service account 讀取「SITCON 2027 籌備時程表」的指定分頁，解析成 Milestone 後記憶體快取
（TTL；表格變動不頻繁，/reload 可強制刷新）。查詢為記憶體內比對，不再打外部 API。

來源欄位（以表頭字串定位，容忍欄位順序變動；其餘欄位一律忽略）：
    事件名稱 / 開始時間 / 結束時間 / 主導組別 / 備註

日期格式在來源並不一致（`2026/09/07` 與 `2026/2/6` 混用），且有空白（尚未決定的事項），
故解析採寬鬆比對，無法解析即視為無日期並排除於預告之外。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .google_http import GOOGLE_NUM_RETRIES, build_google_service, request_http

log = logging.getLogger(__name__)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

# 表頭 → 欄位。來源為繁中表頭；找不到就當該欄不存在（該欄一律為空）。
COL_NAME = "事件名稱"
# 2026-08-06 發現來源表頭把「事件名稱」改成了「項目」；兩種寫法都接受（NT-1 修訂）。
COL_NAME_ALIASES = ("項目",)
COL_START = "開始時間"
COL_END = "結束時間"
COL_TEAM = "主導組別"
COL_NOTE = "備註"

# 主導組別留空時的顯示名稱（僅「全部組別」訂閱者收得到）。
UNASSIGNED_TEAM = "未分組"

_DATE_RE = re.compile(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$")
_TEAM_SPLIT_RE = re.compile(r"[,，、/\s]+")

_Clock = Callable[[], float]


class MilestoneScheduleUnavailableError(RuntimeError):
    """時程表載入失敗且無可用快取。"""


def parse_date(value: str) -> date | None:
    """把來源日期字串轉成 date；空白或無法解析回 None。"""
    s = (value or "").strip()
    m = _DATE_RE.match(s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def norm_team(name: str) -> str:
    """組名正規化：去空白、去尾綴「組」，讓「開發」與「開發組」可對應（同 RO-3 精神）。"""
    n = (name or "").strip()
    if len(n) > 1 and n.endswith("組"):
        n = n[:-1]
    return n


@dataclass(frozen=True, slots=True)
class Milestone:
    """時程表上的一列事件。"""

    name: str
    start: date | None
    end: date | None
    team: str
    note: str = ""

    @property
    def is_single_day(self) -> bool:
        return self.start is not None and (self.end is None or self.end == self.start)


# 事件相對於查詢日的角色：當天／起始日／最後一天。
KIND_SINGLE = "single"
KIND_START = "start"
KIND_END = "end"
_KIND_RANK = {KIND_SINGLE: 0, KIND_START: 1, KIND_END: 2}


@dataclass(frozen=True, slots=True)
class MilestoneHit:
    milestone: Milestone
    kind: str


def parse_milestones(header: list[str], rows: list[list[str]]) -> list[Milestone]:
    """把原始表格解析為 Milestone 清單；無事件名稱或無任何日期的列一律略過。"""
    if COL_NAME not in header:
        header = [COL_NAME if h in COL_NAME_ALIASES else h for h in header]
    idx = {name: header.index(name) for name in (COL_NAME, COL_START, COL_END, COL_TEAM, COL_NOTE) if name in header}

    def cell(row: list[str], name: str) -> str:
        i = idx.get(name)
        if i is None or i >= len(row):
            return ""
        return (row[i] or "").strip()

    out: list[Milestone] = []
    for row in rows:
        name = cell(row, COL_NAME)
        if not name:
            continue
        start = parse_date(cell(row, COL_START))
        end = parse_date(cell(row, COL_END))
        if start is None and end is None:
            continue  # 日期未定（來源常見）：不可能成為某日的預告
        out.append(
            Milestone(
                name=name,
                start=start,
                end=end,
                team=cell(row, COL_TEAM) or UNASSIGNED_TEAM,
                note=cell(row, COL_NOTE),
            )
        )
    return out


class MilestoneSchedule:
    """載入後的時程查詢視圖（純記憶體）。"""

    def __init__(self, milestones: list[Milestone]) -> None:
        self._milestones = milestones

    def __len__(self) -> int:
        return len(self._milestones)

    @property
    def milestones(self) -> list[Milestone]:
        return list(self._milestones)

    def teams(self) -> list[str]:
        """出現過的主導組別（依名稱排序，供 /notify_on 驗證與提示）。"""
        return sorted({m.team for m in self._milestones if m.team})

    def for_date(self, target: date) -> list[MilestoneHit]:
        """target 當日「發生什麼」：單日事件、當日起跑、當日為最後一天。

        跨日事件的中間日不列入——每天重播同一件事只會讓人把通知靜音。
        """
        hits: list[MilestoneHit] = []
        for m in self._milestones:
            if m.start == target and m.is_single_day:
                hits.append(MilestoneHit(m, KIND_SINGLE))
            elif m.start == target:
                hits.append(MilestoneHit(m, KIND_START))
            elif m.end == target:
                hits.append(MilestoneHit(m, KIND_END))
        hits.sort(key=lambda h: (_KIND_RANK.get(h.kind, 9), h.milestone.team, h.milestone.name))
        return hits


def select_for_teams(
    hits: list[MilestoneHit],
    teams: tuple[str, ...] | list[str],
    always_teams: tuple[str, ...] | list[str] = (),
) -> list[MilestoneHit]:
    """依訂閱組別過濾。teams 為空＝全部組別（不過濾）。

    always_teams（預設「全體」「重要日期」）不論訂閱哪幾組都會收到——那些本來就是全員共同事項。
    """
    if not teams:
        return list(hits)
    wanted = {norm_team(t) for t in teams if norm_team(t)}
    wanted |= {norm_team(t) for t in always_teams if norm_team(t)}
    return [h for h in hits if norm_team(h.milestone.team) in wanted]


class MilestoneFetcher(Protocol):
    async def fetch(self) -> tuple[list[str], list[list[str]]]: ...


class GoogleSheetMilestoneFetcher:
    """以 service account 讀取時程表的指定分頁（以 gid 解析分頁名，同 RO-1 作法）。"""

    def __init__(self, sa_json_path: str, sheet_id: str, gid: int) -> None:
        self._sa_json_path = sa_json_path
        self._sheet_id = sheet_id
        self._gid = gid
        self._service: object | None = None
        self._creds: object | None = None

    def _fetch_sync(self) -> tuple[list[str], list[list[str]]]:
        if self._service is None:
            self._service, self._creds = build_google_service("sheets", "v4", self._sa_json_path, [SHEETS_SCOPE])
        service = self._service
        meta = (
            service.spreadsheets()  # type: ignore[attr-defined]
            .get(spreadsheetId=self._sheet_id, fields="sheets(properties(sheetId,title))")
            .execute(http=request_http(self._creds), num_retries=GOOGLE_NUM_RETRIES)
        )
        title = None
        for s in meta.get("sheets", []):
            if s["properties"]["sheetId"] == self._gid:
                title = s["properties"]["title"]
                break
        if title is None:
            raise MilestoneScheduleUnavailableError(f"時程表找不到 gid={self._gid} 對應的分頁")
        resp = (
            service.spreadsheets()  # type: ignore[attr-defined]
            .values()
            .get(spreadsheetId=self._sheet_id, range=f"'{title}'!A:Z")
            .execute(http=request_http(self._creds), num_retries=GOOGLE_NUM_RETRIES)
        )
        values: list[list[str]] = resp.get("values", [])
        header = values[0] if values else []
        rows = values[1:] if len(values) > 1 else []
        return header, rows

    async def fetch(self) -> tuple[list[str], list[list[str]]]:
        return await asyncio.to_thread(self._fetch_sync)


class MilestoneScheduleService:
    """TTL 快取的時程表；載入失敗沿用舊快取（同 EC-11 精神）、single-flight。"""

    def __init__(self, fetcher: MilestoneFetcher, ttl_seconds: int, clock: _Clock = time.monotonic) -> None:
        self._fetcher = fetcher
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: MilestoneSchedule | None = None
        self._at: float | None = None
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._cache is not None and self._at is not None and (self._clock() - self._at) < self._ttl

    async def get(self, *, force: bool = False) -> MilestoneSchedule:
        if self._fresh() and not force:
            assert self._cache is not None
            return self._cache
        async with self._lock:
            if self._fresh() and not force:
                assert self._cache is not None
                return self._cache
            return await self._fetch_locked()

    async def _fetch_locked(self) -> MilestoneSchedule:
        try:
            header, rows = await self._fetcher.fetch()
        except Exception as exc:
            if self._cache is not None:
                log.warning("時程表更新失敗，沿用舊快取", exc_info=True)
                return self._cache
            raise MilestoneScheduleUnavailableError("時程表載入失敗且無可用快取") from exc
        self._cache = MilestoneSchedule(parse_milestones(header, rows))
        self._at = self._clock()
        if rows and not len(self._cache):
            # 有資料卻一筆都解析不出來，幾乎都是表頭又被改名（2026-08-06 就發生過一次）。
            log.warning("時程表有 %d 列但解析出 0 筆里程碑，表頭可能改名了：%r", len(rows), header)
        log.info("時程表載入 %d 筆里程碑", len(self._cache))
        return self._cache

    async def reload(self) -> MilestoneSchedule:
        return await self.get(force=True)


def build_milestone_schedule_service(
    sa_json_path: str, sheet_id: str, gid: int, ttl_seconds: int
) -> MilestoneScheduleService:
    return MilestoneScheduleService(GoogleSheetMilestoneFetcher(sa_json_path, sheet_id, gid), ttl_seconds)
