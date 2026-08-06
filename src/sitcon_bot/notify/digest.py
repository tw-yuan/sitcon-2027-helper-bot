"""每日提醒訊息組裝（NT-5／NT-11）——純函式，不碰 I/O，最易測。

輸出為可直接送出的 HTML（gateway 以 parse_mode=HTML 送出）：靜態骨架用 <b>，
所有外部內容（事件名、組別、卡片標題、人名）一律 escape_html 後才嵌入。

兩個段落：
    📅 里程碑——隔天事項，一行一筆 `[組別] 標題`
    ⚠️ 卡片提醒——所有開著（GL-22：不含 Status::Review）的 GitLab 卡片，附 assignee tag
      （cards.py 先組好）；依 Team:: 組別分組、組內依到期日排序（未填到期日殿後），
      沒掛組別的歸「未分組」殿後
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from ..services.milestone_schedule import MilestoneHit
from ..telegram.formatting import escape_html
from .cards import CardReminder

WEEKDAY_ZH = ("一", "二", "三", "四", "五", "六", "日")

# 卡片段落最多列出的張數；再多就收成一行總數。逾長訊息 gateway 會分段送出（TRIG-8），
# 這裡的上限只是防止看板爆量時把群組洗版。
CARDS_MAX = 100


def format_date(d: date) -> str:
    """2026/09/12（六）"""
    return f"{d.year}/{d.month:02d}/{d.day:02d}（{WEEKDAY_ZH[d.weekday()]}）"


def _short_date(d: date) -> str:
    return f"{d.month:02d}/{d.day:02d}"


def _milestone_section(target: date, hits: list[MilestoneHit], when_label: str) -> str:
    head = f"📅 <b>{when_label} {escape_html(format_date(target))}的里程碑</b>"
    if not hits:
        return f"{head}\n\n（沒有里程碑事項）"
    lines = [head, ""]
    lines += [f"[{escape_html(h.milestone.team)}] <b>{escape_html(h.milestone.name)}</b>" for h in hits]
    return "\n".join(lines)


def _card_line(card: CardReminder) -> str:
    ref = f'<a href="{card.url}">#{card.iid}</a>' if card.url else f"#{card.iid}"
    when = f"{_short_date(card.due)} 到期" if card.due else "未填到期日"
    who = f"— {' '.join(card.mentions)}" if card.mentions else "—（未指派）"
    return f"• {ref} <b>{escape_html(card.title)}</b>（{when}）{who}"


def _sort_key(c: CardReminder) -> tuple[bool, date, int]:
    # 有到期日者在前（過期最久優先），未填到期日者殿後。
    return (c.due is None, c.due or date.max, c.iid)


def _cards_section(cards: Sequence[CardReminder]) -> str:
    # 先以整體到期順序截到上限（保住過期最久的），再分組排版。
    shown = sorted(cards, key=_sort_key)[:CARDS_MAX]
    groups: dict[str, list[CardReminder]] = {}
    for c in shown:
        groups.setdefault(c.team, []).append(c)
    # 組間依「該組最急的卡」排序（最急的組在前），未分組（team 為空）一律殿後。
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] == "", *_sort_key(kv[1][0])))
    lines = ["⚠️ <b>卡片提醒</b>"]
    for team, group in ordered:
        lines += ["", f"<b>【{escape_html(team) or '未分組'}】</b>"]
        lines += [_card_line(c) for c in group]
    if len(cards) > CARDS_MAX:
        lines.append(f"…另有 {len(cards) - CARDS_MAX} 張")
    return "\n".join(lines)


def render_digest(
    target: date,
    hits: list[MilestoneHit],
    *,
    cards: Sequence[CardReminder] = (),
    when_label: str = "明天",
) -> str:
    """組出每日提醒；兩段都空時回傳「沒有里程碑」的說明（是否送出由呼叫端決定）。"""
    parts: list[str] = []
    if hits or not cards:
        parts.append(_milestone_section(target, hits, when_label))
    if cards:
        parts.append(_cards_section(cards))
    return "\n\n".join(parts)
