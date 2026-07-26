"""名冊載入（RO-1～RO-8）。

【硬性・個資隔離 RO-2】：僅擷取白名單欄位（以表頭字串比對）；其餘欄位（含 email、
github_*、本名、電話、匯款帳號…）於**載入層即丟棄**，永不進入記憶體結構、LLM context、
日誌或回覆。此限制以「白名單解析 + 僅含白名單欄位的 frozen dataclass」在程式層強制，
並有專屬測試（NFR-5）。

分工：
  parse_roster(...)         純函式，強制 RO-2/RO-3，最易測。
  Roster                    載入後的查詢視圖（組長/總召判定 RO-4、人名查找 RO-5、LLM 表 RO-7）。
  SheetsFetcher / Google…   Google Sheets I/O（可注入假物件供測試）。
  RosterService             TTL 快取（RO-6）、/reload、載入失敗沿用舊快取（EC-11）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .google_http import GOOGLE_NUM_RETRIES, build_google_service, request_http

log = logging.getLogger(__name__)

# RO-2 白名單：只有這些表頭對應的欄位會被載入，其餘一律丟棄。
WHITELIST_HEADERS: tuple[str, ...] = (
    "nickname",
    "gitlab_username",
    "gitlab_id",
    "telegram_username",
    "telegram_id",
    "role",
    "position",
    "other_role",
)
_WHITELIST_SET = frozenset(WHITELIST_HEADERS)

POSITION_LEADER = "組長"
POSITION_CHIEF = "總召"


class RosterUnavailableError(RuntimeError):
    """名冊無法取得且無可用快取（EC-11）。"""


@dataclass(frozen=True, slots=True)
class Member:
    """名冊成員——**只有 RO-2 白名單欄位**，結構上無法承載其他個資。"""

    gitlab_id: int
    nickname: str | None = None
    gitlab_username: str | None = None
    telegram_username: str | None = None
    telegram_id: int | None = None
    role: str | None = None
    position: str | None = None
    other_role: str | None = None


@dataclass(slots=True)
class RosterParseResult:
    members: list[Member]
    skipped: int = 0
    skipped_reasons: list[str] = field(default_factory=list)


def _to_int(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _norm_team(name: str | None) -> str:
    """組名正規化：去空白、去尾綴「組」，讓「開發」與「開發組」可對應。"""
    if not name:
        return ""
    n = name.strip()
    if n.endswith("組") and len(n) > 1:
        n = n[:-1]
    return n


def parse_roster(header: list[str], rows: list[list[str]]) -> RosterParseResult:
    """把原始表格解析為 Member 清單，強制 RO-2 白名單與 RO-3 正規化。

    - 僅讀取白名單表頭對應的欄位（比對時去空白、不分大小寫）。
    - telegram_username 去前導 @ 並轉小寫。
    - 空白列、缺（或非數字）gitlab_id 之列跳過並計入 skipped（供啟動日誌）。
    """
    col: dict[str, int] = {}
    for i, h in enumerate(header):
        key = (h or "").strip().lower()
        if key in _WHITELIST_SET and key not in col:
            col[key] = i

    members: list[Member] = []
    skipped = 0
    reasons: list[str] = []

    for rownum, r in enumerate(rows, start=2):  # 第 1 列為表頭

        def cell(name: str, row: list[str] = r) -> str:
            j = col.get(name)
            if j is None or j >= len(row):
                return ""
            return (row[j] or "").strip()

        # 完全空白列：靜默跳過
        if not any(cell(w) for w in WHITELIST_HEADERS):
            continue

        gitlab_id = _to_int(cell("gitlab_id"))
        if gitlab_id is None:
            skipped += 1
            reasons.append(f"第 {rownum} 列缺少或非數字 gitlab_id，已跳過")
            continue

        tg_username = cell("telegram_username").lstrip("@").lower() or None
        members.append(
            Member(
                gitlab_id=gitlab_id,
                nickname=cell("nickname") or None,
                gitlab_username=cell("gitlab_username") or None,
                telegram_username=tg_username,
                telegram_id=_to_int(cell("telegram_id")),
                role=cell("role") or None,
                position=cell("position") or None,
                other_role=cell("other_role") or None,
            )
        )

    return RosterParseResult(members=members, skipped=skipped, skipped_reasons=reasons)


class Roster:
    """載入後的名冊查詢視圖。"""

    def __init__(self, members: list[Member]) -> None:
        self._members = members
        self._by_tid: dict[int, Member] = {
            m.telegram_id: m for m in members if m.telegram_id is not None
        }

    @property
    def members(self) -> list[Member]:
        return list(self._members)

    def __len__(self) -> int:
        return len(self._members)

    def leader_of(self, team: str) -> Member | None:
        """RO-4：某組組長 = position==組長 且 role==該組（組名正規化後比對）。"""
        target = _norm_team(team)
        if not target:
            return None
        for m in self._members:
            if m.position == POSITION_LEADER and _norm_team(m.role) == target:
                return m
        return None

    def chiefs(self) -> list[Member]:
        """RO-4：所有總召（position==總召）。"""
        return [m for m in self._members if m.position == POSITION_CHIEF]

    def by_telegram_id(self, telegram_id: int) -> Member | None:
        return self._by_tid.get(telegram_id)

    def search_by_name(self, query: str) -> list[Member]:
        """RO-5 基礎人名查找：比對 nickname（不分大小寫、容忍部分符合）、
        gitlab_username、telegram_username。完整多筆/零筆的反問流程於 people_tools（T8）。"""
        q = query.strip().lower().lstrip("@")
        if not q:
            return []
        hits: list[Member] = []
        for m in self._members:
            nick = (m.nickname or "").lower()
            gl = (m.gitlab_username or "").lower()
            tg = (m.telegram_username or "").lower()
            if (nick and q in nick) or (gl and q == gl) or (tg and q == tg):
                hits.append(m)
        return hits

    def to_llm_rows(self) -> list[dict[str, object]]:
        """RO-7：供 LLM 使用的精簡對照表，僅含白名單欄位。"""
        return [asdict(m) for m in self._members]


class SheetsFetcher(Protocol):
    """名冊來源抽象：回傳 (分頁標題, 表頭, 資料列)。方便以假物件測試。"""

    async def fetch(self) -> tuple[str, list[str], list[list[str]]]: ...


_Clock = Callable[[], float]


class RosterService:
    """名冊快取服務：TTL（RO-6）、/reload 強制刷新、載入失敗沿用舊快取（EC-11）。"""

    def __init__(
        self,
        fetcher: SheetsFetcher,
        ttl_seconds: int,
        clock: _Clock = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._ttl = ttl_seconds
        self._clock = clock
        self._roster: Roster | None = None
        self._loaded_at: float | None = None

    def _is_fresh(self) -> bool:
        return (
            self._roster is not None
            and self._loaded_at is not None
            and (self._clock() - self._loaded_at) < self._ttl
        )

    async def get(self, *, force: bool = False) -> Roster:
        if not force and self._is_fresh():
            assert self._roster is not None
            return self._roster
        try:
            _title, header, rows = await self._fetcher.fetch()
        except Exception as exc:
            if self._roster is not None:
                log.warning("名冊重新載入失敗（%s），沿用上次快取（EC-11）", exc)
                return self._roster
            raise RosterUnavailableError("名冊載入失敗且無可用快取") from exc

        result = parse_roster(header, rows)
        if result.skipped:
            log.warning("名冊載入：跳過 %d 列。%s", result.skipped, "；".join(result.skipped_reasons))
        self._roster = Roster(result.members)
        self._loaded_at = self._clock()
        log.info("名冊載入完成：成員 %d 人", len(self._roster))
        return self._roster

    async def reload(self) -> Roster:
        return await self.get(force=True)

    def cached(self) -> Roster | None:
        return self._roster


# --------------------------------------------------------------------------- #
# Google Sheets I/O（NFR-4：僅申請 spreadsheets.readonly）
# --------------------------------------------------------------------------- #
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


class GoogleSheetsFetcher:
    """以 service account 讀取指定 Sheet 的單一分頁（RO-1）。

    先以 gid 解析分頁標題，再用 values API 取原始字串（AGENTS 6.2：不走 Drive export，
    避免 markdown escape 汙染）。Google client 為同步，包在 to_thread 中呼叫。
    """

    def __init__(self, sa_json_path: str, sheet_id: str, gid: int) -> None:
        self._sa_json_path = sa_json_path
        self._sheet_id = sheet_id
        self._gid = gid
        self._service: object | None = None
        self._creds: object | None = None

    def _fetch_sync(self) -> tuple[str, list[str], list[list[str]]]:
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
            raise RosterUnavailableError(f"找不到 gid={self._gid} 對應的分頁")
        resp = (
            service.spreadsheets()  # type: ignore[attr-defined]
            .values()
            .get(spreadsheetId=self._sheet_id, range=f"'{title}'!A:Z")
            .execute(http=request_http(self._creds), num_retries=GOOGLE_NUM_RETRIES)
        )
        values: list[list[str]] = resp.get("values", [])
        header = values[0] if values else []
        rows = values[1:] if len(values) > 1 else []
        return title, header, rows

    async def fetch(self) -> tuple[str, list[str], list[list[str]]]:
        import asyncio

        return await asyncio.to_thread(self._fetch_sync)


def build_roster_service(
    sa_json_path: str, sheet_id: str, gid: int, ttl_seconds: int
) -> RosterService:
    fetcher = GoogleSheetsFetcher(sa_json_path, sheet_id, gid)
    return RosterService(fetcher, ttl_seconds)


# 型別匯出，方便他處引用
FetchResult = tuple[str, list[str], list[list[str]]]
FetchCallable = Callable[[], Awaitable[FetchResult]]
