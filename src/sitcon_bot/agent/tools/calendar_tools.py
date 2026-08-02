"""Google Calendar 工具（DWD，2026-08-02 追加需求）。

建立／查詢／編輯／刪除行事曆活動，支援邀請對象與 Meet（掛既有會議室連結或開新 Meet）。
「Meet 代碼：大籌」這類指涉由 LLM 依背景知識（會議室連結）解析成 meet_url 後帶入。
刪除為破壞性操作：由 system prompt 規範「必須是使用者明確指名的活動」。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...services.calendar_client import CalendarError, CalendarEvent, CalendarService
from .base import Tool, ToolContext
from .external_data import wrap_external


def _fmt_event(ev: CalendarEvent) -> str:
    """一則活動摘要。id／時間／連結為受信任的識別資訊留在圍欄外；
    標題／邀請對象／地點為外部可控自由文字，包進 <external_data>（NFR-6）。"""
    when = f"{ev.start} ～ {ev.end}"
    lines = [f"[{ev.id}]｜{when}｜{ev.html_link}"]
    if ev.meet_url:
        lines.append(f"Meet：{ev.meet_url}")
    who = "、".join(ev.attendees) if ev.attendees else "（無）"
    ext = f"標題：{ev.summary}｜邀請：{who}"
    if ev.location:
        ext += f"｜地點：{ev.location}"
    lines.append(wrap_external(ext))
    return "\n".join(lines)


class _CalendarToolBase(Tool):
    def __init__(self, calendar: CalendarService) -> None:
        self._cal = calendar


# --------------------------------------------------------------------------- #
# 建立
# --------------------------------------------------------------------------- #
class CreateEventArgs(BaseModel):
    title: str = Field(description="活動標題")
    start: str = Field(description="開始時間 YYYY-MM-DD HH:MM（Asia/Taipei）；全天活動只填 YYYY-MM-DD")
    end: str = Field(description="結束時間，格式同 start")
    attendees: list[str] = Field(default_factory=list, description="邀請對象 email 清單（可含 Google 群組信箱）")
    meet_url: str | None = Field(
        None,
        description="要掛的既有 Google Meet 連結（例：背景知識「會議室連結」裡大籌／各組的固定會議室）",
    )
    create_meet: bool = Field(False, description="true＝開一個全新 Meet；與 meet_url 擇一，meet_url 優先")
    description: str | None = Field(None, description="活動描述（選填）")
    location: str | None = Field(None, description="地點（選填）")


class CalendarCreateEventTool(_CalendarToolBase):
    name = "calendar_create_event"
    description = (
        "在行事曆建立活動：時間、邀請對象、Meet（掛既有會議室連結或開新的）。多場活動就多次呼叫。"
    )
    args_model = CreateEventArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, CreateEventArgs)
        try:
            ev = await self._cal.create_event(
                summary=args.title,
                start=args.start,
                end=args.end,
                attendees=args.attendees,
                description=args.description,
                location=args.location,
                meet_url=args.meet_url,
                create_meet=args.create_meet,
            )
        except CalendarError as exc:
            return f"建立活動失敗：{exc}"
        invited = ""
        if args.attendees:
            invited = (
                "（已寄邀請信給邀請對象）"
                if self._cal.notifies_attendees
                else "（依設定未寄邀請信；邀請對象的日曆上仍會出現活動）"
            )
        parts = [f"✅ 已建立活動{invited}", _fmt_event(ev)]
        if args.create_meet and not ev.meet_url:
            parts.append("⚠️ 新 Meet 未建立成功（可能尚在生成），請點活動連結確認。")
        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 查詢
# --------------------------------------------------------------------------- #
class ListEventsArgs(BaseModel):
    time_min: str = Field(description="區間起：YYYY-MM-DD（含當日）或 YYYY-MM-DD HH:MM")
    time_max: str = Field(description="區間迄：YYYY-MM-DD（含當日）或 YYYY-MM-DD HH:MM")
    query: str | None = Field(None, description="標題／內容關鍵字（選填）")


class CalendarListEventsTool(_CalendarToolBase):
    name = "calendar_list_events"
    description = "查詢行事曆活動（日期區間＋關鍵字）。編輯／刪除前先用這個找出活動 id。"
    args_model = ListEventsArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, ListEventsArgs)
        try:
            events = await self._cal.list_events(time_min=args.time_min, time_max=args.time_max, query=args.query)
        except CalendarError as exc:
            return f"查詢失敗：{exc}"
        if not events:
            return "這個區間查無活動。"
        shown = events[:10]
        header = f"共 {len(events)} 場" + ("（顯示前 10 場，可縮小區間）" if len(events) > 10 else "") + "："
        return header + "\n" + "\n".join(_fmt_event(e) for e in shown)


# --------------------------------------------------------------------------- #
# 編輯
# --------------------------------------------------------------------------- #
class UpdateEventArgs(BaseModel):
    event_id: str = Field(description="活動 id（先用 calendar_list_events 取得）")
    title: str | None = None
    start: str | None = Field(None, description="新開始時間 YYYY-MM-DD HH:MM；改時間時 start/end 要一起給")
    end: str | None = None
    add_attendees: list[str] = Field(default_factory=list, description="要加入的邀請對象 email")
    remove_attendees: list[str] = Field(default_factory=list, description="要移除的邀請對象 email")
    meet_url: str | None = Field(None, description="改掛的既有 Meet 連結")
    description: str | None = None
    location: str | None = None


class CalendarUpdateEventTool(_CalendarToolBase):
    name = "calendar_update_event"
    description = "編輯既有活動：標題、時間、邀請對象（增減）、Meet 連結、描述、地點。"
    args_model = UpdateEventArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, UpdateEventArgs)
        try:
            ev = await self._cal.update_event(
                args.event_id,
                summary=args.title,
                start=args.start,
                end=args.end,
                add_attendees=args.add_attendees or None,
                remove_attendees=args.remove_attendees or None,
                description=args.description,
                location=args.location,
                meet_url=args.meet_url,
            )
        except CalendarError as exc:
            return f"編輯活動失敗：{exc}"
        return "✅ 已更新活動\n" + _fmt_event(ev)


# --------------------------------------------------------------------------- #
# 刪除（破壞性）
# --------------------------------------------------------------------------- #
class DeleteEventArgs(BaseModel):
    event_id: str = Field(description="要刪除的活動 id（必須是使用者明確指名的活動）")


class CalendarDeleteEventTool(_CalendarToolBase):
    name = "calendar_delete_event"
    description = "刪除行事曆活動（破壞性；必須是使用者明確指名的活動）。"
    args_model = DeleteEventArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, DeleteEventArgs)
        try:
            ev = await self._cal.delete_event(args.event_id)
        except CalendarError as exc:
            return f"刪除活動失敗：{exc}"
        note = "（已通知邀請對象）" if self._cal.notifies_attendees and ev.attendees else ""
        return f"✅ 已刪除活動{note}\n" + _fmt_event(ev)


def build_calendar_tools(calendar: CalendarService) -> list[Tool]:
    return [
        CalendarCreateEventTool(calendar),
        CalendarListEventsTool(calendar),
        CalendarUpdateEventTool(calendar),
        CalendarDeleteEventTool(calendar),
    ]
