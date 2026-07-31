"""併發共用工具。

KeyedLock：以「鍵」為單位的 asyncio.Lock 集合。同一鍵序列化、不同鍵完全並行。
用於兩處：
  1. gateway 的 per-chat 序列化（同一群訊息保持順序，不同群並行）；
  2. Drive folder metadata 的 single-flight（同一資料夾同時被多處查時只打一次 API）。

鎖以引用計數管理：最後一個等待者離開即刪除該鍵，避免長期執行下 dict 無上限成長。
所有增減都在單一 event loop 內、且中間不 await，故不需要額外保護。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Hashable
from dataclasses import dataclass


@dataclass(slots=True)
class _Entry:
    lock: asyncio.Lock
    waiters: int = 0


class KeyedLock:
    """`async with keyed_lock(key):` — 同鍵互斥，異鍵並行。"""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[Hashable, _Entry] = {}

    @contextlib.asynccontextmanager
    async def __call__(self, key: Hashable) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = _Entry(asyncio.Lock())
        entry.waiters += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.waiters -= 1
            if entry.waiters <= 0:
                self._entries.pop(key, None)

    def active_keys(self) -> int:
        """目前有人持有或等待的鍵數（測試與觀測用）。"""
        return len(self._entries)
