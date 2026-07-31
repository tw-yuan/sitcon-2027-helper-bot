"""過期卡片提醒（NT-11）——收集開啟中且已到期的 GitLab 卡片，並把 assignee 換成 Telegram tag。

tag 對應鏈（roster 以 gitlab_id 對應）：
    telegram_username → @username（真 mention，會跳通知）
    telegram_id       → tg://user?id= 點擊式 mention（顯示暱稱，同樣會通知）
    查無對應          → 顯示名稱＋「無 TG 對應」（通知不到，但群裡看得出是誰）

名冊拿不到時退化為全部「無 TG 對應」——卡片照樣提醒，不因名冊掛掉整段消失；
GitLab 拿不到則往外拋，由排程端決定降級（只送里程碑段）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from ..services.gitlab_client import Assignee, GitLabClient
from ..services.sheets_roster import Member, RosterService
from ..telegram.formatting import escape_html

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CardReminder:
    """digest 可直接排版的一張過期卡；mentions 為 HTML-ready 片段（空＝未指派）。"""

    iid: int
    url: str  # 可能為空字串（API 未回 web_url 時 digest 退化為純文字 #iid）
    title: str
    due: date
    mentions: tuple[str, ...]


def mention_html(a: Assignee, member: Member | None) -> str:
    if member is not None and member.telegram_username:
        return f"@{escape_html(member.telegram_username)}"
    if member is not None and member.telegram_id is not None:
        display = member.nickname or a.name or a.username or str(member.telegram_id)
        return f'<a href="tg://user?id={member.telegram_id}">{escape_html(display)}</a>'
    display = (member.nickname if member else None) or a.name or a.username or f"gitlab#{a.id}"
    return f"{escape_html(display)}（無 TG 對應）"


async def collect_overdue_cards(gitlab: GitLabClient, roster: RosterService | None, cutoff: date) -> list[CardReminder]:
    """開啟中且 due_date ≤ cutoff 的卡片，過期最久在前。"""
    issues = await gitlab.overdue_issues(cutoff)
    if not issues:
        return []
    by_gitlab_id: dict[int, Member] = {}
    if roster is not None:
        try:
            by_gitlab_id = {m.gitlab_id: m for m in (await roster.get()).members}
        except Exception:
            log.warning("卡片提醒：名冊無法取得，assignee 一律以「無 TG 對應」顯示", exc_info=True)
    return [
        CardReminder(
            iid=i.iid,
            url=i.web_url,
            title=i.title,
            due=date.fromisoformat(i.due_date),  # overdue_issues 保證非空
            mentions=tuple(mention_html(a, by_gitlab_id.get(a.id)) for a in i.assignees),
        )
        for i in issues
    ]
