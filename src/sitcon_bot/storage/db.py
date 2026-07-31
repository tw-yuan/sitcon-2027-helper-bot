"""SQLite 建表與存取（SPEC 第 12 章）。

單一 aiosqlite 連線；aiosqlite 於背景執行緒序列化所有操作，天然滿足
「單一 writer、低寫入量」需求（AGENTS 6.8），避免鎖衝突。重啟後資料保留（NFR-2）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)


def _ensure_parent_dir(path: str) -> None:
    """建立 DB 檔的父目錄（同步小工具，供 connect 以 to_thread 呼叫）。"""
    if path != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)

# SPEC 第 12 章：授權清單與稽核兩張表（欄位與型別逐一對應）。
SCHEMA = """
CREATE TABLE IF NOT EXISTS authorized_groups (
    chat_id        INTEGER PRIMARY KEY,   -- Telegram chat id（負數）
    title          TEXT,                  -- 授權當下的群組名稱
    authorized_by  INTEGER,               -- 管理員 user id
    authorized_at  TEXT                   -- ISO8601
);

CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT,                  -- ISO8601（UTC）
    chat_id        INTEGER,
    chat_title     TEXT,
    user_id        INTEGER,
    username       TEXT,
    trigger_text   TEXT,                  -- 觸發原文
    action         TEXT,                  -- ex: gitlab.create_issue / drive.search / clarify / error
    target         TEXT,                  -- ex: issue#42 / noteId / 查詢條件摘要
    detail         TEXT,                  -- JSON：工具參數摘要與結果
    status         TEXT,                  -- ok / error / clarify
    error          TEXT                   -- 錯誤摘要（可空）
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts);
CREATE INDEX IF NOT EXISTS idx_audit_chat ON audit_log (chat_id);

-- NT-4：里程碑預告訂閱（每群一列；teams 空字串＝全部組別）
CREATE TABLE IF NOT EXISTS milestone_subscriptions (
    chat_id        INTEGER PRIMARY KEY,   -- Telegram chat id（須為已授權群組）
    title          TEXT,                  -- 設定當下的群組名稱（供 /notify_list 顯示）
    teams          TEXT,                  -- 逗號分隔的主導組別；空字串＝全部
    thread_id      INTEGER,               -- forum topic id（在哪個 topic 設定就送到哪；非 forum 為 NULL）
    updated_by     INTEGER,               -- 管理員 user id
    updated_at     TEXT                   -- ISO8601（UTC）
);

-- NT-7：排程狀態鍵值表（目前僅存最後一次成功送出的預告日期，避免重啟後重送）
CREATE TABLE IF NOT EXISTS notify_state (
    key            TEXT PRIMARY KEY,
    value          TEXT,
    updated_at     TEXT                   -- ISO8601（UTC）
);
"""

Params = Sequence[Any]


class Database:
    """薄封裝的 aiosqlite 連線，附 schema 初始化與常用查詢輔助。"""

    def __init__(self, conn: aiosqlite.Connection, path: str) -> None:
        self._conn = conn
        self.path = path

    @classmethod
    async def connect(cls, path: str) -> Database:
        """開啟（必要時建立）資料庫，設定 pragma 並建表。"""
        await asyncio.to_thread(_ensure_parent_dir, path)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        # WAL 提升併發讀寫；busy_timeout 緩衝短暫鎖競爭（EC-16）。
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        db = cls(conn, path)
        await db._init_schema()
        log.info("SQLite 就緒：%s", path)
        return db

    async def _init_schema(self) -> None:
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    @property
    def connection(self) -> aiosqlite.Connection:
        return self._conn

    async def execute(self, sql: str, params: Params = ()) -> None:
        """執行單一寫入並 commit。"""
        await self._conn.execute(sql, params)
        await self._conn.commit()

    async def insert(self, sql: str, params: Params = ()) -> int:
        """執行 INSERT 並回傳 lastrowid。"""
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return int(cur.lastrowid or 0)

    async def fetchall(self, sql: str, params: Params = ()) -> list[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Params = ()) -> aiosqlite.Row | None:
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def close(self) -> None:
        await self._conn.close()
