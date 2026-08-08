"""群組記憶的讀寫（SQLite 持久化，重啟後保留）。

各群使用者可要求小石「記住」事項（偏好、慣例、常用資訊），保存後注入該群的
system prompt，讓後續做事時自動參考。刪除以 (chat_id, id) 雙鍵限定，
避免跨群誤刪；撤銷群組授權（/revoke）時一併清空該群記憶。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from aiosqlite import Row

from .db import Database

log = logging.getLogger(__name__)

# 防呆上限：單群筆數與單筆長度（超過時工具會請使用者先整理，而非默默截斷）
MAX_MEMORIES_PER_GROUP = 30
MAX_CONTENT_CHARS = 500

_SELECT = "SELECT id, chat_id, content, created_by, created_by_name, created_at FROM group_memories"


@dataclass(frozen=True, slots=True)
class GroupMemory:
    id: int
    chat_id: int
    content: str
    created_by: int | None = None
    created_by_name: str | None = None
    created_at: str | None = None


class GroupMemoryStore:
    """群組記憶的 CRUD。上限檢查由工具層負責（回覆得了人話）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self, chat_id: int, content: str, by: int | None = None, by_name: str | None = None
    ) -> GroupMemory:
        now = datetime.now(UTC).isoformat()
        mem_id = await self._db.insert(
            """
            INSERT INTO group_memories (chat_id, content, created_by, created_by_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, content, by, by_name, now),
        )
        log.info("群組記憶新增 chat_id=%s id=%s by=%s", chat_id, mem_id, by)
        return GroupMemory(
            id=mem_id, chat_id=chat_id, content=content, created_by=by, created_by_name=by_name, created_at=now
        )

    async def list_for(self, chat_id: int) -> list[GroupMemory]:
        rows = await self._db.fetchall(f"{_SELECT} WHERE chat_id = ? ORDER BY id", (chat_id,))
        return [self._row_to_memory(r) for r in rows]

    async def count(self, chat_id: int) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) AS n FROM group_memories WHERE chat_id = ?", (chat_id,))
        return int(row["n"]) if row is not None else 0

    async def delete(self, chat_id: int, memory_id: int) -> GroupMemory | None:
        """刪除該群的一筆記憶。回傳被刪的內容；不存在（或屬別群）回 None。"""
        row = await self._db.fetchone(f"{_SELECT} WHERE chat_id = ? AND id = ?", (chat_id, memory_id))
        if row is None:
            return None
        await self._db.execute("DELETE FROM group_memories WHERE chat_id = ? AND id = ?", (chat_id, memory_id))
        log.info("群組記憶刪除 chat_id=%s id=%s", chat_id, memory_id)
        return self._row_to_memory(row)

    async def clear(self, chat_id: int) -> int:
        """清空該群全部記憶（/revoke 用）。回傳刪除筆數。"""
        n = await self.count(chat_id)
        await self._db.execute("DELETE FROM group_memories WHERE chat_id = ?", (chat_id,))
        if n:
            log.info("群組記憶清空 chat_id=%s（%d 筆）", chat_id, n)
        return n

    @staticmethod
    def _row_to_memory(row: Row) -> GroupMemory:
        return GroupMemory(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            content=row["content"],
            created_by=row["created_by"],
            created_by_name=row["created_by_name"],
            created_at=row["created_at"],
        )
