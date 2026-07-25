"""授權群組清單（AUTH-1～AUTH-10）。

以 SQLite 持久化（AUTH-9），另在記憶體保留 chat_id 集合供每則訊息快速判定
（TRIG-1 過濾在授權群組收到所有訊息，is_authorized 必須是零 I/O 的同步查詢）。
身分一律以數字 user id 比對（AUTH-10）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from ..storage.db import Database

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AuthorizedGroup:
    chat_id: int
    title: str | None
    authorized_by: int | None
    authorized_at: str | None


class GroupStore:
    """授權清單的讀寫；記憶體集合與 DB 同步。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._authorized: set[int] = set()

    async def load(self) -> int:
        """自 DB 載入授權集合，回傳筆數（啟動時呼叫）。"""
        rows = await self._db.fetchall("SELECT chat_id FROM authorized_groups")
        self._authorized = {int(r["chat_id"]) for r in rows}
        log.info("載入授權群組 %d 個", len(self._authorized))
        return len(self._authorized)

    def is_authorized(self, chat_id: int) -> bool:
        """同步、零 I/O 的授權判定（供 TRIG-1 過濾用）。"""
        return chat_id in self._authorized

    async def authorize(self, chat_id: int, title: str | None, by: int) -> bool:
        """授權群組。回傳 True 表示新授權；False 表示原本已授權（AUTH-2）。"""
        newly = chat_id not in self._authorized
        await self._db.execute(
            """
            INSERT INTO authorized_groups (chat_id, title, authorized_by, authorized_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title
            """,
            (chat_id, title, by, datetime.now(UTC).isoformat()),
        )
        self._authorized.add(chat_id)
        if newly:
            log.info("授權群組 chat_id=%s title=%r by=%s", chat_id, title, by)
        return newly

    async def revoke(self, chat_id: int) -> bool:
        """撤銷授權。回傳 True 表示原本已授權（AUTH-3）。"""
        existed = chat_id in self._authorized
        await self._db.execute("DELETE FROM authorized_groups WHERE chat_id = ?", (chat_id,))
        self._authorized.discard(chat_id)
        if existed:
            log.info("撤銷授權 chat_id=%s", chat_id)
        return existed

    async def list_groups(self) -> list[AuthorizedGroup]:
        """列出授權群組（AUTH-6），依授權時間排序。"""
        rows = await self._db.fetchall(
            "SELECT chat_id, title, authorized_by, authorized_at FROM authorized_groups ORDER BY authorized_at"
        )
        return [
            AuthorizedGroup(
                chat_id=r["chat_id"],
                title=r["title"],
                authorized_by=r["authorized_by"],
                authorized_at=r["authorized_at"],
            )
            for r in rows
        ]

    async def get(self, chat_id: int) -> AuthorizedGroup | None:
        row = await self._db.fetchone(
            "SELECT chat_id, title, authorized_by, authorized_at FROM authorized_groups WHERE chat_id = ?",
            (chat_id,),
        )
        if row is None:
            return None
        return AuthorizedGroup(
            chat_id=row["chat_id"],
            title=row["title"],
            authorized_by=row["authorized_by"],
            authorized_at=row["authorized_at"],
        )
