"""里程碑預告的訂閱清單與排程狀態（NT-4／NT-7）。

一個群組一列；`teams` 為逗號分隔的主導組別，空字串代表「全部組別」。
撤銷群組授權（/revoke）時一併刪除訂閱——未授權群組不該再收到任何主動訊息（AUTH-3 精神）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from aiosqlite import Row

from ..storage.db import Database

log = logging.getLogger(__name__)

# 排程狀態鍵：最後一次成功送出預告的「目標日期」（YYYY-MM-DD）。
LAST_SENT_KEY = "milestone_digest_last_target_date"

_SELECT = "SELECT chat_id, title, teams, thread_id, updated_by, updated_at FROM milestone_subscriptions"


@dataclass(frozen=True, slots=True)
class Subscription:
    chat_id: int
    title: str | None
    teams: tuple[str, ...]  # 空 tuple＝全部組別
    thread_id: int | None = None  # forum topic：在哪個 topic 設定就送到哪
    updated_by: int | None = None
    updated_at: str | None = None

    @property
    def all_teams(self) -> bool:
        return not self.teams


def _split_teams(raw: str | None) -> tuple[str, ...]:
    return tuple(t.strip() for t in (raw or "").split(",") if t.strip())


class SubscriptionStore:
    """訂閱清單的讀寫（SQLite 持久化，重啟後保留）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def subscribe(
        self,
        chat_id: int,
        title: str | None,
        teams: tuple[str, ...] | list[str],
        by: int,
        thread_id: int | None = None,
    ) -> Subscription:
        """新增或更新訂閱。teams 空＝全部組別。回傳寫入後的狀態。"""
        joined = ",".join(t.strip() for t in teams if t.strip())
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT INTO milestone_subscriptions (chat_id, title, teams, thread_id, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                teams = excluded.teams,
                thread_id = excluded.thread_id,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (chat_id, title, joined, thread_id, by, now),
        )
        log.info(
            "里程碑預告訂閱 chat_id=%s thread_id=%s teams=%r by=%s", chat_id, thread_id, joined or "<全部>", by
        )
        return Subscription(
            chat_id=chat_id,
            title=title,
            teams=_split_teams(joined),
            thread_id=thread_id,
            updated_by=by,
            updated_at=now,
        )

    async def unsubscribe(self, chat_id: int) -> bool:
        """取消訂閱。回傳 True 表示原本有訂閱。"""
        existed = await self.get(chat_id) is not None
        await self._db.execute("DELETE FROM milestone_subscriptions WHERE chat_id = ?", (chat_id,))
        if existed:
            log.info("取消里程碑預告訂閱 chat_id=%s", chat_id)
        return existed

    async def get(self, chat_id: int) -> Subscription | None:
        row = await self._db.fetchone(f"{_SELECT} WHERE chat_id = ?", (chat_id,))
        return None if row is None else self._row_to_sub(row)

    async def list_all(self) -> list[Subscription]:
        rows = await self._db.fetchall(f"{_SELECT} ORDER BY updated_at")
        return [self._row_to_sub(r) for r in rows]

    @staticmethod
    def _row_to_sub(row: Row) -> Subscription:
        return Subscription(
            chat_id=int(row["chat_id"]),
            title=row["title"],
            teams=_split_teams(row["teams"]),
            thread_id=row["thread_id"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
        )


class NotifyStateStore:
    """排程狀態（鍵值）。用於「今天的預告是否已送出」的冪等判斷。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str) -> str | None:
        row = await self._db.fetchone("SELECT value FROM notify_state WHERE key = ?", (key,))
        return None if row is None else row["value"]

    async def set(self, key: str, value: str) -> None:
        await self._db.execute(
            """
            INSERT INTO notify_state (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, datetime.now(UTC).isoformat()),
        )
