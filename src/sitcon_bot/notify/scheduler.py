"""每日里程碑預告排程（NT-6～NT-8、NT-11）。

固定於 Asia/Taipei 每天 `MILESTONE_NOTIFY_HOUR:MINUTE`（預設 23:00）送出**隔天**的里程碑，
並附上**送出當天**仍未關閉的過期 GitLab 卡片提醒（NT-11；卡片不分組別，所有訂閱群都收到）。

設計取捨：
  - 不引入 APScheduler／外部排程（AGENTS 9.9 只允許單機）。以 60 秒 tick 輪詢判斷是否到點，
    成本可忽略，且對系統休眠、時鐘跳動、DST 都天然免疫（每次都重算「今天的到點時刻」）。
  - 冪等：送出後把「目標日期」寫進 notify_state，重啟或重複 tick 都不會重送（NT-7）。
  - 補送：錯過到點（例：23:00 時 bot 正在重啟）時，在 `catchup_minutes` 內啟動仍會補送；
    超過視窗就跳過該日，不會半夜才吵人。
  - 時程表讀取失敗時**不**寫入狀態，讓下一個 tick 在補送視窗內重試。
  - 卡片提醒取得失敗則**降級**為只送里程碑段（不重試整包——里程碑才是主要載荷，
    不因 GitLab 短暫故障把整日預告拖過補送視窗）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..services.milestone_schedule import (
    MilestoneScheduleService,
    MilestoneScheduleUnavailableError,
    select_for_teams,
)
from .cards import CardReminder
from .digest import render_digest
from .subscriptions import LAST_SENT_KEY, NotifyStateStore, Subscription, SubscriptionStore

log = logging.getLogger(__name__)

# 送出一則預告 (chat_id, thread_id, html)：回傳是否成功（失敗不應中斷其餘群組）。
Sender = Callable[[int, int | None, str], Awaitable[bool]]

# 取得以某日為基準（due ≤ 該日）的過期卡片清單；失敗往外拋，由排程端決定降級。
CardsProvider = Callable[[date], Awaitable[list[CardReminder]]]

TICK_SECONDS = 60.0


class MilestoneNotifier:
    """每日預告的排程與派送。"""

    def __init__(
        self,
        *,
        schedule: MilestoneScheduleService,
        subscriptions: SubscriptionStore,
        state: NotifyStateStore,
        sender: Sender,
        cards: CardsProvider | None = None,
        tz: str = "Asia/Taipei",
        hour: int = 23,
        minute: int = 0,
        catchup_minutes: int = 60,
        always_teams: tuple[str, ...] | list[str] = (),
        send_when_empty: bool = False,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self._schedule = schedule
        self._subs = subscriptions
        self._state = state
        self._sender = sender
        self._cards = cards
        self._tz = ZoneInfo(tz)
        self._hour = hour
        self._minute = minute
        self._catchup = timedelta(minutes=max(0, catchup_minutes))
        self._always = tuple(always_teams)
        self._send_when_empty = send_when_empty
        self._tick_seconds = tick_seconds

    # ------------------------------------------------------------------ #
    # 排程
    # ------------------------------------------------------------------ #
    def now(self) -> datetime:
        return datetime.now(self._tz)

    def due_at(self, now: datetime) -> datetime:
        """now 當天的到點時刻。"""
        return now.replace(hour=self._hour, minute=self._minute, second=0, microsecond=0)

    async def tick(self, now: datetime | None = None) -> int:
        """檢查是否該送出；回傳實際送出的群組數（未到點／已送過皆回 0）。"""
        current = now or self.now()
        due = self.due_at(current)
        if current < due or current - due > self._catchup:
            return 0
        target = current.date() + timedelta(days=1)
        if await self._state.get(LAST_SENT_KEY) == target.isoformat():
            return 0
        try:
            sent = await self.dispatch(target)
        except MilestoneScheduleUnavailableError:
            # 不記狀態 → 下一個 tick 仍在補送視窗內就會重試
            log.warning("時程表無法取得，本次預告延後重試")
            return 0
        await self._state.set(LAST_SENT_KEY, target.isoformat())
        log.info("里程碑預告（%s）已送出 %d 個群組", target.isoformat(), sent)
        return sent

    async def run(self, stop: asyncio.Event, ready: asyncio.Event | None = None) -> None:
        """常駐迴圈；stop 被設定即結束。ready 用於等待 Telegram 連線就緒。"""
        if ready is not None:
            waiter = asyncio.create_task(ready.wait())
            stopper = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (waiter, stopper):
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
        log.info("里程碑預告排程啟動：每天 %02d:%02d 送出隔天事項", self._hour, self._minute)
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # 排程邊界：任何錯誤都不該讓迴圈死掉
                log.exception("里程碑預告排程發生未預期錯誤")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._tick_seconds)

    # ------------------------------------------------------------------ #
    # 派送與預覽
    # ------------------------------------------------------------------ #
    async def _cards_for(self, target: date) -> list[CardReminder]:
        """以送出當天（target 前一日）為基準的過期卡片；取不到時降級為只送里程碑段。"""
        if self._cards is None:
            return []
        try:
            return await self._cards(target - timedelta(days=1))
        except Exception:
            log.warning("過期卡片取得失敗，本次僅送里程碑段", exc_info=True)
            return []

    async def dispatch(self, target: date) -> int:
        """把 target 日的預告送給所有訂閱群組；回傳成功送出的群組數。"""
        schedule = await self._schedule.get()
        hits = schedule.for_date(target)
        cards = await self._cards_for(target)
        sent = 0
        for sub in await self._subs.list_all():
            selected = select_for_teams(hits, sub.teams, self._always)
            if not selected and not cards and not self._send_when_empty:
                continue  # 沒有該群關心的事項、也沒有過期卡片，就不吵人
            try:
                ok = await self._sender(sub.chat_id, sub.thread_id, render_digest(target, selected, cards=cards))
            except Exception:
                log.warning("預告送出失敗 chat_id=%s", sub.chat_id, exc_info=True)
                continue
            if ok:
                sent += 1
        return sent

    async def render_for(self, sub: Subscription | None, target: date, *, when_label: str = "明天") -> str:
        """組出某個訂閱設定所對應的預告內容（供 /notify_test 預覽；不影響排程狀態）。"""
        schedule = await self._schedule.get()
        hits = schedule.for_date(target)
        teams = sub.teams if sub is not None else ()
        cards = await self._cards_for(target)
        return render_digest(target, select_for_teams(hits, teams, self._always), cards=cards, when_label=when_label)
