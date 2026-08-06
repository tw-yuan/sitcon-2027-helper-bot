"""NT-11：到期卡片收集（視窗篩選）與 assignee → Telegram tag 對應鏈。"""

from __future__ import annotations

from datetime import date

from sitcon_bot.notify.cards import collect_due_cards, mention_html
from sitcon_bot.services.gitlab_client import Assignee, Issue
from sitcon_bot.services.sheets_roster import Member, Roster

# 預告目標日（隔天）；提醒視窗為 [TARGET−2, TARGET]（隔天／當天到期、過期一天內）。
TARGET = date(2026, 7, 26)


def _issue(
    iid: int,
    due: str | None,
    assignees: list[Assignee] | None = None,
    title: str = "卡",
    labels: list[str] | None = None,
) -> Issue:
    return Issue(
        iid=iid,
        web_url=f"https://gitlab.com/sitcon-tw/2027/-/issues/{iid}",
        title=title,
        description=None,
        labels=labels or [],
        assignees=assignees or [],
        due_date=due,
        state="opened",
    )


class _FakeGitLab:
    def __init__(self, issues: list[Issue], fail: bool = False) -> None:
        self._issues = issues
        self._fail = fail
        self.calls = 0

    async def open_cards(self) -> list[Issue]:
        if self._fail:
            raise RuntimeError("gitlab down")
        self.calls += 1
        return self._issues


class _FakeRoster:
    def __init__(self, members: list[Member] | None = None, fail: bool = False) -> None:
        self._members = members or []
        self._fail = fail

    async def get(self, *, force: bool = False) -> Roster:
        if self._fail:
            raise RuntimeError("sheets down")
        return Roster(self._members)


# ------------------------------------------------------------------ #
# mention 對應鏈
# ------------------------------------------------------------------ #
def test_mention_prefers_telegram_username() -> None:
    m = Member(gitlab_id=1, telegram_username="alice", telegram_id=999, nickname="愛麗絲")
    assert mention_html(Assignee(id=1, username="al", name="Alice"), m) == "@alice"


def test_mention_falls_back_to_clickable_telegram_id() -> None:
    m = Member(gitlab_id=1, telegram_id=999, nickname="愛麗絲")
    out = mention_html(Assignee(id=1, username="al", name="Alice"), m)
    assert out == '<a href="tg://user?id=999">愛麗絲</a>'


def test_mention_member_without_tg_uses_nickname_with_note() -> None:
    m = Member(gitlab_id=1, nickname="小明")
    assert mention_html(Assignee(id=1, username="ming", name="Wang Ming"), m) == "小明（無 TG 對應）"


def test_mention_unknown_member_uses_gitlab_display_name() -> None:
    assert mention_html(Assignee(id=7, username="ming", name="王小明"), None) == "王小明（無 TG 對應）"
    assert mention_html(Assignee(id=7, username="ming"), None) == "ming（無 TG 對應）"


def test_mention_display_name_is_escaped() -> None:
    out = mention_html(Assignee(id=7, name="<b>x</b>"), None)
    assert out == "&lt;b&gt;x&lt;/b&gt;（無 TG 對應）"


# ------------------------------------------------------------------ #
# 收集
# ------------------------------------------------------------------ #
async def test_collect_maps_assignees_and_parses_due() -> None:
    gitlab = _FakeGitLab(
        [
            _issue(
                117,
                "2026-07-25",
                [Assignee(id=1), Assignee(id=2)],
                title="贊助簡報",
                labels=["Team::行政組", "Status::Doing"],
            )
        ]
    )
    roster = _FakeRoster(
        [
            Member(gitlab_id=1, telegram_username="alice"),
            Member(gitlab_id=2, telegram_id=55, nickname="鮑伯"),
        ]
    )
    cards = await collect_due_cards(gitlab, roster, TARGET)
    assert gitlab.calls == 1
    (card,) = cards
    assert card.iid == 117 and card.title == "贊助簡報"
    assert card.team == "行政組"
    assert card.due == date(2026, 7, 25)
    assert card.url.endswith("/issues/117")
    assert card.mentions == ("@alice", '<a href="tg://user?id=55">鮑伯</a>')


async def test_collect_keeps_only_due_window() -> None:
    """2026-08-06 二次修訂：只留隔天到期、當天到期、過期一天內；再早再晚都不提醒。"""
    gitlab = _FakeGitLab(
        [
            _issue(1, "2026-07-23"),  # 過期兩天 → 不列
            _issue(2, "2026-07-24"),  # 過期一天 → 列
            _issue(3, "2026-07-25"),  # 當天到期 → 列
            _issue(4, "2026-07-26"),  # 隔天到期 → 列
            _issue(5, "2026-07-27"),  # 後天才到期 → 不列
        ]
    )
    cards = await collect_due_cards(gitlab, None, TARGET)
    assert [c.iid for c in cards] == [2, 3, 4]


async def test_collect_card_without_due_date_is_excluded() -> None:
    """2026-08-06 二次修訂：未填到期日的卡不提醒。"""
    gitlab = _FakeGitLab([_issue(94, None, title="申請摩茲工寮臨時 keyholder", labels=["Status::Waiting"])])
    assert await collect_due_cards(gitlab, None, TARGET) == []


async def test_collect_without_team_label_has_empty_team() -> None:
    gitlab = _FakeGitLab([_issue(1, "2026-07-25", labels=["Status::Inbox"])])
    (card,) = await collect_due_cards(gitlab, None, TARGET)
    assert card.team == ""


async def test_collect_without_roster_marks_all_unmapped() -> None:
    gitlab = _FakeGitLab([_issue(1, "2026-07-25", [Assignee(id=9, name="某人")])])
    (card,) = await collect_due_cards(gitlab, None, TARGET)
    assert card.mentions == ("某人（無 TG 對應）",)


async def test_collect_degrades_when_roster_unavailable() -> None:
    """名冊掛掉不擋卡片提醒，只是 tag 退化為「無 TG 對應」。"""
    gitlab = _FakeGitLab([_issue(1, "2026-07-25", [Assignee(id=9, username="u9")])])
    (card,) = await collect_due_cards(gitlab, _FakeRoster(fail=True), TARGET)
    assert card.mentions == ("u9（無 TG 對應）",)


async def test_collect_empty_issue_list_skips_roster() -> None:
    assert await collect_due_cards(_FakeGitLab([]), _FakeRoster(fail=True), TARGET) == []


async def test_collect_all_out_of_window_skips_roster() -> None:
    """全數落在視窗外時視同無卡，不去打名冊。"""
    gitlab = _FakeGitLab([_issue(1, "2026-07-01"), _issue(2, None)])
    assert await collect_due_cards(gitlab, _FakeRoster(fail=True), TARGET) == []
