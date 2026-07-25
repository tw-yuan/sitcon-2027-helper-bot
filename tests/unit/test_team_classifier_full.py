"""T9：組別分類/指派解析——12 組完整測試集（≥15 例，含明確、模糊、無法判斷三類）。"""

from __future__ import annotations

import pytest

from sitcon_bot.domain.team_classifier import resolve_team_assignment
from sitcon_bot.services.gitlab_client import LabelIndex
from sitcon_bot.services.sheets_roster import Member, Roster

TEAMS = ["場務", "活動", "總召", "紀錄", "編輯", "行銷", "行政", "製播", "議程", "設計", "財務", "開發"]
FULL_LABELS = [f"Team::{t}組" for t in TEAMS] + ["Status::Inbox"]
IDX = LabelIndex(FULL_LABELS)

# 有組長的組 → 組長 gitlab_id；其餘組無組長 → 落總召
LEADERS = {"開發": 101, "場務": 102, "設計": 103, "議程": 104, "行銷": 105}
CHIEFS = [201, 202]


def _full_roster() -> Roster:
    members = [Member(gitlab_id=gid, nickname=team, role=f"{team}組", position="組長") for team, gid in LEADERS.items()]
    members += [Member(gitlab_id=cid, nickname=f"chief{cid}", position="總召") for cid in CHIEFS]
    return Roster(members)


@pytest.mark.parametrize("team", TEAMS)
def test_all_twelve_teams_map_to_label(team: str) -> None:
    ta = resolve_team_assignment(team, _full_roster(), IDX)
    assert ta.team_label == f"Team::{team}組"
    if team in LEADERS:  # 明確：有組長
        assert ta.assignee_ids == [LEADERS[team]]
    else:  # 模糊：組可判斷但無組長 → 總召 fallback（RO-4）
        assert sorted(ta.assignee_ids) == sorted(CHIEFS)


def test_undetermined_none() -> None:
    ta = resolve_team_assignment(None, _full_roster(), IDX)  # 無法判斷 → GL-3
    assert ta.team_label == "Team::總召組"
    assert sorted(ta.assignee_ids) == sorted(CHIEFS)


def test_unknown_team() -> None:
    ta = resolve_team_assignment("外太空組", _full_roster(), IDX)
    assert ta.team_label == "Team::總召組"
    assert sorted(ta.assignee_ids) == sorted(CHIEFS)


def test_suffix_variants_match() -> None:
    with_suffix = resolve_team_assignment("開發組", _full_roster(), IDX)
    without = resolve_team_assignment("開發", _full_roster(), IDX)
    assert with_suffix.team_label == without.team_label == "Team::開發組"
    assert with_suffix.assignee_ids == without.assignee_ids == [101]


def test_roster_none_no_assignees() -> None:
    ta = resolve_team_assignment("設計組", None, IDX)
    assert ta.team_label == "Team::設計組"
    assert ta.assignee_ids == []
