"""稽核紀錄寫入（LOG-1～LOG-4）。

每次觸發互動寫一筆。detail 以 JSON 存工具參數摘要與結果。ts 存 UTC（AGENTS 6.7）。
LOG-3：非觸發訊息不記錄——由 gateway 端保證（此處僅被觸發互動呼叫）。
LOG-4：呼叫端須確保 detail 不含 RO-2 白名單以外之名冊欄位。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from .db import Database

log = logging.getLogger(__name__)

# status 值域（呼應 SPEC 資料模型註解）
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_CLARIFY = "clarify"


def _now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class AuditEntry:
    """一筆稽核紀錄的讀取視圖。"""

    id: int
    ts: str
    chat_id: int | None
    chat_title: str | None
    user_id: int | None
    username: str | None
    trigger_text: str | None
    action: str | None
    target: str | None
    detail: dict[str, Any] | None
    status: str | None
    error: str | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> AuditEntry:
        detail_raw = row["detail"]
        detail = json.loads(detail_raw) if detail_raw else None
        return cls(
            id=row["id"],
            ts=row["ts"],
            chat_id=row["chat_id"],
            chat_title=row["chat_title"],
            user_id=row["user_id"],
            username=row["username"],
            trigger_text=row["trigger_text"],
            action=row["action"],
            target=row["target"],
            detail=detail,
            status=row["status"],
            error=row["error"],
        )


class AuditLog:
    """稽核紀錄寫入／讀取（讀取僅供主機端與測試，不對群組開放——LOG-2）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        *,
        chat_id: int | None,
        user_id: int | None,
        trigger_text: str | None,
        action: str,
        chat_title: str | None = None,
        username: str | None = None,
        target: str | None = None,
        detail: dict[str, Any] | None = None,
        status: str = STATUS_OK,
        error: str | None = None,
    ) -> int:
        """寫入一筆稽核並回傳其 id。"""
        detail_json = json.dumps(detail, ensure_ascii=False) if detail is not None else None
        row_id = await self._db.insert(
            """
            INSERT INTO audit_log
                (ts, chat_id, chat_title, user_id, username,
                 trigger_text, action, target, detail, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_utc_iso(),
                chat_id,
                chat_title,
                user_id,
                username,
                trigger_text,
                action,
                target,
                detail_json,
                status,
                error,
            ),
        )
        return row_id

    async def recent(self, limit: int = 50) -> list[AuditEntry]:
        """讀取最近的稽核紀錄（供主機端／測試；不對群組開放）。"""
        rows = await self._db.fetchall(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [AuditEntry.from_row(r) for r in rows]

    async def get(self, entry_id: int) -> AuditEntry | None:
        row = await self._db.fetchone("SELECT * FROM audit_log WHERE id = ?", (entry_id,))
        return AuditEntry.from_row(row) if row else None
