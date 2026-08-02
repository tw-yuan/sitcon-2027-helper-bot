"""Google Drive 唯讀搜尋（DR-1～DR-9）。

範圍限縮於指定的資料夾 ID（SITCON 2025／2026／2027，設於 .env 的 name=id 對應）。
service account 只需被分享這兩個資料夾即可（不必是共用雲端硬碟成員）——查詢一律用
corpora=allDrives + includeItemsFromAllDrives + supportsAllDrives。

範圍判定採「隨查隨解」：對每個命中檔案，沿其 parents 鏈往上走到某個範圍根資料夾才算在範圍內，
並就地組出路徑；folder metadata 以快取避免重複查詢。API 呼叫量因此只隨命中筆數增長，不需先枚舉
整棵資料夾樹（避免大量資料夾時逾時）。

【DR-4，2026-08-03 修訂】搜尋結果（search）只回 metadata：檔名、路徑、Drive URL、檔案類型、檔案 ID。
檔案內容另有唯讀的 read_file；**只有路徑任一層含「（私）」（全形或半形）的檔案**，內容僅供 LLM
判斷相關性、不得寫給使用者——其餘檔案內容可正常引用。此區分由程式層在讀取結果標示
（DriveContent.private，依 is_private_path 判定），「不得外流」本身由 system prompt 規範
（見 agent/prompts.py 文件搜尋規則），程式層僅保證：
  1. 讀取一律先做範圍檢查，範圍外檔案讀不到（DR-1）；
  2. 只讀得出文字（Google 文件/試算表/簡報 export、純文字檔 download），二進位檔一律拒絕；
  3. 全程唯讀，仍不寫入任何東西（DR-8/DR-9）；
  4. （私）判定寧枉勿縱：整條路徑（含檔名）任何位置出現標記即視為私。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..concurrency import KeyedLock
from .google_http import GOOGLE_NUM_RETRIES, build_google_service, request_http

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_MAX_DEPTH = 12  # 祖先鏈保護上限
_Clock = Callable[[], float]

CONTENT_LIMIT = 8000  # read_file 回傳字數上限（僅供相關性判斷，不需全文）

# Google 原生檔 → 匯出成文字的 MIME
_EXPORT_AS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
# 可直接下載當文字讀的非 Google 原生檔
_TEXT_MIMES = {"application/json", "application/xml", "application/x-yaml", "application/yaml"}


class DriveReadError(Exception):
    """read_file 無法取得內容（範圍外、不存在、型別不支援）。訊息可直接回給 LLM。"""


# （私）標記：資料夾（或檔名）含此字樣者，內容僅供 LLM 判斷、不得寫給使用者（DR-4 修訂）
_PRIVATE_MARKERS = ("（私）", "(私)")


def is_private_path(path: str) -> bool:
    """路徑任一位置含（私）標記（全形或半形括號）即視為私——寧枉勿縱。"""
    return any(marker in path for marker in _PRIVATE_MARKERS)


@dataclass(frozen=True, slots=True)
class DriveFile:
    """搜尋結果——只有 metadata（DR-4）；file_id 供後續 read_file 讀內容用。"""

    name: str
    path: str
    url: str
    mime: str | None = None
    file_id: str = ""


@dataclass(frozen=True, slots=True)
class DriveContent:
    """read_file 的結果：檔案 metadata ＋ 取出的純文字內容。

    private：路徑含（私）標記——內容僅供 LLM 判斷相關性，不得寫給使用者（DR-4 修訂）。
    """

    file: DriveFile
    text: str
    truncated: bool = False
    private: bool = False


def content_mode(mime: str | None) -> tuple[str, str | None] | None:
    """回傳 ('export', 匯出 MIME) / ('download', None)；無法取出文字則 None。"""
    if not mime:
        return None
    if mime in _EXPORT_AS:
        return "export", _EXPORT_AS[mime]
    if mime.startswith("text/") or mime in _TEXT_MIMES:
        return "download", None
    return None


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
    async def get_file(self, file_id: str) -> dict[str, Any] | None: ...
    async def fetch_text(self, file_id: str, export_mime: str | None) -> str: ...


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
        # single-flight：併發解析多個檔案的祖先鏈時，同一個資料夾只查一次
        self._folder_lock = KeyedLock()

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
        async with self._folder_lock(folder_id):
            cached = self._folder_cache.get(folder_id)  # 等鎖期間可能已被別人填好
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
        # 每個命中檔案都要沿 parents 往上走才知道在不在範圍內，逐檔序列做會累積成幾十次
        # 往返（20 筆命中 × 最深 12 層）。改為併發解析：搭配 _folder 的 single-flight，
        # 實際 API 次數不變（共用祖先只查一次），但延遲從「相加」變成「取最深的那條」。
        candidates = [(f, (f.get("parents") or [None])[0]) for f in raw]
        candidates = [(f, parent) for f, parent in candidates if parent]
        paths = await asyncio.gather(
            *(self._scope_path(parent, selected_root_ids) for _f, parent in candidates)
        )
        matches: list[DriveFile] = [
            self._to_file(f, path)
            for (f, _parent), path in zip(candidates, paths, strict=True)
            if path is not None  # None = 範圍外，DR-1
        ]

        total = len(matches)
        page = matches[offset : offset + limit]
        return SearchResult(
            files=page, total=total, offset=offset, has_more=(offset + limit) < total, keywords=keywords
        )

    @staticmethod
    def _to_file(raw: dict[str, Any], folder_path: str) -> DriveFile:
        name = raw.get("name", "")
        return DriveFile(
            name=name,
            path=f"{folder_path}/{name}",
            url=raw.get("webViewLink", ""),
            mime=raw.get("mimeType"),
            file_id=raw.get("id", ""),
        )

    async def read_file(self, file_id: str) -> DriveContent:
        """讀取範圍內檔案的純文字內容（供相關性判斷）。取不到時丟 DriveReadError。

        範圍檢查與搜尋同一套：沿 parents 走到某個範圍根才算數，範圍外一律拒讀（DR-1）。
        """
        self._maybe_expire_cache()
        data = await self._gateway.get_file(file_id)
        if data is None:
            raise DriveReadError("找不到這個檔案，或 bot 沒有讀取權限。")
        parents = data.get("parents") or []
        folder_path = await self._scope_path(parents[0], set(self._scope.values())) if parents else None
        if folder_path is None:
            raise DriveReadError("這個檔案不在可搜尋的範圍資料夾內，不能讀取。")

        meta = self._to_file(data, folder_path)
        mode = content_mode(meta.mime)
        if mode is None:
            raise DriveReadError(f"這個檔案類型（{meta.mime or '未知'}）無法取出文字內容，只能看檔名與路徑。")

        text = await self._gateway.fetch_text(file_id, mode[1])
        truncated = len(text) > CONTENT_LIMIT
        return DriveContent(
            file=meta, text=text[:CONTENT_LIMIT], truncated=truncated, private=is_private_path(meta.path)
        )


# --------------------------------------------------------------------------- #
# Google Drive I/O（唯讀，DR-8/DR-9）
# --------------------------------------------------------------------------- #
class GoogleDriveGateway:
    """corpora=allDrives 查詢：service account 只要被分享範圍資料夾即可，不必是共用硬碟成員。"""

    def __init__(self, sa_json_path: str) -> None:
        self._sa_json_path = sa_json_path
        self._service: Any = None
        self._creds: Any = None

    def _service_or_build(self) -> Any:
        if self._service is None:
            self._service, self._creds = build_google_service("drive", "v3", self._sa_json_path, [DRIVE_SCOPE])
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
                .execute(http=request_http(self._creds), num_retries=GOOGLE_NUM_RETRIES)
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
                .execute(http=request_http(self._creds), num_retries=GOOGLE_NUM_RETRIES)
            )
        except HttpError as exc:
            if getattr(exc, "status_code", None) in (403, 404):
                return None  # 無權限/不存在 → 視為不可解析（範圍外）
            raise

    def _get_file_sync(self, file_id: str) -> dict[str, Any] | None:
        from googleapiclient.errors import HttpError

        service = self._service_or_build()
        try:
            return (
                service.files()
                .get(
                    fileId=file_id,
                    supportsAllDrives=True,
                    fields="id,name,parents,mimeType,webViewLink",
                )
                .execute(http=request_http(self._creds), num_retries=GOOGLE_NUM_RETRIES)
            )
        except HttpError as exc:
            if getattr(exc, "status_code", None) in (403, 404):
                return None
            raise

    def _fetch_text_sync(self, file_id: str, export_mime: str | None) -> str:
        """Google 原生檔走 export_media 轉文字；其餘（純文字檔）走 get_media 下載。皆為唯讀。"""
        service = self._service_or_build()
        if export_mime:
            req = service.files().export_media(fileId=file_id, mimeType=export_mime)
        else:
            req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        data = req.execute(http=request_http(self._creds), num_retries=GOOGLE_NUM_RETRIES)
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data)

    async def search_files(self, query: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._search_sync, query)

    async def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_folder_sync, folder_id)

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_file_sync, file_id)

    async def fetch_text(self, file_id: str, export_mime: str | None) -> str:
        return await asyncio.to_thread(self._fetch_text_sync, file_id, export_mime)


def build_drive_service(sa_json_path: str, scope: dict[str, str], ttl_seconds: int) -> DriveSearchService:
    return DriveSearchService(GoogleDriveGateway(sa_json_path), scope, ttl_seconds)
