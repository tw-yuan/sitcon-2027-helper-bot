"""NT-4／NT-6～NT-11：訂閱儲存、每日排程（到點、去重、補送）、過期卡片、管理指令。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sitcon_bot.auth.groups import GroupStore
from sitcon_bot.notify.cards import CardReminder
from sitcon_bot.notify.scheduler import CardsProvider, MilestoneNotifier
from sitcon_bot.notify.subscriptions import LAST_SENT_KEY, NotifyStateStore, SubscriptionStore
from sitcon_bot.services.milestone_schedule import MilestoneSchedule, MilestoneScheduleService, parse_milestones
from sitcon_bot.settings import Settings
from sitcon_bot.storage.db import Database
from sitcon_bot.telegram.commands import CommandHandlers, MilestoneDeps

TPE = ZoneInfo("Asia/Taipei")
ADMIN = 42
CHAT = -1001

HEADER = ["事件名稱", "開始時間", "結束時間", "主導組別", "備註"]
ROWS = [
    ["二籌", "2026/09/12", "2026/09/12", "全體", ""],
    ["開發組零籌", "2026/09/05", "2026/09/12", "開發組", "喬時間"],
    ["2026 年份單據報帳截止", "2026/12/17", "2026/12/17", "財務組", ""],
]


class _Fetcher:
    def __init__(self) -> None:
        self.fail = False

    async def fetch(self) -> tuple[list[str], list[list[str]]]:
        if self.fail:
            raise RuntimeError("sheets down")
        return HEADER, ROWS


def _schedule_service(fetcher: _Fetcher | None = None) -> MilestoneScheduleService:
    return MilestoneScheduleService(fetcher or _Fetcher(), ttl_seconds=600, clock=lambda: 0.0)


class _Recorder:
    """記錄送出的訊息（chat_id, thread_id, text）。"""

    def __init__(self, ok: bool = True) -> None:
        self.sent: list[tuple[int, int | None, str]] = []
        self.ok = ok

    async def __call__(self, chat_id: int, thread_id: int | None, text: str) -> bool:
        self.sent.append((chat_id, thread_id, text))
        return self.ok


def _notifier(
    db: Database,
    sender: _Recorder,
    schedule: MilestoneScheduleService | None = None,
    cards: CardsProvider | None = None,
) -> MilestoneNotifier:
    return MilestoneNotifier(
        schedule=schedule or _schedule_service(),
        subscriptions=SubscriptionStore(db),
        state=NotifyStateStore(db),
        sender=sender,
        cards=cards,
        tz="Asia/Taipei",
        hour=20,
        minute=0,
        catchup_minutes=180,
        always_teams=("全體", "重要日期"),
    )


def _at(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=TPE)


# ------------------------------------------------------------------ #
# 訂閱儲存
# ------------------------------------------------------------------ #
async def test_subscribe_update_and_unsubscribe(db: Database) -> None:
    store = SubscriptionStore(db)
    assert await store.get(CHAT) is None

    sub = await store.subscribe(CHAT, "開發群", ["開發組", "行政組"], ADMIN, thread_id=7)
    assert sub.teams == ("開發組", "行政組")
    assert sub.all_teams is False
    assert (await store.get(CHAT)).thread_id == 7

    await store.subscribe(CHAT, "開發群", [], ADMIN)  # 改訂全部
    again = await store.get(CHAT)
    assert again.all_teams is True
    assert len(await store.list_all()) == 1  # 同群只有一列

    assert await store.unsubscribe(CHAT) is True
    assert await store.unsubscribe(CHAT) is False
    assert await store.list_all() == []


async def test_subscription_survives_new_store_instance(db: Database) -> None:
    await SubscriptionStore(db).subscribe(CHAT, "群", ["財務組"], ADMIN)
    fresh = await SubscriptionStore(db).list_all()
    assert [s.teams for s in fresh] == [("財務組",)]


async def test_notify_state_roundtrip(db: Database) -> None:
    state = NotifyStateStore(db)
    assert await state.get(LAST_SENT_KEY) is None
    await state.set(LAST_SENT_KEY, "2026-09-12")
    await state.set(LAST_SENT_KEY, "2026-09-13")
    assert await state.get(LAST_SENT_KEY) == "2026-09-13"


# ------------------------------------------------------------------ #
# 排程：到點、去重、補送
# ------------------------------------------------------------------ #
async def test_tick_before_due_sends_nothing(db: Database) -> None:
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN)
    assert await _notifier(db, sender).tick(_at(2026, 9, 11, 19, 59)) == 0
    assert sender.sent == []


async def test_tick_at_due_sends_tomorrow(db: Database) -> None:
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN, thread_id=3)
    sent = await _notifier(db, sender).tick(_at(2026, 9, 11, 20, 0))
    assert sent == 1
    chat_id, thread_id, text = sender.sent[0]
    assert (chat_id, thread_id) == (CHAT, 3)
    assert "2026/09/12" in text and "二籌" in text and "開發組零籌" in text


async def test_tick_is_idempotent_for_the_day(db: Database) -> None:
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN)
    notifier = _notifier(db, sender)
    assert await notifier.tick(_at(2026, 9, 11, 20, 0)) == 1
    assert await notifier.tick(_at(2026, 9, 11, 20, 1)) == 0
    assert len(sender.sent) == 1


async def test_restart_does_not_resend(db: Database) -> None:
    """狀態存在 DB → 重啟後（新的 notifier 實例）不會重送同一天的預告。"""
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN)
    await _notifier(db, sender).tick(_at(2026, 9, 11, 20, 0))
    assert await _notifier(db, sender).tick(_at(2026, 9, 11, 21, 30)) == 0


async def test_catch_up_within_window(db: Database) -> None:
    """20:00 錯過（bot 重啟中），22:30 啟動仍補送。"""
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN)
    assert await _notifier(db, sender).tick(_at(2026, 9, 11, 22, 30)) == 1


async def test_no_catch_up_after_window(db: Database) -> None:
    """超過補送視窗就跳過，不會半夜才吵人。"""
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN)
    assert await _notifier(db, sender).tick(_at(2026, 9, 11, 23, 30)) == 0
    assert sender.sent == []


async def test_next_day_sends_again(db: Database) -> None:
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN)
    notifier = _notifier(db, sender)
    await notifier.tick(_at(2026, 9, 11, 20, 0))
    await notifier.tick(_at(2026, 12, 16, 20, 0))
    assert len(sender.sent) == 2
    assert "報帳截止" in sender.sent[1][2]


async def test_empty_day_sends_nothing_but_marks_done(db: Database) -> None:
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN)
    notifier = _notifier(db, sender)
    assert await notifier.tick(_at(2026, 9, 20, 20, 0)) == 0
    assert sender.sent == []
    assert await NotifyStateStore(db).get(LAST_SENT_KEY) == "2026-09-21"


async def test_sheet_failure_retries_next_tick(db: Database) -> None:
    """讀不到時程表時不記狀態，補送視窗內的下一個 tick 會重試。"""
    sender = _Recorder()
    fetcher = _Fetcher()
    fetcher.fail = True
    notifier = _notifier(db, sender, _schedule_service(fetcher))
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN)

    assert await notifier.tick(_at(2026, 9, 11, 20, 0)) == 0
    assert await NotifyStateStore(db).get(LAST_SENT_KEY) is None
    fetcher.fail = False
    assert await notifier.tick(_at(2026, 9, 11, 20, 1)) == 1


async def test_team_filter_per_group(db: Database) -> None:
    sender = _Recorder()
    subs = SubscriptionStore(db)
    await subs.subscribe(-1, "財務群", ["財務組"], ADMIN)
    await subs.subscribe(-2, "開發群", ["開發組"], ADMIN)
    await subs.subscribe(-3, "大群", [], ADMIN)

    assert await _notifier(db, sender).tick(_at(2026, 9, 11, 20, 0)) == 3
    by_chat = {c: t for c, _, t in sender.sent}
    # 財務群只收到「全體」的二籌，收不到開發組的事項
    assert "二籌" in by_chat[-1] and "開發組零籌" not in by_chat[-1]
    assert "開發組零籌" in by_chat[-2] and "二籌" in by_chat[-2]
    assert "開發組零籌" in by_chat[-3]


async def test_group_with_nothing_relevant_is_not_notified(db: Database) -> None:
    """只訂閱開發組的群，在只有財務組事項的日子不會被打擾（NT-8）。"""
    sender = _Recorder()
    subs = SubscriptionStore(db)
    await subs.subscribe(-2, "開發群", ["開發組"], ADMIN)
    await subs.subscribe(-4, "財務群", ["財務組"], ADMIN)

    assert await _notifier(db, sender).tick(_at(2026, 12, 16, 20, 0)) == 1
    assert [c for c, _, _ in sender.sent] == [-4]


async def test_send_failure_does_not_block_other_groups(db: Database) -> None:
    calls: list[int] = []

    async def flaky(chat_id: int, thread_id: int | None, text: str) -> bool:
        calls.append(chat_id)
        if chat_id == -1:
            raise RuntimeError("bot 已被踢出群組")
        return True

    subs = SubscriptionStore(db)
    await subs.subscribe(-1, "壞群", [], ADMIN)
    await subs.subscribe(-2, "好群", [], ADMIN)
    notifier = MilestoneNotifier(
        schedule=_schedule_service(),
        subscriptions=subs,
        state=NotifyStateStore(db),
        sender=flaky,
        hour=20,
    )
    assert await notifier.tick(_at(2026, 9, 11, 20, 0)) == 1
    assert calls == [-1, -2]


async def test_dispatch_when_no_subscriptions(db: Database) -> None:
    sender = _Recorder()
    assert await _notifier(db, sender).dispatch(date(2026, 9, 12)) == 0


# ------------------------------------------------------------------ #
# NT-11：過期卡片提醒
# ------------------------------------------------------------------ #
def _card(iid: int = 117) -> CardReminder:
    return CardReminder(iid=iid, url="", title=f"卡{iid}", team="行政組", due=date(2026, 9, 10), mentions=("@alice",))


async def test_dispatch_appends_overdue_cards(db: Database) -> None:
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN)
    seen: list[date] = []

    async def cards(cutoff: date) -> list[CardReminder]:
        seen.append(cutoff)
        return [_card()]

    assert await _notifier(db, sender, cards=cards).tick(_at(2026, 9, 11, 20, 0)) == 1
    assert seen == [date(2026, 9, 11)]  # 基準為送出當天（target 9/12 的前一日），且整輪只抓一次
    text = sender.sent[0][2]
    assert "卡片提醒" in text and "#117" in text and "@alice" in text
    assert "二籌" in text  # 里程碑段照舊


async def test_cards_fetched_once_for_all_groups(db: Database) -> None:
    subs = SubscriptionStore(db)
    await subs.subscribe(-1, "群一", [], ADMIN)
    await subs.subscribe(-2, "群二", [], ADMIN)
    calls: list[date] = []

    async def cards(cutoff: date) -> list[CardReminder]:
        calls.append(cutoff)
        return [_card()]

    sender = _Recorder()
    assert await _notifier(db, sender, cards=cards).tick(_at(2026, 9, 11, 20, 0)) == 2
    assert len(calls) == 1
    assert all("卡片提醒" in t for _, _, t in sender.sent)  # 卡片不分組別，兩群都收到


async def test_group_without_milestones_still_gets_cards(db: Database) -> None:
    """只訂開發組的群在只有財務事項的日子，仍會收到過期卡片（訊息只有卡片段）。"""
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(-2, "開發群", ["開發組"], ADMIN)

    async def cards(cutoff: date) -> list[CardReminder]:
        return [_card()]

    assert await _notifier(db, sender, cards=cards).tick(_at(2026, 12, 16, 20, 0)) == 1
    text = sender.sent[0][2]
    assert "卡片提醒" in text and "里程碑" not in text


async def test_no_cards_and_no_milestones_stays_silent(db: Database) -> None:
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN)

    async def cards(cutoff: date) -> list[CardReminder]:
        return []

    assert await _notifier(db, sender, cards=cards).tick(_at(2026, 9, 20, 20, 0)) == 0
    assert sender.sent == []
    assert await NotifyStateStore(db).get(LAST_SENT_KEY) == "2026-09-21"


async def test_cards_failure_degrades_to_milestones_only(db: Database) -> None:
    """GitLab 掛掉不擋里程碑預告；當日卡片段直接略過、不重試整包。"""
    sender = _Recorder()
    await SubscriptionStore(db).subscribe(CHAT, "群", [], ADMIN)

    async def boom(cutoff: date) -> list[CardReminder]:
        raise RuntimeError("gitlab down")

    assert await _notifier(db, sender, cards=boom).tick(_at(2026, 9, 11, 20, 0)) == 1
    text = sender.sent[0][2]
    assert "二籌" in text and "卡片提醒" not in text
    assert await NotifyStateStore(db).get(LAST_SENT_KEY) == "2026-09-12"


async def test_render_for_preview_includes_cards(db: Database) -> None:
    async def cards(cutoff: date) -> list[CardReminder]:
        return [_card()]

    out = await _notifier(db, _Recorder(), cards=cards).render_for(None, date(2026, 9, 12))
    assert "卡片提醒" in out and "#117" in out


# ------------------------------------------------------------------ #
# 管理指令
# ------------------------------------------------------------------ #
def _settings(**over: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token="123:abc",
        telegram_admin_id=ADMIN,
        llm_api_key="k",
        gitlab_token="g",
        hackmd_token="h",
        hackmd_team_path="sitcon",
        **over,
    )


def _handlers(db: Database, *, with_milestones: bool = True) -> tuple[CommandHandlers, _Recorder]:
    sender = _Recorder()
    deps = (
        MilestoneDeps(
            subscriptions=SubscriptionStore(db),
            schedule=_schedule_service(),
            notifier=_notifier(db, sender),
        )
        if with_milestones
        else None
    )
    return CommandHandlers(_settings(), GroupStore(db), milestones=deps), sender


async def test_notify_on_all_teams(db: Database) -> None:
    handlers, _ = _handlers(db)
    out = await handlers.notify_on(CHAT, "開發群", ADMIN, "")
    assert "全部組別" in out
    sub = await SubscriptionStore(db).get(CHAT)
    assert sub is not None and sub.all_teams


@pytest.mark.parametrize("args", ["全部", "all", "  "])
async def test_notify_on_all_keywords(db: Database, args: str) -> None:
    handlers, _ = _handlers(db)
    await handlers.notify_on(CHAT, "群", ADMIN, args)
    assert (await SubscriptionStore(db).get(CHAT)).all_teams


async def test_notify_on_specific_teams_normalizes(db: Database) -> None:
    handlers, _ = _handlers(db)
    out = await handlers.notify_on(CHAT, "群", ADMIN, "開發、財務組 財務組")
    assert "開發組" in out and "財務組" in out
    sub = await SubscriptionStore(db).get(CHAT)
    assert sub.teams == ("開發組", "財務組")  # 正規化為表上寫法並去重


async def test_notify_on_rejects_unknown_team(db: Database) -> None:
    handlers, _ = _handlers(db)
    out = await handlers.notify_on(CHAT, "群", ADMIN, "公關組")
    assert "不認得" in out and "公關組" in out
    assert await SubscriptionStore(db).get(CHAT) is None


async def test_notify_on_records_thread_id(db: Database) -> None:
    handlers, _ = _handlers(db)
    await handlers.notify_on(CHAT, "群", ADMIN, "", thread_id=99)
    assert (await SubscriptionStore(db).get(CHAT)).thread_id == 99


async def test_notify_off_and_list(db: Database) -> None:
    handlers, _ = _handlers(db)
    assert "沒有任何群組" in await handlers.notify_list()
    await handlers.notify_on(CHAT, "開發群", ADMIN, "開發組")
    listing = await handlers.notify_list()
    assert "開發群" in listing and str(CHAT) in listing and "開發組" in listing
    assert "已取消" in await handlers.notify_off(CHAT)
    assert "原本就沒有訂閱" in await handlers.notify_off(CHAT)


async def test_revoke_also_drops_subscription(db: Database) -> None:
    handlers, _ = _handlers(db)
    groups = GroupStore(db)
    await groups.authorize(CHAT, "群", ADMIN)
    handlers = CommandHandlers(
        _settings(),
        groups,
        milestones=MilestoneDeps(
            subscriptions=SubscriptionStore(db), schedule=_schedule_service(), notifier=_notifier(db, _Recorder())
        ),
    )
    await handlers.notify_on(CHAT, "群", ADMIN, "")
    out = await handlers.revoke(CHAT)
    assert "里程碑預告訂閱也已一併取消" in out
    assert await SubscriptionStore(db).get(CHAT) is None


async def test_notify_test_previews_without_marking_sent(db: Database) -> None:
    handlers, _ = _handlers(db)
    await handlers.notify_on(CHAT, "群", ADMIN, "")
    out = await handlers.notify_test(CHAT)
    assert "里程碑" in out
    assert await NotifyStateStore(db).get(LAST_SENT_KEY) is None


async def test_notify_test_hints_when_not_subscribed(db: Database) -> None:
    handlers, _ = _handlers(db)
    assert "尚未訂閱" in await handlers.notify_test(CHAT)


async def test_commands_report_disabled_feature(db: Database) -> None:
    handlers, _ = _handlers(db, with_milestones=False)
    for out in (
        await handlers.notify_on(CHAT, "群", ADMIN, ""),
        await handlers.notify_off(CHAT),
        await handlers.notify_list(),
        await handlers.notify_test(CHAT),
    ):
        assert "未啟用" in out


# ------------------------------------------------------------------ #
# 到點時刻計算
# ------------------------------------------------------------------ #
def test_due_at_uses_configured_time(db: Database) -> None:
    notifier = _notifier(db, _Recorder())
    assert notifier.due_at(_at(2026, 9, 11, 3, 15)) == _at(2026, 9, 11, 20, 0)


def test_schedule_view_used_by_notifier_is_parsed_once() -> None:
    view = MilestoneSchedule(parse_milestones(HEADER, ROWS))
    assert len(view) == 3
    assert view.for_date(date(2026, 9, 12) - timedelta(days=7))[0].milestone.name == "開發組零籌"
