"""到期卡片提醒（NT-11，2026-08-06 二次修訂）——收集「開著」（GL-22：opened 且 native status ≠ Review）
且到期日臨近的 GitLab 卡片：以預告目標日 target（＝隔天）為準，列出 due ∈ [target−2, target]
（隔天到期、當天到期、過期一天內）；過期超過一天、未填到期日者皆不提醒。
並把 assignee 換成 Telegram tag、從 Team:: label 取出組名供 digest 分組。

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
from datetime import date, timedelta

from ..services.gitlab_client import Assignee, GitLabClient
from ..services.sheets_roster import Member, RosterService
from ..telegram.formatting import escape_html

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CardReminder:
    """digest 可直接排版的一張到期卡；mentions 為 HTML-ready 片段（空＝未指派）。"""

    iid: int
    url: str  # 可能為空字串（API 未回 web_url 時 digest 退化為純文字 #iid）
    title: str
    team: str  # Team:: label 去前綴後的組名；空字串＝卡片沒掛組別（digest 歸入「未分組」）
    due: date  # 必有到期日——未填到期日的卡在收集階段就被排除（NT-11 二次修訂）
    mentions: tuple[str, ...]


def _team_of(labels: list[str]) -> str:
    return next((label.removeprefix("Team::") for label in labels if label.startswith("Team::")), "")


def mention_html(a: Assignee, member: Member | None) -> str:
    if member is not None and member.telegram_username:
        return f"@{escape_html(member.telegram_username)}"
    if member is not None and member.telegram_id is not None:
        display = member.nickname or a.name or a.username or str(member.telegram_id)
        return f'<a href="tg://user?id={member.telegram_id}">{escape_html(display)}</a>'
    display = (member.nickname if member else None) or a.name or a.username or f"gitlab#{a.id}"
    return f"{escape_html(display)}（無 TG 對應）"


async def collect_due_cards(gitlab: GitLabClient, roster: RosterService | None, target: date) -> list[CardReminder]:
    """開著（GL-22）且 due ∈ [target−2, target] 的卡片（target＝預告目標日，即隔天）：
    隔天到期、當天到期、過期一天內；過期超過一天與未填到期日不列。過期最久在前。"""
    dated = [
        (i, due)
        for i in await gitlab.open_cards()
        if i.due_date and target - timedelta(days=2) <= (due := date.fromisoformat(i.due_date)) <= target
    ]
    if not dated:
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
            team=_team_of(i.labels),
            due=due,
            mentions=tuple(mention_html(a, by_gitlab_id.get(a.id)) for a in i.assignees),
        )
        for i, due in dated
    ]
