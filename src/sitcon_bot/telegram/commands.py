"""管理與說明指令的處理（AUTH-2/3/6/7/8）。

各方法回傳可直接送出的 HTML（gateway 以 parse_mode=HTML 分段送出，不再整段 escape）。
靜態模板可用 <b> 等標籤；動態內容（群組名等）在此以 escape_html 處理，避免破壞 HTML 或注入。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from ..auth.groups import GroupStore
from ..notify.scheduler import MilestoneNotifier
from ..notify.subscriptions import SubscriptionStore
from ..services.milestone_schedule import (
    MilestoneScheduleService,
    MilestoneScheduleUnavailableError,
    norm_team,
)
from ..settings import Settings
from ..storage.memories import GroupMemoryStore
from .formatting import escape_html

log = logging.getLogger(__name__)

# /reload 的重載回呼：回傳一段人話摘要（各項筆數）。T3 尚未接快取，預設 None。
ReloadCallback = Callable[[], Awaitable[str]]

PRIVATE_NOTICE = "小石僅在授權群組內提供服務。"
PRIVATE_AUTHORIZE_REDIRECT = "請在目標群組內執行 /authorize（管理員限定）。"

HELP_TEXT = """<b>小石</b> — SITCON 2027 工作人員助理

在授權群組裡，用一句話就能操作 GitLab 卡片、找雲端硬碟檔案、開/找/改 HackMD 筆記。
觸發方式：@提及我、reply 我的訊息、或用「小石」開頭。

<b>GitLab 卡片</b>
• 小石 幫我開一張卡：官網倒數計時器壞了
• 小石 開一張卡給行政組，標題場地保證金匯款，due 8/15
• 小石 把 #42 改成 Doing，加上 0913 一籌
• 小石 在 #42 留言：場地已確認
• 小石 列出行政組還開著的卡

<b>找文件（雲端硬碟＋HackMD 一起找，唯讀）</b>
• 小石 幫我找去年的場地租借合約
• 小石 只找雲端硬碟裡的贊助合約
（雲端硬碟只會給你檔名、路徑與連結，內容請點連結自己看）

<b>照片搜尋（歷年 Flickr 活動照）</b>
• 小石 找幾張講者演講的橫式照片
• 小石 幫我找 Camp 2026 工作坊、有人物的照片

<b>HackMD 筆記</b>
• 小石 開一份 0913 一籌的會議記錄
• 小石 幫行政組開今天的會議記錄
• 小石 找上次討論贊助方案的那份文件

<b>群組記憶</b>
每個群組可以讓小石長期記住偏好、慣例或常用資訊，之後做事時會自動遵守（重啟不遺失）。
• 小石 記住：這群的卡預設都指派給 Yuan
• 小石 你目前記得哪些事？
• 小石 忘掉第 2 條記憶

<b>里程碑預告（管理員設定）</b>
每天晚上 23:00 自動預告隔天的籌備時程里程碑，並附上到期在即（隔天到期～過期一天內）的 GitLab 卡片提醒（tag assignee）。
• /notify_on — 本群訂閱「全部組別」
• /notify_on 開發組, 行政組 — 只收這幾組（＋全體、重要日期）
• /notify_off — 本群取消訂閱
• /notify_list — 列出所有訂閱群組
• /notify_test — 立刻預覽本群明天會收到的內容

