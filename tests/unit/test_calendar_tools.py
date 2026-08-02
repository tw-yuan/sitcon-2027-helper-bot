"""Calendar（DWD，2026-08-02 追加需求）：時間解析、既有 Meet 掛載、邀請對象增減、工具輸出格式。"""

from __future__ import annotations

from typing import Any

import pytest

from sitcon_bot.agent.tools.base import ToolContext
from sitcon_bot.agent.tools.calendar_tools import (
    CalendarCreateEventTool,
    CalendarDeleteEventTool,
    CalendarListEventsTool,
    CalendarUpdateEventTool,
    CreateEventArgs,
    DeleteEventArgs,
    ListEventsArgs,
    UpdateEventArgs,
)
from sitcon_bot.services.calendar_client import (
    CalendarError,
    CalendarService,
    meet_conference_data,
)

CTX = ToolContext(chat_id=-100, thread_id=None, user_id=42, username="yuan", text="x")


class FakeGateway:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.last_insert_body: dict[str, Any] | None = None
        self.last_patch_body: dict[str, Any] | None = None
        self.last_list_args: tuple[str, str, str | None] | None = None
        self.deleted: list[str] = []
        self._next = 1

    def _materialize(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        ev = dict(self.events.get(event_id, {}))
        ev.update(body)
        ev["id"] = event_id
        ev.setdefault("htmlLink", f"https://calendar.google.com/event?eid={event_id}")
        conf = ev.get("conferenceData") or {}
        if "createRequest" in conf:
            ev["hangoutLink"] = "https://meet.google.com/new-new-new"
        return ev

    async def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.last_insert_body = body
        event_id = f"ev{self._next}"
        self._next += 1
        self.events[event_id] = self._materialize(event_id, body)
        return dict(self.events[event_id])

    async def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.last_patch_body = body
        self.events[event_id] = self._materialize(event_id, body)
        return dict(self.events[event_id])

    async def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        ev = self.events.get(event_id)
        return dict(ev) if ev is not None else None

    async def list_events(
        self, calendar_id: str, time_min: str, time_max: str, query: str | None
    ) -> list[dict[str, Any]]:
        self.last_list_args = (time_min, time_max, query)
        return [dict(e) for e in self.events.values()]

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.deleted.append(event_id)
        self.events.pop(event_id, None)


def _service(gw: FakeGateway) -> CalendarService:
    return CalendarService(gw, calendar_id="primary", tz="Asia/Taipei")


# ------------------------------------------------------------------ #
# meet_conference_data／時間解析
# ------------------------------------------------------------------ #
def test_meet_conference_data_from_url() -> None:
    data = meet_conference_data("https://meet.google.com/uee-eyar-cos")
    assert data["conferenceId"] == "uee-eyar-cos"
    assert data["entryPoints"][0]["uri"] == "https://meet.google.com/uee-eyar-cos"
    assert data["conferenceSolution"]["key"]["type"] == "hangoutsMeet"


def test_meet_conference_data_bare_code_with_domain() -> None:
    assert meet_conference_data("meet.google.com/abc-defg-hij")["conferenceId"] == "abc-defg-hij"


def test_meet_conference_data_rejects_garbage() -> None:
    with pytest.raises(CalendarError, match="Meet 連結"):
        meet_conference_data("https://example.com/xyz")


# ------------------------------------------------------------------ #
# 建立
# ------------------------------------------------------------------ #
async def test_create_event_with_attendees_and_existing_meet() -> None:
    gw = FakeGateway()
    ev = await _service(gw).create_event(
        summary="SITCON 2027 零籌",
        start="2026-08-15 17:00",
        end="2026-08-15 20:00",
        attendees=["sitcon@googlegroups.com"],
        meet_url="https://meet.google.com/uee-eyar-cos",
    )
    body = gw.last_insert_body
    assert body["start"] == {"dateTime": "2026-08-15T17:00", "timeZone": "Asia/Taipei"}
    assert body["end"] == {"dateTime": "2026-08-15T20:00", "timeZone": "Asia/Taipei"}
    assert body["attendees"] == [{"email": "sitcon@googlegroups.com"}]
    assert body["conferenceData"]["conferenceId"] == "uee-eyar-cos"
    assert ev.meet_url == "https://meet.google.com/uee-eyar-cos"
    assert ev.attendees == ["sitcon@googlegroups.com"]


async def test_create_all_day_event() -> None:
    gw = FakeGateway()
    await _service(gw).create_event(summary="行前準備日", start="2026-08-14", end="2026-08-14")
    assert gw.last_insert_body["start"] == {"date": "2026-08-14"}


async def test_create_event_new_meet_uses_create_request() -> None:
    gw = FakeGateway()
    ev = await _service(gw).create_event(
        summary="x", start="2026-08-15 17:00", end="2026-08-15 18:00", create_meet=True
    )
    conf = gw.last_insert_body["conferenceData"]
    assert conf["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"
    assert ev.meet_url == "https://meet.google.com/new-new-new"


async def test_create_event_bad_time_format() -> None:
    with pytest.raises(CalendarError, match="時間格式"):
        await _service(FakeGateway()).create_event(summary="x", start="8/15 17:00", end="8/15 18:00")


# ------------------------------------------------------------------ #
# 編輯（attendees 增減以現值合併）
# ------------------------------------------------------------------ #
async def test_update_event_attendee_merge() -> None:
    gw = FakeGateway()
    svc = _service(gw)
    ev = await svc.create_event(
        summary="x", start="2026-08-15 17:00", end="2026-08-15 18:00",
        attendees=["a@x.tw", "b@x.tw"],
    )
    await svc.update_event(ev.id, add_attendees=["c@x.tw"], remove_attendees=["A@x.tw"])  # 大小寫不敏感
    assert gw.last_patch_body["attendees"] == [{"email": "b@x.tw"}, {"email": "c@x.tw"}]


async def test_update_event_no_change_returns_current() -> None:
    gw = FakeGateway()
    svc = _service(gw)
    ev = await svc.create_event(summary="x", start="2026-08-15 17:00", end="2026-08-15 18:00")
    got = await svc.update_event(ev.id)
    assert got.id == ev.id
    assert gw.last_patch_body is None  # 無變更不打 API


# ------------------------------------------------------------------ #
# 工具層：輸出格式與 external 包裹
# ------------------------------------------------------------------ #
async def test_create_tool_reports_and_wraps_external() -> None:
    gw = FakeGateway()
    tool = CalendarCreateEventTool(_service(gw))
    reply = await tool.run(
        CreateEventArgs(
            title="SITCON 2027 零籌",
            start="2026-08-15 17:00",
            end="2026-08-15 20:00",
            attendees=["sitcon@googlegroups.com"],
            meet_url="https://meet.google.com/uee-eyar-cos",
        ),
        CTX,
    )
    assert "✅ 已建立活動" in reply
    assert "已寄邀請信" in reply  # 預設 notifies_attendees=True
    assert "Meet：https://meet.google.com/uee-eyar-cos" in reply
    assert "<external_data>" in reply  # 標題／邀請對象為外部可控（NFR-6）
    assert "SITCON 2027 零籌" in reply


async def test_create_tool_no_invite_mail_when_send_updates_none() -> None:
    gw = FakeGateway()
    svc = CalendarService(gw, calendar_id="primary", tz="Asia/Taipei", notifies_attendees=False)
    tool = CalendarCreateEventTool(svc)
    reply = await tool.run(
        CreateEventArgs(
            title="x", start="2026-08-15 17:00", end="2026-08-15 20:00", attendees=["a@x.tw"]
        ),
        CTX,
    )
    assert "未寄邀請信" in reply
    assert "已寄邀請信" not in reply


async def test_list_tool_time_range_covers_whole_days() -> None:
    gw = FakeGateway()
    svc = _service(gw)
    await svc.create_event(summary="a", start="2026-08-15 17:00", end="2026-08-15 18:00")
    tool = CalendarListEventsTool(svc)
    reply = await tool.run(ListEventsArgs(time_min="2026-08-15", time_max="2026-08-15"), CTX)
    time_min, time_max, _ = gw.last_list_args
    assert time_min == "2026-08-15T00:00:00+08:00"  # 帶 offset 的 RFC3339
    assert time_max == "2026-08-15T23:59:59+08:00"
    assert "共 1 場" in reply


async def test_list_tool_empty() -> None:
    tool = CalendarListEventsTool(_service(FakeGateway()))
    reply = await tool.run(ListEventsArgs(time_min="2026-08-15", time_max="2026-08-15"), CTX)
    assert "查無活動" in reply


async def test_update_tool_reports() -> None:
    gw = FakeGateway()
    svc = _service(gw)
    ev = await svc.create_event(summary="舊", start="2026-08-15 17:00", end="2026-08-15 18:00")
    tool = CalendarUpdateEventTool(svc)
    reply = await tool.run(UpdateEventArgs(event_id=ev.id, title="新", start="2026-08-16 17:00",
                                           end="2026-08-16 18:00"), CTX)
    assert "✅ 已更新活動" in reply
    assert "2026-08-16T17:00" in reply


async def test_delete_tool_reports_deleted_event() -> None:
    gw = FakeGateway()
    svc = _service(gw)
    ev = await svc.create_event(summary="要刪的", start="2026-08-15 17:00", end="2026-08-15 18:00")
    tool = CalendarDeleteEventTool(svc)
    reply = await tool.run(DeleteEventArgs(event_id=ev.id), CTX)
    assert "✅ 已刪除活動" in reply
    assert gw.deleted == [ev.id]


async def test_delete_tool_missing_event() -> None:
    tool = CalendarDeleteEventTool(_service(FakeGateway()))
    reply = await tool.run(DeleteEventArgs(event_id="nope"), CTX)
    assert "刪除活動失敗" in reply
    assert "找不到" in reply
