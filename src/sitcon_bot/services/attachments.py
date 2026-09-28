"""使用者訊息附件（圖片、貼圖、檔案…）的暫存索引與內容讀取。

gateway 從觸發訊息（及其被回覆訊息、同一相簿）取出附件 → 登記到 AttachmentStore 拿到短代號 →
agent 在使用者訊息中列出代號（圖片另以視覺區塊附上）→ 工具（attachment_read、GitLab 附檔）
以代號取回並下載內容。

- 只存 Telegram file_id 等參照，不落地；內容在需要時才下載（小量快取，供同回合重複使用）。
- 代號綁定 chat_id：工具只能取用同一個對話裡登記的附件，避免跨群取檔。
- TTL 與 reply-chain 脈絡相同，回覆小石的回覆續接時仍可引用同一批附件。
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

# Telegram Bot API getFile 的下載上限
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024
# Anthropic 單張圖片上限 5MB（base64 前）；超過就不送視覺，但仍可上傳／附檔
VISION_MAX_BYTES = 5 * 1024 * 1024
# 模型視覺支援的圖片格式（Anthropic 與 OpenAI 相容格式的交集）
VISION_MIME = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

KIND_LABEL = {
    "photo": "圖片",
    "document": "檔案",
    "sticker": "貼圖",
    "video": "影片",
    "animation": "GIF 動圖",
    "video_note": "圓形影片",
    "audio": "音訊",
    "voice": "語音訊息",
}

Downloader = Callable[[str], Awaitable[bytes]]


class AttachmentError(RuntimeError):
    """取用附件失敗（代號失效、跨群、太大、下載失敗）；訊息可直接轉告使用者。"""


@dataclass(slots=True)
class Attachment:
    kind: str  # photo / document / sticker / video / animation / video_note / audio / voice
    file_id: str
    file_name: str
    mime: str | None = None
    size: int | None = None
    source: str = "本則訊息"  # 本則訊息／被回覆的訊息／同一則相簿
    note: str = ""  # 補充說明（如貼圖的 emoji）
    # 視覺用的預覽圖：影片／動圖／動態貼圖取縮圖；本身就是可看圖片時為 None（用原檔）
    preview_file_id: str | None = None
    preview_mime: str | None = None
    # 登記後才填
    id: str = ""
    chat_id: int = 0

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, "附件")

    def vision_source(self) -> tuple[str, str] | None:
        """可送模型視覺的 (file_id, mime)；無法視覺處理時回 None。"""
        if self.mime in VISION_MIME and (self.size is None or self.size <= VISION_MAX_BYTES):
            return self.file_id, self.mime or "image/jpeg"
        if self.preview_file_id:
            return self.preview_file_id, self.preview_mime or "image/jpeg"
        return None


def sniff_image_mime(data: bytes) -> str | None:
    """依檔頭判斷視覺支援的圖片格式；不是（或不支援）回 None。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_MIME_PDF = "application/pdf"
_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MIME_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_OFFICE_BY_EXT = {"pdf": _MIME_PDF, "docx": _MIME_DOCX, "xlsx": _MIME_XLSX, "pptx": _MIME_PPTX}
_TEXT_MIME = frozenset(
    {"application/json", "application/xml", "application/x-yaml", "application/yaml", "application/javascript"}
)
_TEXT_EXT = frozenset(
    {"txt", "md", "markdown", "csv", "tsv", "json", "yaml", "yml", "xml", "html", "htm", "log", "ini", "toml",
     "py", "js", "ts", "sh", "sql", "svg", "srt", "vtt"}
)  # fmt: skip


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _effective_mime(mime: str | None, name: str) -> str | None:
    """Telegram 常把 Office 檔標成 application/octet-stream；以副檔名補判。"""
    if mime in (None, "", "application/octet-stream", "application/zip"):
        return _OFFICE_BY_EXT.get(_ext(name), mime)
    return mime


def is_text_readable(mime: str | None, name: str) -> bool:
    m = _effective_mime(mime, name) or ""
    return (
        m in (_MIME_PDF, _MIME_DOCX, _MIME_XLSX, _MIME_PPTX)
        or m.startswith("text/")
        or m in _TEXT_MIME
        or m == "image/svg+xml"
        or _ext(name) in _TEXT_EXT
    )


