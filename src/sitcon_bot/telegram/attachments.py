"""從 Telegram 訊息取出附件參照（不下載內容）。

支援：圖片（取最大尺寸）、檔案、貼圖（靜態 webp 直接當圖；動態／影片貼圖取縮圖）、
影片／GIF 動圖／圓形影片（縮圖供視覺）、音訊、語音。
"""

from __future__ import annotations

from typing import Any

from ..services.attachments import Attachment

_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "application/x-tgsticker": "tgs",
}


def _thumb(obj: Any) -> tuple[str | None, str | None]:
    """縮圖 (file_id, mime)。mime 只是預設值——貼圖縮圖可能是 webp，實際格式由 store 下載後嗅探。"""
    t = getattr(obj, "thumbnail", None)
    if t is None:
        return None, None
    return t.file_id, "image/jpeg"


def _name(prefix: str, message_id: int, mime: str | None, fallback_ext: str) -> str:
    return f"{prefix}_{message_id}.{_EXT_BY_MIME.get(mime or '', fallback_ext)}"


def extract_attachments(message: Any, source: str = "本則訊息") -> list[Attachment]:
    """取出一則訊息的所有附件（Telegram 一則訊息最多一個媒體，相簿是多則訊息）。"""
    mid = getattr(message, "message_id", 0)
    out: list[Attachment] = []

    photos = getattr(message, "photo", None) or ()
    if photos:
        p = photos[-1]  # 最大尺寸
        out.append(
            Attachment(
                kind="photo",
                file_id=p.file_id,
                file_name=f"photo_{mid}.jpg",
                mime="image/jpeg",
                size=p.file_size,
                source=source,
            )
        )

    doc = getattr(message, "document", None)
    if doc is not None:
        mime = doc.mime_type
        # animation 訊息同時帶 document 欄位；交給下方 animation 分支處理以免重複
        if getattr(message, "animation", None) is None:
            pid, pmime = (None, None) if (mime or "").startswith("image/") else _thumb(doc)
            out.append(
                Attachment(
                    kind="document",
                    file_id=doc.file_id,
                    file_name=doc.file_name or _name("file", mid, mime, "bin"),
                    mime=mime,
                    size=doc.file_size,
                    source=source,
                    preview_file_id=pid,
                    preview_mime=pmime,
                )
            )

    sticker = getattr(message, "sticker", None)
    if sticker is not None:
        animated = bool(sticker.is_animated or sticker.is_video)
        mime = "application/x-tgsticker" if sticker.is_animated else "video/webm" if sticker.is_video else "image/webp"
        pid, pmime = _thumb(sticker) if animated else (None, None)
        note = f"貼圖 emoji {sticker.emoji}" if sticker.emoji else ""
        if sticker.set_name:
            note = f"{note}，貼圖包 {sticker.set_name}" if note else f"貼圖包 {sticker.set_name}"
        out.append(
            Attachment(
                kind="sticker",
                file_id=sticker.file_id,
                file_name=_name("sticker", mid, mime, "webp"),
                mime=mime,
                size=sticker.file_size,
                source=source,
                note=note,
                preview_file_id=pid,
                preview_mime=pmime,
            )
        )

    for kind in ("video", "animation", "video_note"):
        media = getattr(message, kind, None)
        if media is None:
            continue
        mime = getattr(media, "mime_type", None) or "video/mp4"
        pid, pmime = _thumb(media)
        out.append(
            Attachment(
                kind=kind,
                file_id=media.file_id,
                file_name=getattr(media, "file_name", None) or _name(kind, mid, mime, "mp4"),
                mime=mime,
                size=media.file_size,
                source=source,
                preview_file_id=pid,
                preview_mime=pmime,
            )
        )

    for kind in ("audio", "voice"):
        media = getattr(message, kind, None)
        if media is None:
            continue
        mime = getattr(media, "mime_type", None) or ("audio/ogg" if kind == "voice" else "audio/mpeg")
        out.append(
            Attachment(
                kind=kind,
                file_id=media.file_id,
                file_name=getattr(media, "file_name", None) or _name(kind, mid, mime, "ogg"),
                mime=mime,
                size=media.file_size,
                source=source,
            )
        )
    return out
