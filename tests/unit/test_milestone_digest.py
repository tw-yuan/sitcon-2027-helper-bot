"""NT-5／NT-11：預告訊息組裝（HTML 安全、單行里程碑、卡片段落）。"""

from __future__ import annotations

from datetime import date

from sitcon_bot.notify.cards import CardReminder
from sitcon_bot.notify.digest import CARDS_MAX, format_date, render_digest
from sitcon_bot.services.milestone_schedule import (
    KIND_SINGLE,
    KIND_START,
    Milestone,
    MilestoneHit,
)

D = date(2026, 9, 12)


def _hit(name: str = "二籌", team: str = "全體", kind: str = KIND_SINGLE, note: str = "") -> MilestoneHit:
    return MilestoneHit(Milestone(name, D, date(2026, 9, 20), team, note), kind)


def _card(
    iid: int = 117,
    title: str = "贊助簡報初稿",
    due: date = date(2026, 7, 25),
    mentions: tuple[str, ...] = ("@alice",),
    url: str = "https://gitlab.com/sitcon-tw/2027/-/issues/117",
) -> CardReminder:
    return CardReminder(iid=iid, url=url, title=title, due=due, mentions=mentions)


def test_format_date_has_padded_weekday() -> None:
    assert format_date(date(2026, 9, 12)) == "2026/09/12（六）"


def test_empty_digest_states_nothing_scheduled() -> None:
    out = render_digest(date(2026, 9, 13), [])
    assert "2026/09/13（日）" in out
    assert "沒有里程碑事項" in out


def test_milestone_is_one_line_title_only() -> None:
    """里程碑一行一筆 `[組別] 標題`；不再有期間與備註行。"""
    out = render_digest(D, [_hit(kind=KIND_START, note="喬時間")])
    assert "[全體] <b>二籌</b>" in out
    assert "起跑" not in out and "最後一天" not in out and "📝" not in out


def test_sheet_content_is_html_escaped() -> None:
    """試算表內容是外部資料：<b> 之類必須被 escape，不能破壞 HTML parse mode。"""
    out = render_digest(D, [_hit(name="<b>假粗體</b> & 測試", team="<設計組>")])
    assert "<b>&lt;b&gt;假粗體&lt;/b&gt; &amp; 測試</b>" in out
    assert "[&lt;設計組&gt;]" in out


def test_when_label_is_configurable() -> None:
    assert render_digest(D, [], when_label="今天").startswith("📅 <b>今天 ")


# ------------------------------------------------------------------ #
# 卡片段落（NT-11）
# ------------------------------------------------------------------ #
def test_card_line_has_link_title_due_and_mention() -> None:
    out = render_digest(D, [_hit()], cards=[_card()])
    assert (
        '• <a href="https://gitlab.com/sitcon-tw/2027/-/issues/117">#117</a> <b>贊助簡報初稿</b>（07/25 到期）— @alice'
    ) in out


def test_card_section_after_milestones_with_blank_line() -> None:
    out = render_digest(D, [_hit()], cards=[_card()])
    assert "\n\n⚠️ <b>卡片提醒</b>\n\n" in out
    assert out.index("里程碑") < out.index("卡片提醒")


def test_card_mentions_are_space_joined() -> None:
    out = render_digest(D, [_hit()], cards=[_card(mentions=("@bob", "@carol"))])
    assert "— @bob @carol" in out


def test_card_without_assignee_says_unassigned() -> None:
    out = render_digest(D, [_hit()], cards=[_card(mentions=())])
    assert "—（未指派）" in out


def test_card_without_url_falls_back_to_plain_ref() -> None:
    out = render_digest(D, [_hit()], cards=[_card(url="")])
    assert "• #117 <b>贊助簡報初稿</b>" in out


def test_card_title_is_html_escaped() -> None:
    out = render_digest(D, [_hit()], cards=[_card(title="<script> & co")])
    assert "<b>&lt;script&gt; &amp; co</b>" in out


def test_cards_without_milestones_omits_milestone_section() -> None:
    """隔天沒里程碑但有過期卡：只送卡片段，不出現「沒有里程碑事項」噪音。"""
    out = render_digest(D, [], cards=[_card()])
    assert "里程碑" not in out
    assert out.startswith("⚠️ <b>卡片提醒</b>")


def test_cards_are_capped_with_summary_line() -> None:
    cards = [_card(iid=i, url="") for i in range(1, CARDS_MAX + 6)]
    out = render_digest(D, [], cards=cards)
    assert f"• #{CARDS_MAX} " in out
    assert f"• #{CARDS_MAX + 1} " not in out
    assert "…另有 5 張" in out
