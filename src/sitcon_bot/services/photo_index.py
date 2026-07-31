"""SITCON Flickr 照片索引（讀 Photo Finder 試算表的 photos 分頁）。

以 service account 讀取試算表指定分頁（預設 photos，~28k 列），解析成 Photo 後**記憶體快取**
（TTL；照片極少變動，用長 TTL，/reload 可強制刷新）。搜尋為記憶體內關鍵字比對，比對範圍是
預先組好的小寫字串 blob（相簿名＋視覺描述＋各類 tag＋主體類型＋攝影師）。

只取對搜尋/呈現有用、且填充率高的欄位（event_name/year、license、sponsorship 等在來源多為空）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .google_http import GOOGLE_NUM_RETRIES, build_google_service, request_http

log = logging.getLogger(__name__)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
_SPLIT_RE = re.compile(r"[;；,、]")
# 以表頭名對應欄位（容忍欄位順序變動）
_WANTED = (
    "photo_id", "photo_url", "image_preview_url", "album_title", "subject_type", "photographer",
    "scene_tags", "mood_tags", "recommended_uses", "orientation", "visual_description", "people_count",
)

_Clock = Callable[[], float]


class PhotoIndexUnavailableError(RuntimeError):
    """照片索引載入失敗且無可用快取。"""


def _split_tags(value: str) -> list[str]:
    return [t.strip() for t in _SPLIT_RE.split(value) if t.strip()]


@dataclass(slots=True)
class Photo:
    photo_id: str
    photo_url: str
    preview_url: str
    album_title: str
    subject_type: str
    photographer: str
    scene_tags: list[str]
    mood_tags: list[str]
    recommended_uses: list[str]
    orientation: str
    visual_description: str
    people_count: int
    blob: str = ""  # 預先組好的小寫搜尋字串


@dataclass(slots=True)
class PhotoSearchResult:
    photos: list[Photo]
    total: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.photos) < self.total


def parse_photos(header: list[str], rows: list[list[str]]) -> list[Photo]:
    idx = {name: header.index(name) for name in _WANTED if name in header}

    def cell(row: list[str], name: str) -> str:
        i = idx.get(name)
        return row[i].strip() if (i is not None and i < len(row) and row[i]) else ""

    photos: list[Photo] = []
    for row in rows:
        pid = cell(row, "photo_id")
        if not pid:
            continue
        scene = _split_tags(cell(row, "scene_tags"))
        mood = _split_tags(cell(row, "mood_tags"))
        uses = _split_tags(cell(row, "recommended_uses"))
        album = cell(row, "album_title")
        subject = cell(row, "subject_type")
        photographer = cell(row, "photographer")
        desc = cell(row, "visual_description")
        try:
            people = int(cell(row, "people_count") or 0)
        except ValueError:
            people = 0
        blob = " ".join([album, desc, subject, photographer, *scene, *mood, *uses]).lower()
        photos.append(
            Photo(
                photo_id=pid,
                photo_url=cell(row, "photo_url"),
                preview_url=cell(row, "image_preview_url"),
                album_title=album,
                subject_type=subject,
                photographer=photographer,
                scene_tags=scene,
                mood_tags=mood,
                recommended_uses=uses,
                orientation=cell(row, "orientation"),
                visual_description=desc,
                people_count=people,
                blob=blob,
            )
        )
    return photos


class PhotoIndex:
    """載入後的照片查詢視圖。"""

    def __init__(self, photos: list[Photo]) -> None:
        self._photos = photos

    def __len__(self) -> int:
        return len(self._photos)

    def search(
        self,
        keywords: list[str],
        *,
        orientation: str | None = None,
        subject_type: str | None = None,
        has_people: bool | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> PhotoSearchResult:
        kws = [k.strip().lower() for k in keywords if k.strip()]

        def match(p: Photo) -> bool:
            if orientation and p.orientation != orientation:
                return False
            if subject_type and p.subject_type != subject_type:
                return False
            if has_people is True and p.people_count <= 0:
                return False
            if has_people is False and p.people_count > 0:
                return False
            return all(k in p.blob for k in kws)

        hits = [p for p in self._photos if match(p)]
        return PhotoSearchResult(photos=hits[offset : offset + limit], total=len(hits), offset=offset)


class PhotoFetcher(Protocol):
    async def fetch(self) -> tuple[list[str], list[list[str]]]: ...


class GoogleSheetPhotoFetcher:
    def __init__(self, sa_json_path: str, sheet_id: str, tab: str) -> None:
        self._sa_json_path = sa_json_path
        self._sheet_id = sheet_id
        self._tab = tab
        self._service: object | None = None
        self._creds: object | None = None

    def _fetch_sync(self) -> tuple[list[str], list[list[str]]]:
        if self._service is None:
            self._service, self._creds = build_google_service(
                "sheets", "v4", self._sa_json_path, [SHEETS_SCOPE]
            )
        resp = (
            self._service.spreadsheets()  # type: ignore[attr-defined]
            .values()
            .get(spreadsheetId=self._sheet_id, range=f"'{self._tab}'!A1:Y")
            .execute(http=request_http(self._creds), num_retries=GOOGLE_NUM_RETRIES)
        )
        values: list[list[str]] = resp.get("values", [])
        header = values[0] if values else []
        rows = values[1:] if len(values) > 1 else []
        return header, rows

    async def fetch(self) -> tuple[list[str], list[list[str]]]:
        return await asyncio.to_thread(self._fetch_sync)


class PhotoIndexService:
    """TTL 快取的照片索引；載入失敗時沿用舊快取（EC-11）。

    single-flight：快取冷掉時同時進來的多個查詢只會抓一次 Sheets。
    """

    def __init__(self, fetcher: PhotoFetcher, ttl_seconds: int, clock: _Clock = time.monotonic) -> None:
        self._fetcher = fetcher
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: PhotoIndex | None = None
        self._at: float | None = None
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._cache is not None and self._at is not None and (self._clock() - self._at) < self._ttl

    async def get(self, *, force: bool = False) -> PhotoIndex:
        if self._fresh() and not force:
            assert self._cache is not None
            return self._cache
        async with self._lock:
            # 等鎖期間可能已有人載好；force（/reload）例外，一定重抓。
            if self._fresh() and not force:
                assert self._cache is not None
                return self._cache
            return await self._fetch_locked()

    async def _fetch_locked(self) -> PhotoIndex:
        try:
            header, rows = await self._fetcher.fetch()
            self._cache = PhotoIndex(parse_photos(header, rows))
            self._at = self._clock()
            log.info("照片索引載入 %d 張", len(self._cache))
        except Exception as exc:
            if self._cache is not None:
                log.warning("照片索引更新失敗，沿用舊快取", exc_info=True)
                return self._cache
            raise PhotoIndexUnavailableError("照片索引載入失敗且無可用快取") from exc
        return self._cache

    async def reload(self) -> PhotoIndex:
        return await self.get(force=True)


def build_photo_index_service(
    sa_json_path: str, sheet_id: str, tab: str, ttl_seconds: int
) -> PhotoIndexService:
    return PhotoIndexService(GoogleSheetPhotoFetcher(sa_json_path, sheet_id, tab), ttl_seconds)