管理指令（管理員）：/authorize /revoke /list_groups /reload /notify_on /notify_off /notify_list /notify_test"""

START_TEXT = "我是小石，SITCON 2027 的工作人員助理。輸入 /help 看我能做什麼。"

# /notify_on 參數的組別分隔字元（中英標點與空白皆可）
_TEAM_SPLIT_RE = re.compile(r"[,，、/\s]+")
# 代表「全部組別」的參數寫法
_ALL_KEYWORDS = frozenset({"全部", "全部組別", "所有", "所有組別", "all", "*"})

MILESTONE_DISABLED = "里程碑預告功能未啟用（MILESTONE_NOTIFY_ENABLED=false）。"
MILESTONE_UNAVAILABLE = "目前讀不到籌備時程表，請稍後再試；若只是要訂閱全部組別，可直接用 /notify_on。"


@dataclass(slots=True)
class MilestoneDeps:
    """里程碑預告指令所需的協作物件（未啟用時為 None）。"""

    subscriptions: SubscriptionStore
    schedule: MilestoneScheduleService
    notifier: MilestoneNotifier


class CommandHandlers:
    """處理管理與說明指令。"""

    def __init__(
        self,
        settings: Settings,
        groups: GroupStore,
        reload_cb: ReloadCallback | None = None,
        milestones: MilestoneDeps | None = None,
        memories: GroupMemoryStore | None = None,
    ) -> None:
        self._settings = settings
        self._groups = groups
        self._reload_cb = reload_cb
        self._milestones = milestones
        self._memories = memories

    async def authorize(self, chat_id: int, title: str | None) -> str:
        newly = await self._groups.authorize(chat_id, title, self._settings.telegram_admin_id)
        if newly:
            name = escape_html(title) if title else "(未命名群組)"
            return f"✅ 已授權此群組「{name}」（chat_id={chat_id}）。群組成員現在可以使用小石。"
        return "此群組已授權。"

    async def revoke(self, chat_id: int) -> str:
        existed = await self._groups.revoke(chat_id)
        # 未授權群組不該再收到任何主動訊息 → 一併清掉里程碑預告訂閱；群組記憶也一併清空
        unsubscribed = False
        if self._milestones is not None:
            unsubscribed = await self._milestones.subscriptions.unsubscribe(chat_id)
        cleared = 0
        if self._memories is not None:
            cleared = await self._memories.clear(chat_id)
        if existed:
            tails = []
            if unsubscribed:
                tails.append("里程碑預告訂閱")
            if cleared:
                tails.append(f"群組記憶 {cleared} 筆")
            tail = f"（{'、'.join(tails)}也已一併清除）" if tails else ""
            return f"已撤銷此群組的授權，小石在此群組將停止服務。{tail}"
        return "此群組原本就未授權。"

    async def list_groups(self) -> str:
        groups = await self._groups.list_groups()
        if not groups:
            return "目前沒有授權任何群組。"
        lines = [f"目前授權群組（{len(groups)}）："]
        lines += [f"• {escape_html(g.title) if g.title else '(未命名)'}（{g.chat_id}）" for g in groups]
        return "\n".join(lines)

    async def reload(self) -> str:
        if self._reload_cb is None:
            return "已重載（目前尚無可重載的快取）。"
        summary = await self._reload_cb()
        return f"已重載：{summary}"

    # ------------------------------------------------------------------ #
    # 里程碑預告（NT-4／NT-9）
    # ------------------------------------------------------------------ #
    async def notify_on(
        self, chat_id: int, title: str | None, admin_id: int, args: str, thread_id: int | None = None
    ) -> str:
        """訂閱本群的里程碑預告。args 空或「全部」＝所有組別，否則為組別清單。"""
        if self._milestones is None:
            return MILESTONE_DISABLED
        raw = [t for t in _TEAM_SPLIT_RE.split(args.strip()) if t]
        wants_all = not raw or all(t.lower() in _ALL_KEYWORDS for t in raw)

        teams: list[str] = []
        if not wants_all:
            try:
                schedule = await self._milestones.schedule.get()
            except MilestoneScheduleUnavailableError:
                return MILESTONE_UNAVAILABLE
            known = {norm_team(t): t for t in schedule.teams()}
            unknown = [t for t in raw if norm_team(t) not in known]
            if unknown:
                return (
                    f"❌ 不認得這些組別：{escape_html('、'.join(unknown))}\n"
                    f"時程表目前的主導組別：{escape_html('、'.join(schedule.teams()))}"
                )
            teams = list(dict.fromkeys(known[norm_team(t)] for t in raw))  # 正規化為表上的寫法並去重

        await self._milestones.subscriptions.subscribe(chat_id, title, teams, admin_id, thread_id)
        scope = "全部組別" if not teams else escape_html("、".join(teams))
        extra = "" if not teams else f"（另含 {escape_html('、'.join(self._settings.milestone_always_team_list))}）"
        return (
            f"✅ 已設定本群接收里程碑預告：{scope}{extra}\n"
            f"每天 {self._settings.milestone_notify_hour:02d}:"
            f"{self._settings.milestone_notify_minute:02d} 預告隔天事項；可用 /notify_test 先看看內容。"
        )

    async def notify_off(self, chat_id: int) -> str:
        if self._milestones is None:
            return MILESTONE_DISABLED
        existed = await self._milestones.subscriptions.unsubscribe(chat_id)
        return "已取消本群的里程碑預告。" if existed else "本群原本就沒有訂閱里程碑預告。"

    async def notify_list(self) -> str:
        if self._milestones is None:
            return MILESTONE_DISABLED
        subs = await self._milestones.subscriptions.list_all()
        if not subs:
            return "目前沒有任何群組訂閱里程碑預告。"
        lines = [f"里程碑預告訂閱（{len(subs)}）："]
        for s in subs:
            name = escape_html(s.title) if s.title else "(未命名)"
            scope = "全部組別" if s.all_teams else escape_html("、".join(s.teams))
            lines.append(f"• {name}（{s.chat_id}）→ {scope}")
        lines.append(
            f"每天 {self._settings.milestone_notify_hour:02d}:"
            f"{self._settings.milestone_notify_minute:02d} 送出隔天事項。"
        )
        return "\n".join(lines)

    async def notify_test(self, chat_id: int) -> str:
        """預覽本群明天會收到的內容（不影響排程狀態，也不會標記為已送出）。"""
        if self._milestones is None:
            return MILESTONE_DISABLED
        sub = await self._milestones.subscriptions.get(chat_id)
        target = self._milestones.notifier.now().date() + timedelta(days=1)
        try:
            body = await self._milestones.notifier.render_for(sub, target)
        except MilestoneScheduleUnavailableError:
            return MILESTONE_UNAVAILABLE
        if sub is None:
            body += "\n\n（本群尚未訂閱，以上為「全部組別」的預覽；要訂閱請用 /notify_on）"
        return body

    def help_text(self) -> str:
        return HELP_TEXT

    def start_text(self) -> str:
        return START_TEXT

    def private_notice(self) -> str:
        return PRIVATE_NOTICE

    def private_authorize_redirect(self) -> str:
        return PRIVATE_AUTHORIZE_REDIRECT
