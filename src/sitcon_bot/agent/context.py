"""反問續接與回覆續接狀態（純 reply-chain 模型）。

小石採「一訊息一 session」：每則觸發預設無狀態；脈絡只沿「回覆鏈」傳遞（TRIG-4 修訂）：

1. 回覆 ask_user 問句 → 以待答狀態（PendingStore）續接，答案填回原 tool_result。
2. 回覆小石的一般回覆 →【2026-08-11 修訂】以該回合的**完整 transcript**（含工具呼叫與結果）
   續接（HistoryStore），小石因此記得上一輪查到什麼、做了什麼；先前只帶被回覆訊息的文字，
   追問「那第二筆呢」會失憶。
3. 回覆其他訊息（他人訊息、過期或重啟後的小石訊息）→ 退回只帶被回覆訊息文字當脈絡。

兩個 store 都以 (chat_id, bot 訊息 message_id) 為鍵、TTL 與筆數上限自動淘汰，重啟即失效
（可接受：退回第 3 種）。Pending 為一次性（take 即移除）；History 為 peek（同一則回覆可被
多次、多人接續，各自展開新的回覆鏈）。transcript 存入前先修剪到字數預算，避免長鏈無限成長。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..services.llm.base import Message, TextBlock, ToolResultBlock, ToolUseBlock

_Clock = Callable[[], float]

# 續接 transcript 的字數預算（約略對應 token 上限的安全值；超過時從最舊的完整回合開始丟）
HISTORY_CHAR_BUDGET = 60_000


@dataclass(slots=True)
class Pending:
    """一次未完成的反問（ask_user）狀態，供使用者回覆問句後續接。"""

    messages: list[Message]
    resolved_results: list[ToolResultBlock]
    ask_user_id: str


@dataclass(slots=True)
class _Entry[V]:
    value: V
    at: float


class _TtlDict[V]:
    """以 (chat_id, message_id) 為鍵的 TTL 字典；筆數達上限時淘汰最舊者。"""

    def __init__(self, ttl_seconds: int, max_entries: int, clock: _Clock) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._store: dict[tuple[int, int], _Entry[V]] = {}

    def put(self, chat_id: int, message_id: int, value: V) -> None:
        self._evict()
        self._store[(chat_id, message_id)] = _Entry(value, self._clock())

    def pop(self, chat_id: int, message_id: int) -> V | None:
        entry = self._store.pop((chat_id, message_id), None)
        if entry is None or (self._clock() - entry.at) > self._ttl:
            return None
        return entry.value

    def peek(self, chat_id: int, message_id: int) -> V | None:
        entry = self._store.get((chat_id, message_id))
        if entry is None or (self._clock() - entry.at) > self._ttl:
            return None
        return entry.value

    def _evict(self) -> None:
        now = self._clock()
        for k in [k for k, e in self._store.items() if (now - e.at) > self._ttl]:
            self._store.pop(k, None)
        if len(self._store) >= self._max:  # 硬上限：移除最舊者
            for k, _ in sorted(self._store.items(), key=lambda kv: kv[1].at)[: len(self._store) - self._max + 1]:
                self._store.pop(k, None)


class PendingStore:
    """以 (chat_id, message_id) 為鍵保存 ask_user 待答狀態；take 即移除（一次性）。"""

    def __init__(self, ttl_seconds: int, max_entries: int = 500, clock: _Clock = time.monotonic) -> None:
        self._d: _TtlDict[Pending] = _TtlDict(ttl_seconds, max_entries, clock)

    def put(self, chat_id: int, message_id: int, pending: Pending) -> None:
        self._d.put(chat_id, message_id, pending)

    def take(self, chat_id: int, message_id: int) -> Pending | None:
        """取出並移除；過期則視為不存在。"""
        return self._d.pop(chat_id, message_id)


def _block_size(block: object) -> int:
    if isinstance(block, TextBlock):
        return len(block.text)
    if isinstance(block, ToolResultBlock):
        return len(block.content)
    if isinstance(block, ToolUseBlock):
        return len(str(block.input))
    return 0


def _message_size(m: Message) -> int:
    return sum(_block_size(b) for b in m.content)


def _is_turn_start(m: Message) -> bool:
    """使用者「發話」訊息（全 TextBlock）＝安全切點；tool_result 回填訊息不可作為開頭。"""
    return m.role == "user" and bool(m.content) and all(isinstance(b, TextBlock) for b in m.content)


def trim_history(messages: list[Message], budget_chars: int = HISTORY_CHAR_BUDGET) -> list[Message]:
    """把 transcript 修剪到預算內：從最舊的完整回合開始丟，切點只落在使用者發話訊息上
    （確保 tool_use／tool_result 配對完整）。至少保留最後一個完整回合。"""
    total = sum(_message_size(m) for m in messages)
    if total <= budget_chars:
        return list(messages)
    starts = [i for i, m in enumerate(messages) if _is_turn_start(m)]
    if not starts:
        return list(messages)
    for i in starts:
        suffix = messages[i:]
        if sum(_message_size(m) for m in suffix) <= budget_chars:
            return list(suffix)
    return list(messages[starts[-1] :])  # 連最後一回合都超標 → 仍保留它（單回合本身有上限）


class HistoryStore:
    """已完成回合的完整 transcript（含工具呼叫與結果），以小石回覆的 message_id 為鍵。

    get 為 peek（不移除）：同一則回覆可被不同人、不同時間各自接續；TTL 過期即失效。
    """

    def __init__(self, ttl_seconds: int, max_entries: int = 300, clock: _Clock = time.monotonic) -> None:
        self._d: _TtlDict[list[Message]] = _TtlDict(ttl_seconds, max_entries, clock)

    def put(self, chat_id: int, message_id: int, messages: list[Message]) -> None:
        self._d.put(chat_id, message_id, trim_history(messages))

    def get(self, chat_id: int, message_id: int) -> list[Message] | None:
        got = self._d.peek(chat_id, message_id)
        return list(got) if got is not None else None
