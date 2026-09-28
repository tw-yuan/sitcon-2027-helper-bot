"""使用者附件的讀取工具。附件上傳到 GitLab 卡片／留言則由 GitLab 工具的 attachment_ids 處理。"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from ...services.attachments import AttachmentError, AttachmentStore, extract_text, human_size
from .base import Tool, ToolContext
from .external_data import wrap_external

CONTENT_LIMIT = 12000  # 與 drive_read_file 相同的單次回傳上限


class ReadAttachmentArgs(BaseModel):
    attachment_id: str = Field(description="附件代號（使用者訊息附件清單中方括號內的 6 碼）")
    offset: int = Field(0, ge=0, description="從第幾個字開始讀（內容被截斷時帶上次結果提示的 offset 續讀）")


class AttachmentReadTool(Tool):
    name = "attachment_read"
    description = (
        "讀取使用者在 Telegram 附上的檔案文字內容（PDF、Word、Excel、PowerPoint、純文字／CSV／JSON 等）。"
        "圖片不用這個——能看的圖已直接附在訊息裡。"
    )
    args_model = ReadAttachmentArgs

    def __init__(self, store: AttachmentStore) -> None:
        self._store = store

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, ReadAttachmentArgs)
        try:
            att = self._store.get(ctx.chat_id, args.attachment_id)
            data = await self._store.load(att)
            # 解析是 CPU 工作（PDF 可能上百頁），丟 thread 避免卡住其他對話
            text, kind = await asyncio.to_thread(extract_text, att.mime, att.file_name, data)
        except AttachmentError as exc:
            return str(exc)
        except Exception as exc:  # 壞檔、加密 PDF 等
            return f"「{args.attachment_id}」解析失敗：{exc}"

        text = text.strip("\n")
        total = len(text)
        offset = min(args.offset, total)
        page = text[offset : offset + CONTENT_LIMIT]
        head = f"[{att.id}] {kind}｜{human_size(att.size)}｜共 {total} 字，本次 {offset}～{offset + len(page)}"
        tail = ""
        if offset + CONTENT_LIMIT < total:
            tail = f"\n（內容未完，續讀請帶 offset={offset + CONTENT_LIMIT}）"
        if not page.strip():
            return head + "\n（沒有擷取到文字；可能是掃描檔或純圖片內容）"
        return head + "\n" + wrap_external(f"檔名：{att.file_name}\n{page}") + tail


def build_attachment_tools(store: AttachmentStore) -> list[Tool]:
    return [AttachmentReadTool(store)]
