"""T8/T9：組別指派解析（GL-2／GL-3／RO-4）。"""

from __future__ import annotations

from sitcon_bot.domain.team_classifier import resolve_team_assignment
from sitcon_bot.services.gitlab_client import LabelIndex
from sitcon_bot.services.sheets_roster import Member, Roster

LABELS = ["Team::開發組", "Team::行政組", "Team::總召組", "Status::Inbox", "Status::Doing"]
IDX = LabelIndex(LABELS)


def _roster() -> Roster:
    return Roster(
        [
            Member(gitlab_id=1, nickname="Yuan", role="開發組", position="組長"),
            Member(gitlab_id=2, nickname="Amy", position="總召"),
            Member(gitlab_id=3, nickname="Bob", position="總召"),
            Member(gitlab_id=4, nickname="Cat", role="行政組", position="組員"),  # 行政組無組長
        ]
    )


def test_team_with_leader() -> None:
    ta = resolve_team_assignment("開發組", _roster(), IDX)
    assert ta.team_label == "Team::開發組"
    assert ta.assignee_ids == [1]


def test_team_name_without_suffix_normalized() -> None:
    ta = resolve_team_assignment("開發", _roster(), IDX)  # 無「組」
    assert ta.team_label == "Team::開發組"
    assert ta.assignee_ids == [1]


def test_team_without_leader_falls_back_to_chiefs() -> None:
    ta = resolve_team_assignment("行政組", _roster(), IDX)  # RO-4
    assert ta.team_label == "Team::行政組"  # label 仍為該組
    assert sorted(ta.assignee_ids) == [2, 3]


def test_undetermined_goes_to_chief_team() -> None:
    ta = resolve_team_assignment(None, _roster(), IDX)  # GL-3
    assert ta.team_label == "Team::總召組"
    assert sorted(ta.assignee_ids) == [2, 3]


def test_unknown_team_goes_to_chief_team() -> None:
    ta = resolve_team_assignment("不存在的組", _roster(), IDX)
    assert ta.team_label == "Team::總召組"
    assert sorted(ta.assignee_ids) == [2, 3]


def test_roster_none_applies_label_without_assignees() -> None:
    ta = resolve_team_assignment("開發組", None, IDX)
    assert ta.team_label == "Team::開發組"
    assert ta.assignee_ids == []
