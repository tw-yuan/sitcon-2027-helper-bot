"""照片搜尋工具（SITCON Flickr 照片索引）。

只回 metadata（相簿、Flickr 連結、縮圖、描述）；並把最相關的前幾張縮圖塞進 ctx.media，
由 gateway 以圖片送出（代表圖＋連結清單）。外部（半可信）內容以 <external_data> 包裹（NFR-6）。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ...services.photo_index import Photo, PhotoIndexService
from .base import MediaItem, Tool, ToolContext
from .external_data import wrap_external

log = logging.getLogger(__name__)

MAX_LISTED = 10  # 文字清單最多列幾張
MAX_THUMBS = 3  # 代表縮圖最多送幾張
_CAPTION_LIMIT = 900  # Telegram caption 上限 1024，留餘裕


def _larger(preview_url: str) -> str:
    """Flickr 縮圖 _q.jpg（150 方裁）→ _z.jpg（640）較適合預覽；非該格式則原樣。"""
    if preview_url.endswith("_q.jpg"):
        return preview_url[: -len("_q.jpg")] + "_z.jpg"
    return preview_url


def _caption(p: Photo) -> str:
    tags = "、".join(p.scene_tags[:4])
    desc = p.visual_description[:80]
    parts = [p.album_title]
    if tags:
        parts.append(tags)
    if desc:
        parts.append(desc)
    head = "｜".join(parts)
    return f"{head}\n{p.photo_url}"[:_CAPTION_LIMIT]


class PhotoSearchArgs(BaseModel):
    keywords: list[str] = Field(
        description="描述照片內容的關鍵字（如 講者、工作坊、合照、茶點、螢幕；也可含活動/相簿名如 Camp 2026）"
    )
    orientation: str | None = Field(None, description="橫式='landscape'、直式='portrait'")
    subject_type: str | None = Field(
        None, description="主體類型：people／screen／object／food／space／text_signage"
    )
    has_people: bool | None = Field(None, description="是否需要有人物入鏡")
    offset: int = Field(0, description="要跳過的筆數（要求下一批時用）")


class PhotoSearchTool(Tool):
    name = "photo_search"
    description = (
        "在 SITCON Flickr 照片索引中依內容搜尋照片（歷年活動照）。可用關鍵字（場景/情緒/主體/活動名）"
        "與橫直、主體類型、是否有人物過濾；回相簿、Flickr 連結、縮圖與描述，並附上最相關的前幾張縮圖。"
    )
    args_model = PhotoSearchArgs

    def __init__(self, service: PhotoIndexService | None) -> None:
        self._svc = service

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, PhotoSearchArgs)
        if self._svc is None:
            return "照片搜尋未設定，請通知管理員。"
        kws = "、".join(k for k in args.keywords if k.strip()) or "（無）"
        try:
            index = await self._svc.get()
        except Exception:
            log.warning("照片索引載入失敗", exc_info=True)
            return "照片索引暫時無法載入，請稍後再試或通知管理員。"

        res = index.search(
            args.keywords,
            orientation=args.orientation,
            subject_type=args.subject_type,
            has_people=args.has_people,
            offset=args.offset,
            limit=MAX_LISTED,
        )
        if res.total == 0:
            return f"找不到符合的照片（關鍵字：{kws}）。可換個關鍵字或放寬條件再試一次。"

        # 代表縮圖（前幾張）→ 交給 gateway 送出圖片
        for p in res.photos[:MAX_THUMBS]:
            if p.preview_url:
                ctx.media.append(MediaItem(url=_larger(p.preview_url), caption=_caption(p)))

        lines = [f"- {p.album_title}｜{p.visual_description[:50]}｜{p.photo_url}" for p in res.photos]
        header = f"共 {res.total} 張（關鍵字：{kws}）"
        if res.has_more:
            header += f"，顯示第 {res.offset + 1}–{res.offset + len(res.photos)} 張，可要求下一批"
        note = f"（已附上最相關的前 {min(MAX_THUMBS, len(res.photos))} 張縮圖）"
        return f"{header}：\n" + wrap_external("\n".join(lines)) + f"\n{note}"


def build_photo_tools(service: PhotoIndexService | None) -> list[Tool]:
    return [PhotoSearchTool(service)]