def extract_text(mime: str | None, name: str, data: bytes) -> tuple[str, str]:
    """附件 bytes → (文字, 類型說明)。解析器沿用 Drive 讀取的實作（純函式、不打網路）。"""
    from .drive_content import (  # 延遲載入：pypdf 等只在讀檔時需要
        extract_docx_text,
        extract_pdf_text,
        extract_pptx_text,
        extract_svg_text,
        extract_xlsx_text,
    )

    m = _effective_mime(mime, name)
    if m == _MIME_PDF:
        return extract_pdf_text(data)
    if m == _MIME_DOCX:
        return extract_docx_text(data)
    if m == _MIME_XLSX:
        return extract_xlsx_text(data)
    if m == _MIME_PPTX:
        return extract_pptx_text(data)
    if not is_text_readable(mime, name):
        raise AttachmentError(f"「{name}」（{mime or '類型未知'}）沒有可擷取的文字內容。")
    text = data.decode("utf-8", errors="replace")
    if m == "image/svg+xml" or _ext(name) == "svg":
        return (extract_svg_text(text) or text), "SVG"
    return text, "純文字"


def human_size(n: int | None) -> str:
    if n is None:
        return "大小未知"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


class AttachmentStore:
    def __init__(
        self,
        ttl_seconds: int,
        max_entries: int = 500,
        cache_bytes: int = 40 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, Attachment]] = OrderedDict()
        self._downloader: Downloader | None = None
        # 下載內容快取（file_id → bytes），LRU、總量上限；同回合看圖＋上傳不必下載兩次
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_cap = cache_bytes
        self._cache_used = 0

    def bind_downloader(self, downloader: Downloader) -> None:
        """gateway 建好 bot 後綁定下載函式（file_id → bytes）。"""
        self._downloader = downloader

    def add(self, chat_id: int, att: Attachment) -> Attachment:
        now = self._clock()
        for key in [k for k, (at, _) in self._items.items() if now - at > self._ttl]:
            self._items.pop(key, None)
        while len(self._items) >= self._max:
            self._items.popitem(last=False)
        # 登記副本：同一份參照（如相簿暫存）可能被多次觸發各自登記，不能共用代號欄位
        att = replace(att, id=self._new_id(), chat_id=chat_id)
        self._items[att.id] = (now, att)
        return att

    def _new_id(self) -> str:
        while True:
            token = secrets.token_hex(3)  # 6 碼，短到 LLM 不易抄錯
            if token not in self._items:
                return token

    def get(self, chat_id: int, att_id: str) -> Attachment:
        item = self._items.get(att_id.strip().strip("[]").lower())
        if item is None or (self._clock() - item[0]) > self._ttl:
            raise AttachmentError(f"找不到附件代號 {att_id}（可能已過期），請使用者重新傳一次檔案。")
        att = item[1]
        if att.chat_id != chat_id:
            raise AttachmentError(f"附件 {att_id} 不屬於這個對話，不能使用。")
        return att

    async def load(self, att: Attachment) -> bytes:
        """下載附件原檔。"""
        if att.size is not None and att.size > TELEGRAM_DOWNLOAD_LIMIT:
            raise AttachmentError(
                f"「{att.file_name}」有 {human_size(att.size)}，超過 Telegram Bot 可下載的 20MB 上限，請改傳雲端連結。"
            )
        return await self._fetch(att.file_id)

    async def load_vision(self, att: Attachment) -> tuple[bytes, str] | None:
        """下載可供視覺處理的圖（原圖或縮圖）→ (bytes, 實際 mime)；不適用時回 None。

        mime 以檔頭嗅探為準：宣稱的 mime 不可靠（貼圖縮圖 webp/jpg 不定、檔案可能副檔名造假），
        送錯 media_type 會讓整個 LLM 呼叫 400。
        """
        src = att.vision_source()
        if src is None:
            return None
        data = await self._fetch(src[0])
        mime = sniff_image_mime(data)
        if mime is None or len(data) > VISION_MAX_BYTES:
            return None
        return data, mime

    async def _fetch(self, file_id: str) -> bytes:
        cached = self._cache.get(file_id)
        if cached is not None:
            self._cache.move_to_end(file_id)
            return cached
        if self._downloader is None:
            raise AttachmentError("附件下載功能尚未就緒，請稍後再試。")
        try:
            data = await self._downloader(file_id)
        except Exception as exc:
            raise AttachmentError(f"下載附件失敗：{exc}") from exc
        self._remember(file_id, data)
        return data

    def _remember(self, file_id: str, data: bytes) -> None:
        if len(data) > self._cache_cap:
            return
        self._cache[file_id] = data
        self._cache_used += len(data)
        while self._cache_used > self._cache_cap and self._cache:
            _, old = self._cache.popitem(last=False)
            self._cache_used -= len(old)
