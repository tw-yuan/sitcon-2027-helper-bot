"""Google Drive 唯讀搜尋（DR-1～DR-9）。

範圍限縮於指定的資料夾 ID（SITCON 2025／2026／2027，設於 .env 的 name=id 對應）。
service account 只需被分享這兩個資料夾即可（不必是共用雲端硬碟成員）——查詢一律用
corpora=allDrives + includeItemsFromAllDrives + supportsAllDrives。

範圍判定採「隨查隨解」：對每個命中檔案，沿其 parents 鏈往上走到某個範圍根資料夾才算在範圍內，
並就地組出路徑；folder metadata 以快取避免重複查詢。API 呼叫量因此只隨命中筆數增長，不需先枚舉
整棵資料夾樹（避免大量資料夾時逾時）。

【硬性 DR-4】結果只回 metadata：檔名、路徑、Drive URL、檔案類型；不回檔案內容／縮圖／片段
（回傳型別只有 DriveFile{name,path,url,mime}，不存在讀取內容的 code path）。fullText 僅作
伺服器端過濾提升召回（DR-3）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .google_http import GOOGLE_NUM_RETRIES

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_MAX_DEPTH = 12  # 祖先鏈保護上限
_Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class DriveFile:
    """搜尋結果——**只有 metadata**，結構上無法承載檔案內容（DR-4）。"""

    name: str
    path: str
    url: str
    mime: str | None = None


@dataclass(slots=True)
class SearchResult:
    files: list[DriveFile]
    total: int
    offset: int
    has_more: bool
    keywords: list[str] = field(default_factory=list)


class DriveGateway(Protocol):
    """Google Drive I/O 抽象（可注入假物件）。"""

    async def search_files(self, query: str) -> list[dict[str, Any]]: ...
    async def get_folder(self, folder_id: str) -> dict[str, Any] | None: ...


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_search_query(keywords: list[str]) -> str:
    parts = ["trashed = false", f"mimeType != '{FOLDER_MIME}'"]
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        e = _escape(kw)
        parts.append(f"(name contains '{e}' or fullText contains '{e}')")
    return " and ".join(parts)


class DriveSearchService:
    def __init__(
        self,
        gateway: DriveGateway,
        scope: dict[str, str],  # 範圍名稱 -> 資料夾 id
        ttl_seconds: int,
        clock: _Clock = time.monotonic,
    ) -> None:
        self._gateway = gateway
        self._scope = dict(scope)  # name -> id
        self._root_name = {fid: name for name, fid in scope.items()}  # id -> name
        self._ttl = ttl_seconds
        self._clock = clock
        self._folder_cache: dict[str, tuple[str, str | None]] = {}  # id -> (name, parent_id)
        self._cache_at: float | None = None

    def _maybe_expire_cache(self) -> None:
        if self._cache_at is None or (self._clock() - self._cache_at) >= self._ttl:
            self._folder_cache = {}
            self._cache_at = self._clock()

    def reload(self) -> None:
        self._folder_cache = {}
        self._cache_at = None

    async def _folder(self, folder_id: str) -> tuple[str, str | None] | None:
        cached = self._folder_cache.get(folder_id)
        if cached is not None:
            return cached
        data = await self._gateway.get_folder(folder_id)
        if data is None:
            return None
        parents = data.get("parents") or []
        entry = (data.get("name", ""), parents[0] if parents else None)
        self._folder_cache[folder_id] = entry
        return entry

    async def _scope_path(self, parent_id: str, selected_root_ids: set[str]) -> str | None:
        """沿 parents 走到某個選定範圍根；在範圍內回傳自範圍根起算的資料夾路徑，否則 None。"""
        chain: list[str] = []
        cur: str | None = parent_id
        seen: set[str] = set()
        for _ in range(_MAX_DEPTH):
            if cur is None or cur in seen:
                return None
            seen.add(cur)
            if cur in selected_root_ids:
                chain.append(self._root_name[cur])
                return "/".join(reversed(chain))
            entry = await self._folder(cur)
            if entry is None:
                return None
            name, parent = entry
            chain.append(name)
            cur = parent
        return None

    async def search(
        self,
        keywords: list[str],
        *,
        scope_names: list[str] | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> SearchResult:
        self._maybe_expire_cache()
        selected = [n for n in (scope_names or list(self._scope)) if n in self._scope] or list(self._scope)
        selected_root_ids = {self._scope[n] for n in selected}

        raw = await self._gateway.search_files(build_search_query(keywords))
        matches: list[DriveFile] = []
        for f in raw:
            parents = f.get("parents") or []
            parent = parents[0] if parents else None
            if parent is None:
                continue
            folder_path = await self._scope_path(parent, selected_root_ids)
            if folder_path is None:
                continue  # 範圍外，DR-1
            matches.append(
                DriveFile(
                    name=f.get("name", ""),
                    path=f"{folder_path}/{f.get('name', '')}",
                    url=f.get("webViewLink", ""),
                    mime=f.get("mimeType"),
                )
            )

        total = len(matches)
        page = matches[offset : offset + limit]
        return SearchResult(
            files=page, total=total, offset=offset, has_more=(offset + limit) < total, keywords=keywords
        )


# --------------------------------------------------------------------------- #
# Google Drive I/O（唯讀，DR-8/DR-9）
# --------------------------------------------------------------------------- #
class GoogleDriveGateway:
    """corpora=allDrives 查詢：service account 只要被分享範圍資料夾即可，不必是共用硬碟成員。"""

    def __init__(self, sa_json_path: str) -> None:
        self._sa_json_path = sa_json_path
        self._service: Any = None

    def _build_service(self) -> Any:
        from .google_http import build_google_service

        return build_google_service("drive", "v3", self._sa_json_path, [DRIVE_SCOPE])

    def _service_or_build(self) -> Any:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _search_sync(self, query: str) -> list[dict[str, Any]]:
        service = self._service_or_build()
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            resp = (
                service.files()
                .list(
                    q=query,
                    corpora="allDrives",
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    fields="nextPageToken, files(id,name,parents,mimeType,webViewLink)",
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute(num_retries=GOOGLE_NUM_RETRIES)
            )
            items.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token or len(items) >= 1000:
                break
        return items

    def _get_folder_sync(self, folder_id: str) -> dict[str, Any] | None:
        from googleapiclient.errors import HttpError

        service = self._service_or_build()
        try:
            return (
                service.files()
                .get(fileId=folder_id, supportsAllDrives=True, fields="id,name,parents")
                .execute(num_retries=GOOGLE_NUM_RETRIES)
            )
        except HttpError as exc:
            if getattr(exc, "status_code", None) in (403, 404):
                return None  # 無權限/不存在 → 視為不可解析（範圍外）
            raise

    async def search_files(self, query: str) -> list[dict[str, Any]]:
        import asyncio

        return await asyncio.to_thread(self._search_sync, query)

    async def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        import asyncio

        return await asyncio.to_thread(self._get_folder_sync, folder_id)


def build_drive_service(sa_json_path: str, scope: dict[str, str], ttl_seconds: int) -> DriveSearchService:
    return DriveSearchService(GoogleDriveGateway(sa_json_path), scope, ttl_seconds)
