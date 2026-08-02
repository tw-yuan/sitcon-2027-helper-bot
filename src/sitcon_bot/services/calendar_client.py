"""Google Calendar（domain-wide delegation，2026-08-02 追加需求）。

以 service account 冒用 GOOGLE_DWD_SUBJECT 指定的帳號操作其行事曆（預設 primary）：
建立／查詢／編輯／刪除活動，支援邀請對象（attendees）、掛既有 Meet 連結（如背景知識裡的
會議室連結）或為活動開全新 Meet。

- 既有 Meet：Calendar API 不能「指定代碼開會議」，但接受把另一場會議的 conferenceData
  複製到新活動（conferenceDataVersion=1 + conferenceId + entryPoints），效果即掛上既有
  會議室連結；固定會議室（大籌／各組）都適用。
- 邀請通知依 CALENDAR_SEND_UPDATES 設定（all＝寄邀請信／externalOnly＝只寄網域外／none＝不寄；
  none 時邀請對象仍會在自己的 Google 日曆上看到活動，只是沒有 email）。
- 時間一律以設定時區（Asia/Taipei）解析；只給日期（YYYY-MM-DD）視為全天活動。
- DWD 未在 Workspace 後台授權時 API 回 unauthorized_client／invalid_grant，轉為可讀訊息。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .google_http import GOOGLE_NUM_RETRIES, build_google_service, request_http

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
_MEET_RE = re.compile(r"(?:https?://)?meet\.google\.com/([a-z][a-z0-9-]+)", re.IGNORECASE)
_LIST_MAX = 50


class CalendarError(Exception):
    """Calendar 操作錯誤；訊息可直接回給 LLM。"""


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """回給工具層的活動摘要。"""

    id: str
    summary: str
    start: str
    end: str
    html_link: str
    meet_url: str | None = None
    attendees: list[str] = field(default_factory=list)
    location: str | None = None
    description: str | None = None

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> CalendarEvent:
        start = d.get("start") or {}
        end = d.get("end") or {}
        meet = d.get("hangoutLink")
        if not meet:
            for ep in (d.get("conferenceData") or {}).get("entryPoints", []):
                if ep.get("entryPointType") == "video":
                    meet = ep.get("uri")
                    break
        return cls(
            id=d.get("id", ""),
            summary=d.get("summary", "（無標題）"),
            start=start.get("dateTime") or start.get("date") or "",
            end=end.get("dateTime") or end.get("date") or "",
            html_link=d.get("htmlLink", ""),
            meet_url=meet,
            attendees=[a["email"] for a in d.get("attendees", []) if a.get("email")],
            location=d.get("location"),
            description=d.get("description"),
        )


class CalendarGateway(Protocol):
    """Google Calendar I/O 抽象（可注入假物件）。"""

    async def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    async def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    async def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None: ...
    async def list_events(
        self, calendar_id: str, time_min: str, time_max: str, query: str | None
    ) -> list[dict[str, Any]]: ...
    async def delete_event(self, calendar_id: str, event_id: str) -> None: ...


def meet_conference_data(meet_url: str) -> dict[str, Any]:
    """把既有 Meet 連結組成 conferenceData（複製語意，掛上既有會議室）。格式不對拋 CalendarError。"""
    m = _MEET_RE.search(meet_url.strip())
    if m is None:
        raise CalendarError(f"Meet 連結格式看不懂：{meet_url}（應為 https://meet.google.com/xxx-xxxx-xxx）")
    code = m.group(1).lower()
    uri = f"https://meet.google.com/{code}"
    return {
        "conferenceSolution": {"key": {"type": "hangoutsMeet"}, "name": "Google Meet"},
        "conferenceId": code,
        "entryPoints": [{"entryPointType": "video", "uri": uri, "label": f"meet.google.com/{code}"}],
    }


def _time_field(value: str, tz: str) -> dict[str, str]:
    """'YYYY-MM-DD HH:MM'（或 ISO）→ dateTime＋timeZone；只有日期 → 全天活動的 date。"""
    v = value.strip().replace(" ", "T")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return {"date": v}
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?", v):
        raise CalendarError(f"時間格式看不懂：{value}（用 YYYY-MM-DD HH:MM，或全天活動只給 YYYY-MM-DD）")
    return {"dateTime": v, "timeZone": tz}


class CalendarService:
    def __init__(
        self,
        gateway: CalendarGateway,
        calendar_id: str = "primary",
        tz: str = "Asia/Taipei",
        notifies_attendees: bool = True,
    ) -> None:
        self._gw = gateway
        self._calendar_id = calendar_id
        self._tz = tz
        # 供工具層決定回報措辭（實際寄不寄由 gateway 的 sendUpdates 決定，兩者同源於設定）
        self.notifies_attendees = notifies_attendees

    async def create_event(
        self,
        *,
        summary: str,
        start: str,
        end: str,
        attendees: list[str] | None = None,
        description: str | None = None,
        location: str | None = None,
        meet_url: str | None = None,
        create_meet: bool = False,
    ) -> CalendarEvent:
        body: dict[str, Any] = {
            "summary": summary,
            "start": _time_field(start, self._tz),
            "end": _time_field(end, self._tz),
        }
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees]
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if meet_url:
            body["conferenceData"] = meet_conference_data(meet_url)
        elif create_meet:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": uuid.uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
        raw = await self._gw.insert_event(self._calendar_id, body)
        return CalendarEvent.from_raw(raw)

    async def update_event(
        self,
        event_id: str,
        *,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        add_attendees: list[str] | None = None,
        remove_attendees: list[str] | None = None,
        description: str | None = None,
        location: str | None = None,
        meet_url: str | None = None,
    ) -> CalendarEvent:
        """patch 語意：只送有變更的欄位；attendees 以現值增減後整組送回（API 是整組覆蓋）。"""
        body: dict[str, Any] = {}
        if summary is not None:
            body["summary"] = summary
        if start is not None:
            body["start"] = _time_field(start, self._tz)
        if end is not None:
            body["end"] = _time_field(end, self._tz)
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        if meet_url is not None:
            body["conferenceData"] = meet_conference_data(meet_url)
        if add_attendees or remove_attendees:
            current = await self.get_event(event_id)
            drop = {e.lower() for e in (remove_attendees or [])}
            final = [e for e in current.attendees if e.lower() not in drop]
            final.extend(e for e in (add_attendees or []) if e.lower() not in {f.lower() for f in final})
            body["attendees"] = [{"email": e} for e in final]
        if not body:
            return await self.get_event(event_id)
        raw = await self._gw.patch_event(self._calendar_id, event_id, body)
        return CalendarEvent.from_raw(raw)

    async def get_event(self, event_id: str) -> CalendarEvent:
        raw = await self._gw.get_event(self._calendar_id, event_id)
        if raw is None:
            raise CalendarError(f"找不到這個活動（id={event_id}），可能已被刪除。")
        return CalendarEvent.from_raw(raw)

    def _rfc3339(self, value: str, *, end_of_day: bool) -> str:
        """時間字串 → 帶時區 offset 的 RFC3339（timeMin/timeMax 必須帶 offset）。"""
        v = value.strip().replace(" ", "T")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            v += "T23:59:59" if end_of_day else "T00:00:00"
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?", v):
            raise CalendarError(f"時間格式看不懂：{value}（用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）")
        return datetime.fromisoformat(v).replace(tzinfo=ZoneInfo(self._tz)).isoformat()

    async def list_events(self, *, time_min: str, time_max: str, query: str | None = None) -> list[CalendarEvent]:
        """查某日期區間的活動（time_min／time_max 給日期時涵蓋整天）。"""
        lo = self._rfc3339(time_min, end_of_day=False)
        hi = self._rfc3339(time_max, end_of_day=True)
        raw = await self._gw.list_events(self._calendar_id, lo, hi, query)
        return [CalendarEvent.from_raw(r) for r in raw]

    async def delete_event(self, event_id: str) -> CalendarEvent:
        """刪除活動；回傳被刪的活動摘要（供回報）。"""
        ev = await self.get_event(event_id)
        await self._gw.delete_event(self._calendar_id, event_id)
        return ev


# --------------------------------------------------------------------------- #
# Google Calendar I/O（DWD）
# --------------------------------------------------------------------------- #
class GoogleCalendarGateway:
    """寫入的 sendUpdates 依設定（all／externalOnly／none）；掛 conferenceData 需 conferenceDataVersion=1。"""

    def __init__(self, sa_json_path: str, subject: str, send_updates: str = "all") -> None:
        self._sa_json_path = sa_json_path
        self._subject = subject
        self._send_updates = send_updates
        self._service: Any = None
        self._creds: Any = None

    def _service_or_build(self) -> Any:
        if self._service is None:
            self._service, self._creds = build_google_service(
                "calendar", "v3", self._sa_json_path, [CALENDAR_SCOPE], subject=self._subject
            )
        return self._service

    def _execute(self, req: Any) -> Any:
        try:
            return req.execute(http=request_http(self._creds), num_retries=GOOGLE_NUM_RETRIES)
        except Exception as exc:  # RefreshError（DWD 未授權）等轉為可讀訊息
            text = str(exc)
            if "unauthorized_client" in text or "invalid_grant" in text:
                raise CalendarError(
                    f"DWD 冒用 {self._subject} 失敗：Workspace 後台尚未對此 service account "
                    f"授權 Calendar scope，請通知管理員。"
                ) from exc
            raise

    def _insert_sync(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        req = self._service_or_build().events().insert(
            calendarId=calendar_id, body=body, conferenceDataVersion=1, sendUpdates=self._send_updates
        )
        return self._execute(req)

    def _patch_sync(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        req = self._service_or_build().events().patch(
            calendarId=calendar_id, eventId=event_id, body=body, conferenceDataVersion=1,
            sendUpdates=self._send_updates,
        )
        return self._execute(req)

    def _get_sync(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        from googleapiclient.errors import HttpError

        req = self._service_or_build().events().get(calendarId=calendar_id, eventId=event_id)
        try:
            return self._execute(req)
        except HttpError as exc:
            if getattr(exc, "status_code", None) in (404, 410):
                return None
            raise

    def _list_sync(
        self, calendar_id: str, time_min: str, time_max: str, query: str | None
    ) -> list[dict[str, Any]]:
        req = self._service_or_build().events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            q=query,
            singleEvents=True,
            orderBy="startTime",
            maxResults=_LIST_MAX,
        )
        resp = self._execute(req)
        return list(resp.get("items", []))

    def _delete_sync(self, calendar_id: str, event_id: str) -> None:
        req = self._service_or_build().events().delete(
            calendarId=calendar_id, eventId=event_id, sendUpdates=self._send_updates
        )
        self._execute(req)

    async def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._insert_sync, calendar_id, body)

    async def patch_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._patch_sync, calendar_id, event_id, body)

    async def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, calendar_id, event_id)

    async def list_events(
        self, calendar_id: str, time_min: str, time_max: str, query: str | None
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_sync, calendar_id, time_min, time_max, query)

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        await asyncio.to_thread(self._delete_sync, calendar_id, event_id)


def build_calendar_service(
    sa_json_path: str,
    subject: str,
    calendar_id: str = "primary",
    tz: str = "Asia/Taipei",
    send_updates: str = "all",
) -> CalendarService:
    return CalendarService(
        GoogleCalendarGateway(sa_json_path, subject, send_updates),
        calendar_id,
        tz,
        notifies_attendees=(send_updates != "none"),
    )
