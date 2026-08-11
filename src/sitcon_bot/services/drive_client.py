"""Google Drive 唯讀搜尋與導覽（DR-1～DR-12）。

範圍限縮於指定的資料夾 ID（SITCON 2025／2026／2027，設於 .env 的 name=id 對應）。
service account 只需被分享這些資料夾即可（不必是共用雲端硬碟成員）——查詢一律用
corpora=allDrives + includeItemsFromAllDrives + supportsAllDrives。

範圍判定採「隨查隨解」：對每個命中檔案，沿其 parents 鏈往上走到某個範圍根資料夾才算在範圍內，
並就地組出路徑；folder metadata 以快取避免重複查詢。API 呼叫量因此只隨命中筆數增長，不需先枚舉
整棵資料夾樹（避免大量資料夾時逾時）。

【2026-08-11 修訂（實掃三年資料夾後的最佳化）】
- 捷徑（shortcut）視同納入：範圍內「指向資料夾的捷徑」其目標子樹視同範圍內（有效路徑＝捷徑
  所在路徑；例：2026 議程組整包在另一個共用硬碟，僅靠捷徑連進年度資料夾）。實作：每個快取
  週期列一次範圍內的資料夾捷徑，把目標資料夾 id 註冊為「虛擬根」（_alias_path），祖先鏈解析
  時一併比對。（私）標記沿捷徑路徑繼承，寧枉勿縱。
- 搜尋放寬（DR-3 修訂）：多關鍵字「全部符合」0 筆時自動改「任一符合」重查一次，結果標示
  widened；排序改「檔名命中關鍵字數 → 修改時間新→舊」。
- 新增資料夾瀏覽 list_folder（DR-12）：搜尋不到時可逐層瀏覽，僅回 metadata（同 DR-4/DR-6）。
- 讀取搬到 drive_content.DriveContentService（各檔型專屬 API 完整擷取；DR-10 修訂）；本模組
  提供 resolve_for_read（範圍檢查＋捷徑目標解析＋（私）判定）與 fetch_text（export／下載後援）。

【DR-4，2026-08-03 修訂】搜尋結果（search）只回 metadata：檔名、路徑、Drive URL、檔案類型、檔案 ID。
**只有路徑任一層含「（私）」（全形或半形）的檔案**，內容僅供 LLM 判斷相關性、不得寫給使用者
——其餘檔案內容可正常引用。此區分由程式層在讀取結果標示（依 is_private_path 判定），
「不得外流」本身由 system prompt 規範（見 agent/prompts.py 文件搜尋規則），程式層僅保證：
  1. 讀取一律先做範圍檢查，範圍外檔案讀不到（DR-1）；
  2. 全程唯讀，不寫入任何東西（DR-8/DR-9）；
  3. （私）判定寧枉勿縱：整條路徑（含檔名）任何位置出現標記即視為私。
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
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_MAX_DEPTH = 12  # 祖先鏈保護上限
_Clock = Callable[[], float]

CONTENT_LIMIT = 12000  # 讀取工具單次回傳字數上限；過長以 offset 分段續讀

# Google 原生檔 → 匯出成文字的 MIME（drive_content 各檔型 API 的後援路徑）
_EXPORT_AS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
# 可直接下載當文字讀的非 Google 原生檔
_TEXT_MIMES = {"application/json", "application/xml", "application/x-yaml", "application/yaml"}


class DriveReadError(Exception):
    """讀取／瀏覽無法完成（範圍外、不存在、型別不支援）。訊息可直接回給 LLM。"""


# （私）標記：資料夾（或檔名）含此字樣者，內容僅供 LLM 判斷、不得寫給使用者（DR-4 修訂）
_PRIVATE_MARKERS = ("（私）", "(私)")


def is_private_path(path: str) -> bool:
    """路徑任一位置含（私）標記（全形或半形括號）即視為私——寧枉勿縱。"""
    return any(marker in path for marker in _PRIVATE_MARKERS)


@dataclass(frozen=True, slots=True)
class DriveFile:
    """搜尋／瀏覽結果——只有 metadata（DR-4）；file_id 供後續讀取內容用。

    target_mime：捷徑目標的型別（僅捷徑有值）；modified：最後修改時間（RFC3339，排序用）。
    """

    name: str
    path: str
    url: str
    mime: str | None = None
    file_id: str = ""
    modified: str = ""
    target_mime: str | None = None


@dataclass(frozen=True, slots=True)
class DriveContent:
    """讀取工具的結果：檔案 metadata ＋ 取出的純文字內容。

    private：路徑含（私）標記——內容僅供 LLM 判斷相關性，不得寫給使用者（DR-4 修訂）。
    kind：型別說明（如「Google 試算表（17 張工作表）」）；offset/total_len 供分段續讀。
    """

    file: DriveFile
    text: str
    truncated: bool = False
    private: bool = False
    kind: str = ""
    offset: int = 0
    total_len: int = 0


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
    widened: bool = False  # 全部符合 0 筆 → 已放寬為任一符合


@dataclass(slots=True)
class FolderListing:
    """list_folder 的結果：資料夾路徑＋子項 metadata（資料夾在前）。"""

    path: str
    folder_id: str
    folders: list[DriveFile]
    files: list[DriveFile]


class DriveGateway(Protocol):
    """Google Drive I/O 抽象（可注入假物件）。"""

    async def search_files(self, query: str) -> list[dict[str, Any]]: ...
    async def get_folder(self, folder_id: str) -> dict[str, Any] | None: ...
    async def get_file(self, file_id: str) -> dict[str, Any] | None: ...
    async def list_children(self, folder_id: str) -> list[dict[str, Any]]: ...
    async def fetch_text(self, file_id: str, export_mime: str | None) -> str: ...


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_search_query(keywords: list[str], *, require_all: bool = True) -> str:
    """組 files.list 查詢。require_all=False 時關鍵字之間改 or（放寬重查用）。"""
    kw_parts = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        e = _escape(kw)
        kw_parts.append(f"(name contains '{e}' or fullText contains '{e}')")
    base = ["trashed = false", f"mimeType != '{FOLDER_MIME}'"]
    if not kw_parts:
        return " and ".join(base)
    if require_all:
        return " and ".join(base + kw_parts)
    return " and ".join([*base, "(" + " or ".join(kw_parts) + ")"])


_SHORTCUT_QUERY = f"trashed = false and mimeType = '{SHORTCUT_MIME}'"


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
        # 捷徑虛擬根：目標資料夾 id -> 有效路徑（捷徑所在路徑；2026-08-11 修訂）
        self._alias_path: dict[str, str] = {}
        self._alias_loaded = False
        self._alias_lock = asyncio.Lock()

    def _maybe_expire_cache(self) -> None:
        if self._cache_at is None or (self._clock() - self._cache_at) >= self._ttl:
            self._folder_cache = {}
            self._alias_path = {}
            self._alias_loaded = False
            self._cache_at = self._clock()

    def reload(self) -> None:
        self._folder_cache = {}
        self._alias_path = {}
        self._alias_loaded = False
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

    def _selected_root_names(self, selected_root_ids: set[str]) -> set[str]:
        return {self._root_name[r] for r in selected_root_ids if r in self._root_name}

    async def _scope_path(self, parent_id: str, selected_root_ids: set[str]) -> str | None:
        """沿 parents 走到某個選定範圍根；在範圍內回傳自範圍根起算的資料夾路徑，否則 None。

        捷徑虛擬根（_alias_path）視同範圍根：祖先命中時以其有效路徑取代，
        但年度須落在選定範圍內（alias 路徑第一段＝年度名）。
        """
        chain: list[str] = []
        cur: str | None = parent_id
        seen: set[str] = set()
        root_names = self._selected_root_names(selected_root_ids)
        for _ in range(_MAX_DEPTH):
            if cur is None or cur in seen:
                return None
            seen.add(cur)
            if cur in selected_root_ids:
                chain.append(self._root_name[cur])
                return "/".join(reversed(chain))
            alias = self._alias_path.get(cur)
            if alias is not None:
                if alias.split("/", 1)[0] not in root_names:
                    return None  # 捷徑掛在未選定的年度 → 視同範圍外（DR-2）
                chain.append(alias)
                return "/".join(reversed(chain))
            entry = await self._folder(cur)
            if entry is None:
                return None
            name, parent = entry
            chain.append(name)
            cur = parent
        return None

    async def _ensure_aliases(self) -> None:
        """每個快取週期列一次範圍內的「資料夾捷徑」，把目標登記為虛擬根。

        失敗不擋主流程（純召回增強）；巢狀捷徑（捷徑目標下又有捷徑）以兩輪解析涵蓋。
        """
        if self._alias_loaded:
            return
        async with self._alias_lock:
            if self._alias_loaded:
                return
            self._alias_loaded = True  # 先立旗：失敗也等下個 TTL 再試，避免每次查詢重打
            try:
                raw = await self._gateway.search_files(_SHORTCUT_QUERY)
            except Exception:
                log.warning("捷徑清單載入失敗（略過虛擬根）", exc_info=True)
                return
            candidates = []
            for f in raw:
                details = f.get("shortcutDetails") or {}
                target_id = details.get("targetId")
                if details.get("targetMimeType") == FOLDER_MIME and target_id:
                    candidates.append((f, target_id))
            all_roots = set(self._scope.values())
            for _round in range(2):
                progressed = False
                for f, target_id in candidates:
                    if target_id in self._alias_path or target_id in self._root_name:
                        continue
                    parent = (f.get("parents") or [None])[0]
                    if not parent:
                        continue
                    path = await self._scope_path(parent, all_roots)
                    if path is not None:
                        self._alias_path[target_id] = f"{path}/{f.get('name', '')}"
                        progressed = True
                if not progressed:
                    break
            if self._alias_path:
                log.info("Drive 捷徑虛擬根：%d 個資料夾捷徑已納入範圍", len(self._alias_path))

    def register_alias(self, target_folder_id: str, effective_path: str) -> None:
        """把資料夾捷徑的目標登記為虛擬根（瀏覽／讀取途中遇到時就地補登）。"""
        if target_folder_id not in self._root_name:
            self._alias_path.setdefault(target_folder_id, effective_path)

    # ------------------------------------------------------------------ #
    # 搜尋（DR-1～DR-7）
    # ------------------------------------------------------------------ #
    async def search(
        self,
        keywords: list[str],
        *,
        scope_names: list[str] | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> SearchResult:
        self._maybe_expire_cache()
        await self._ensure_aliases()
        selected = [n for n in (scope_names or list(self._scope)) if n in self._scope] or list(self._scope)
        selected_root_ids = {self._scope[n] for n in selected}
        clean_kws = [k.strip() for k in keywords if k.strip()]

        matches = await self._search_in_scope(build_search_query(clean_kws), selected_root_ids)
        widened = False
        if not matches and len(clean_kws) > 1:
            # 全部符合 0 筆 → 放寬為任一符合（DR-3 修訂），排序會把命中多的排前面
            matches = await self._search_in_scope(
                build_search_query(clean_kws, require_all=False), selected_root_ids
            )
            widened = bool(matches)

        matches = self._rank(matches, clean_kws)
        total = len(matches)
        page = matches[offset : offset + limit]
        return SearchResult(
            files=page,
            total=total,
            offset=offset,
            has_more=(offset + limit) < total,
            keywords=keywords,
            widened=widened,
        )

    async def _search_in_scope(self, query: str, selected_root_ids: set[str]) -> list[DriveFile]:
        raw = await self._gateway.search_files(query)
        # 每個命中檔案都要沿 parents 往上走才知道在不在範圍內，逐檔序列做會累積成幾十次
        # 往返（20 筆命中 × 最深 12 層）。改為併發解析：搭配 _folder 的 single-flight，
        # 實際 API 次數不變（共用祖先只查一次），但延遲從「相加」變成「取最深的那條」。
        candidates = [(f, (f.get("parents") or [None])[0]) for f in raw]
        candidates = [(f, parent) for f, parent in candidates if parent]
        paths = await asyncio.gather(
            *(self._scope_path(parent, selected_root_ids) for _f, parent in candidates)
        )
        return [
            self._to_file(f, path)
            for (f, _parent), path in zip(candidates, paths, strict=True)
            if path is not None  # None = 範圍外，DR-1
        ]

    @staticmethod
    def _rank(files: list[DriveFile], keywords: list[str]) -> list[DriveFile]:
        """檔名命中關鍵字數多者在前；同分依修改時間新→舊（RFC3339 字串可直接比大小）。"""

        def name_hits(f: DriveFile) -> int:
            lower = f.name.lower()
            return sum(1 for kw in keywords if kw.lower() in lower)

        files = sorted(files, key=lambda f: f.modified, reverse=True)
        files.sort(key=name_hits, reverse=True)
        return files

    @staticmethod
    def _to_file(raw: dict[str, Any], folder_path: str) -> DriveFile:
        name = raw.get("name", "")
        details = raw.get("shortcutDetails") or {}
        return DriveFile(
            name=name,
            path=f"{folder_path}/{name}",
            url=raw.get("webViewLink", ""),
            mime=raw.get("mimeType"),
            file_id=raw.get("id", ""),
            modified=raw.get("modifiedTime", ""),
            target_mime=details.get("targetMimeType"),
        )

    # ------------------------------------------------------------------ #
    # 資料夾瀏覽（DR-12，2026-08-11 新增）
    # ------------------------------------------------------------------ #
    async def list_folder(self, target: str = "") -> FolderListing:
        """列出資料夾內容（僅 metadata）。target 可為年度名稱、資料夾 ID 或捷徑 ID；
        留空時列出各年度根資料夾（不打 API）。"""
        self._maybe_expire_cache()
        await self._ensure_aliases()
        t = (target or "").strip()
        if not t:
            roots = [
                DriveFile(name=name, path=name, url="", mime=FOLDER_MIME, file_id=fid)
                for name, fid in self._scope.items()
            ]
            return FolderListing(path="（範圍根）", folder_id="", folders=roots, files=[])

        folder_id, path = await self._resolve_folder(t)
        children = await self._gateway.list_children(folder_id)
        folders: list[DriveFile] = []
        files: list[DriveFile] = []
        for child in children:
            entry = self._to_file(child, path)
            if child.get("mimeType") == FOLDER_MIME:
                folders.append(entry)
            else:
                files.append(entry)
                # 資料夾捷徑就地補登虛擬根，讓後續搜尋／讀取涵蓋其子樹
                if entry.target_mime == FOLDER_MIME:
                    details = child.get("shortcutDetails") or {}
                    if details.get("targetId"):
                        self.register_alias(details["targetId"], entry.path)
        return FolderListing(path=path, folder_id=folder_id, folders=folders, files=files)

    async def _resolve_folder(self, t: str) -> tuple[str, str]:
        """把年度名稱／資料夾 ID／捷徑 ID 解析成（可列出的資料夾 id, 有效路徑）。"""
        if t in self._scope:
            return self._scope[t], t
        if t in self._root_name:
            return t, self._root_name[t]
        if t in self._alias_path:
            return t, self._alias_path[t]
        data = await self._gateway.get_file(t)
        if data is None:
            raise DriveReadError("找不到這個資料夾，或 bot 沒有讀取權限。")
        mime = data.get("mimeType")
        all_roots = set(self._scope.values())
        if mime == SHORTCUT_MIME:
            details = data.get("shortcutDetails") or {}
            if details.get("targetMimeType") != FOLDER_MIME or not details.get("targetId"):
                raise DriveReadError("這個捷徑指向的不是資料夾；要讀檔案內容請用 drive_read_file。")
            own_path = await self._entry_path(data, all_roots)
            if own_path is None:
                raise DriveReadError("這個捷徑不在可搜尋的範圍資料夾內。")
            self.register_alias(details["targetId"], own_path)
            return details["targetId"], own_path
        if mime == FOLDER_MIME:
            path = await self._entry_path(data, all_roots)
            if path is None:
                raise DriveReadError("這個資料夾不在可搜尋的範圍資料夾內。")
            return t, path
        raise DriveReadError("這個 ID 是檔案不是資料夾；請用 drive_read_file 讀它的內容。")

    async def _entry_path(self, data: dict[str, Any], selected_root_ids: set[str]) -> str | None:
        """檔案／資料夾／捷徑「自身」的有效路徑（含自身名稱）。"""
        parents = data.get("parents") or []
        if not parents:
            return None
        parent_path = await self._scope_path(parents[0], selected_root_ids)
        if parent_path is None:
            return None
        return f"{parent_path}/{data.get('name', '')}"

    # ------------------------------------------------------------------ #
    # 讀取目標解析（DR-10 修訂：內容擷取移至 drive_content，本處管範圍／捷徑／（私））
    # ------------------------------------------------------------------ #
    async def resolve_for_read(self, file_id: str) -> tuple[DriveFile, bool]:
        """範圍檢查＋捷徑解析：回傳（有效 metadata（捷徑已換成目標）, 是否（私））。

        範圍檢查與搜尋同一套：沿 parents 走到某個範圍根（含捷徑虛擬根）才算數（DR-1）。
        """
        self._maybe_expire_cache()
        await self._ensure_aliases()
        data = await self._gateway.get_file(file_id)
        if data is None:
            raise DriveReadError("找不到這個檔案，或 bot 沒有讀取權限。")
        all_roots = set(self._scope.values())

        if data.get("mimeType") == SHORTCUT_MIME:
            details = data.get("shortcutDetails") or {}
            target_id = details.get("targetId")
            if not target_id:
                raise DriveReadError("這個捷徑缺少目標資訊，讀不到內容。")
            own_path = await self._entry_path(data, all_roots)
            if own_path is None:
                raise DriveReadError("這個捷徑不在可搜尋的範圍資料夾內，不能讀取。")
            if details.get("targetMimeType") == FOLDER_MIME:
                self.register_alias(target_id, own_path)
                raise DriveReadError("這個捷徑指向資料夾；要看裡面有什麼請用 drive_list_folder。")
            target = await self._gateway.get_file(target_id)
            if target is None:
                raise DriveReadError("捷徑的目標檔案讀不到（可能沒分享給 bot），只能提供捷徑本身的連結。")
            # 目標若能自行解析路徑就用目標路徑；否則沿用捷徑所在路徑（捷徑在範圍內＝視同納入）
            target_path = await self._entry_path(target, all_roots)
            effective_path = target_path or own_path
            meta = DriveFile(
                name=target.get("name", data.get("name", "")),
                path=effective_path,
                url=target.get("webViewLink", data.get("webViewLink", "")),
                mime=target.get("mimeType"),
                file_id=target_id,
                modified=target.get("modifiedTime", ""),
            )
            private = is_private_path(own_path) or is_private_path(effective_path)
            return meta, private

        path = await self._entry_path(data, all_roots)
        if path is None:
            raise DriveReadError("這個檔案不在可搜尋的範圍資料夾內，不能讀取。")
        meta = DriveFile(
            name=data.get("name", ""),
            path=path,
            url=data.get("webViewLink", ""),
            mime=data.get("mimeType"),
            file_id=data.get("id", ""),
            modified=data.get("modifiedTime", ""),
        )
        return meta, is_private_path(path)

    async def fetch_text(self, file_id: str, export_mime: str | None) -> str:
        """文字取得後援（Google 檔 export／純文字下載）；由 drive_content 依型別呼叫。"""
        return await self._gateway.fetch_text(file_id, export_mime)


# --------------------------------------------------------------------------- #
# Google Drive I/O（唯讀，DR-8/DR-9）
# --------------------------------------------------------------------------- #
_FILE_FIELDS = "id,name,parents,mimeType,webViewLink,modifiedTime,shortcutDetails"


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

    def _list_sync(self, query: str, *, order_by: str | None = None, cap: int = 1000) -> list[dict[str, Any]]:
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
                    fields=f"nextPageToken, files({_FILE_FIELDS})",
                    pageSize=100,
                    pageToken=page_token,
                    **({"orderBy": order_by} if order_by else {}),
                )
                .execute(http=request_http(self._creds), num_retries=GOOGLE_NUM_RETRIES)
            )
            items.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token or len(items) >= cap:
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
                .get(fileId=file_id, supportsAllDrives=True, fields=_FILE_FIELDS)
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
        return await asyncio.to_thread(self._list_sync, query)

    async def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_folder_sync, folder_id)

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_file_sync, file_id)

    async def list_children(self, folder_id: str) -> list[dict[str, Any]]:
        query = f"'{_escape(folder_id)}' in parents and trashed = false"
        return await asyncio.to_thread(
            lambda: self._list_sync(query, order_by="folder,name", cap=500)
        )

    async def fetch_text(self, file_id: str, export_mime: str | None) -> str:
        return await asyncio.to_thread(self._fetch_text_sync, file_id, export_mime)


def build_drive_service(sa_json_path: str, scope: dict[str, str], ttl_seconds: int) -> DriveSearchService:
    return DriveSearchService(GoogleDriveGateway(sa_json_path), scope, ttl_seconds)
