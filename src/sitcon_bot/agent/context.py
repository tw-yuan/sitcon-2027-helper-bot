"""反問續接狀態（純 reply-chain 模型）。

小石採「一訊息一 session」：每則觸發預設無狀態；只有當使用者「回覆」某則訊息時，才把被回覆
訊息的內容當脈絡（由 gateway 取 reply_to 內容注入，內容一律視為資料）。唯一需伺服器端保存的是
ask_user 反問的進行中狀態——以 bot 問句訊息的 message_id 為鍵存放，使用者回覆該問句時據以續接。
TTL 與筆數上限避免無限成長。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..services.llm.base import Message, ToolResultBlock

_Clock = Callable[[], float]
PendingKey = tuple[int, int]  # (chat_id, bot 問句 message_id)


@dataclass(slots=True)
class Pending:
    """一次未完成的反問（ask_user）狀態，供使用者回覆問句後續接。"""

    messages: list[Message]
    resolved_results: list[ToolResultBlock]
    ask_user_id: str


@dataclass(slots=True)
class _Entry:
    pending: Pending
    at: float


class PendingStore:
    """以 (chat_id, message_id) 為鍵保存 ask_user 待答狀態；TTL 過期與筆數上限自動淘汰。"""

    def __init__(self, ttl_seconds: int, max_entries: int = 500, clock: _Clock = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._store: dict[PendingKey, _Entry] = {}

    def put(self, chat_id: int, message_id: int, pending: Pending) -> None:
        self._evict()
        self._store[(chat_id, message_id)] = _Entry(pending, self._clock())

    def take(self, chat_id: int, message_id: int) -> Pending | None:
        """取出並移除；過期則視為不存在。"""
        entry = self._store.pop((chat_id, message_id), None)
        if entry is None or (self._clock() - entry.at) > self._ttl:
            return None
        return entry.pending

    def _evict(self) -> None:
        now = self._clock()
        for k in [k for k, e in self._store.items() if (now - e.at) > self._ttl]:
            self._store.pop(k, None)
        if len(self._store) >= self._max:  # 硬上限：移除最舊者
            for k, _ in sorted(self._store.items(), key=lambda kv: kv[1].at)[: len(self._store) - self._max + 1]:
                self._store.pop(k, None)
