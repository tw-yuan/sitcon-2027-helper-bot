"""HackMD client（HM-1～HM-16）。作用範圍限單一 team workspace。

以 httpx 直接呼叫 REST（api.hackmd.io/v1）。硬性：不實作任何刪除（HM-16，client 層無 delete 方法）。
folders／notes 以 TTL 快取（HM-8/HM-10）；429/5xx 退避重試（EC-9）、401/403 憑證錯誤（EC-10）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.hackmd.io/v1"
_Clock = Callable[[], float]
_Sleep = Callable[[float], Awaitable[None]]


class HackMDError(RuntimeError):
    """HackMD 操作錯誤基底。"""


class HackMDCredentialError(HackMDError):
    """憑證失效（401/403，EC-10）。"""


class HackMDAPIError(HackMDError):
    def __init__(self, message: str, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


@dataclass(slots=True)
class Folder:
    id: str
    name: str
    parent_folder_id: str | None = None


@dataclass(slots=True)
class NoteMeta:
    id: str
    title: str
    tags: list[str] = field(default_factory=list)
    url: str = ""
    folder: str | None = None  # 最上層資料夾名（年度，如「SITCON 2027」）；root 筆記為 None


def _top_folder(note: dict[str, Any]) -> str | None:
    """自 folderPaths 取最上層資料夾名（HackMD 用 folderPaths 表示歸檔，非 parentFolderId）。"""
    fp = note.get("folderPaths")
    if isinstance(fp, list) and fp and isinstance(fp[0], dict):
        return fp[0].get("name")
    return None


@dataclass(slots=True)
class Note:
    id: str
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    url: str = ""


def _note_url(data: dict[str, Any]) -> str:
    if data.get("publishLink"):
        return str(data["publishLink"])
    if data.get("shortId"):
        return f"https://hackmd.io/{data['shortId']}"
    if data.get("permalink"):
        return f"https://hackmd.io/{data['permalink']}"
    return ""


class HackMDClient:
    def __init__(
        self,
        token: str,
        team_path: str,
        default_read_perm: str = "signed_in",
        default_write_perm: str = "signed_in",
        cache_ttl: int = 600,
        http: httpx.AsyncClient | None = None,
        clock: _Clock = time.monotonic,
        sleep: _Sleep = asyncio.sleep,
        max_retries: int = 2,
    ) -> None:
        self._team = team_path
        self.default_read_perm = default_read_perm
        self.default_write_perm = default_write_perm
        self._ttl = cache_ttl
        self._clock = clock
        self._sleep = sleep
        self._max_retries = max_retries
        self._http = http or httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        self._folders: list[Folder] | None = None
        self._folders_at: float | None = None
        self._notes: list[NoteMeta] | None = None
        self._notes_at: float | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    def reload(self) -> None:
        self._folders = self._folders_at = None
        self._notes = self._notes_at = None

    def _fresh(self, at: float | None) -> bool:
        return at is not None and (self._clock() - at) < self._ttl

    # ----------------------------- HTTP ----------------------------- #
    async def _request(self, method: str, url: str, json: dict[str, Any] | None = None) -> Any:
        attempt = 0
        while True:
            try:
                resp = await self._http.request(method, url, json=json)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    await self._sleep(0.5 * (2**attempt))
                    attempt += 1
                    continue
                raise HackMDAPIError(f"HackMD 連線失敗：{exc}") from exc

            status = resp.status_code
            if status in (401, 403):
                raise HackMDCredentialError("HackMD 憑證失效，請通知管理員")
            if status == 429 or 500 <= status < 600:
                if attempt < self._max_retries:
                    await self._sleep(0.5 * (2**attempt))
                    attempt += 1
                    continue
                raise HackMDAPIError(f"HackMD 回應錯誤（HTTP {status}）", status)
            if status >= 400:
                raise HackMDAPIError(f"HackMD 回應錯誤（HTTP {status}）：{resp.text[:200]}", status)
            if resp.content:
                return resp.json()
            return None

    # ---------------------------- folders --------------------------- #
    async def list_folders(self) -> list[Folder]:
        if self._folders is None or not self._fresh(self._folders_at):
            data = await self._request("GET", f"/teams/{self._team}/folders")
            self._folders = [
                Folder(id=f["id"], name=f.get("name", ""), parent_folder_id=f.get("parentFolderId"))
                for f in (data or [])
            ]
            self._folders_at = self._clock()
        assert self._folders is not None
        return self._folders

    async def find_folder(self, name: str, parent_id: str | None = None) -> Folder | None:
        """在指定 parent 底下找同名資料夾；parent_id=None 表示頂層（用於定位年度根資料夾）。

        同一 team 內多個年度可能有同名組別資料夾（如各年皆有「開發組」），故一律以 parent 限縮。
        """
        for f in await self.list_folders():
            if f.name == name and f.parent_folder_id == parent_id:
                return f
        return None

    async def create_folder(self, name: str, parent_id: str | None = None) -> Folder:
        payload: dict[str, Any] = {"name": name}
        if parent_id:
            payload["parentFolderId"] = parent_id
        data = await self._request("POST", f"/teams/{self._team}/folders", payload)
        self._folders = self._folders_at = None  # 失效快取
        return Folder(id=data["id"], name=data.get("name", name), parent_folder_id=data.get("parentFolderId"))

    async def ensure_meeting_subfolder(self, team_folder_id: str, subfolder_name: str) -> Folder:
        """在組別資料夾下取得（或建立）指定子資料夾（HM-9，僅限此固定子層）。"""
        existing = await self.find_folder(subfolder_name, parent_id=team_folder_id)
        if existing is not None:
            return existing
        return await self.create_folder(subfolder_name, parent_id=team_folder_id)

    # ----------------------------- notes ---------------------------- #
    async def list_notes(self) -> list[NoteMeta]:
        if self._notes is None or not self._fresh(self._notes_at):
            data = await self._request("GET", f"/teams/{self._team}/notes")
            self._notes = [
                NoteMeta(
                    id=n["id"],
                    title=n.get("title", ""),
                    tags=list(n.get("tags") or []),
                    url=_note_url(n),
                    folder=_top_folder(n),
                )
                for n in (data or [])
            ]
            self._notes_at = self._clock()
        assert self._notes is not None
        return self._notes

    async def get_note(self, note_id: str) -> Note:
        data = await self._request("GET", f"/teams/{self._team}/notes/{note_id}")
        return Note(
            id=data["id"],
            title=data.get("title", ""),
            content=data.get("content", ""),
            tags=list(data.get("tags") or []),
            url=_note_url(data),
        )

    async def create_note(
        self,
        *,
        title: str,
        content: str,
        tags: list[str],
        parent_folder_id: str | None = None,
        read_perm: str | None = None,
        write_perm: str | None = None,
    ) -> Note:
        payload: dict[str, Any] = {
            "title": title,
            "content": content,
            "tags": tags,
            "readPermission": read_perm or self.default_read_perm,
            "writePermission": write_perm or self.default_write_perm,
        }
        if parent_folder_id:
            payload["parentFolderId"] = parent_folder_id
        data = await self._request("POST", f"/teams/{self._team}/notes", payload)
        self._notes = self._notes_at = None
        return Note(
            id=data["id"],
            title=data.get("title", title),
            content=data.get("content", content),
            tags=list(data.get("tags") or tags),
            url=_note_url(data),
        )

    async def update_note(
        self, note_id: str, *, content: str | None = None, tags: list[str] | None = None
    ) -> None:
        """更新既有 team note（HM-13）。整份內容寫回；不刪除（HM-16）。"""
        payload: dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if tags is not None:
            payload["tags"] = tags
        if not payload:
            return
        await self._request("PATCH", f"/teams/{self._team}/notes/{note_id}", payload)
        self._notes = self._notes_at = None


def build_hackmd_client(settings: Any) -> HackMDClient:
    return HackMDClient(
        token=settings.hackmd_token.get_secret_value(),
        team_path=settings.hackmd_team_path,
        default_read_perm=settings.hackmd_default_read_perm,
        default_write_perm=settings.hackmd_default_write_perm,
        cache_ttl=settings.cache_ttl_hackmd,
    )
