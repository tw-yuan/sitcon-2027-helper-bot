"""組別指派解析（GL-2／GL-3／RO-4 的確定性部分）。

語意判斷（「官網倒數計時器」→ 開發組）由 LLM 在主迴圈以職掌文件＋label 名稱完成，
結果以 team_name 傳入。本模組把 team_name 映射為要套用的 Team:: label 與 assignee：

  team 可判斷 + 有組長  → Team::<組> + 組長
  team 可判斷 + 無組長  → Team::<組> + 全部總召（RO-4）
  team 無法判斷（None） → Team::總召組 + 全部總召（GL-3 fallback）

名冊暫不可用時，只套 label、不自動指派，並於 note 說明。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..services.gitlab_client import LabelIndex
    from ..services.sheets_roster import Roster

CHIEF_TEAM = "總召組"


@dataclass(slots=True)
class TeamAssignment:
    team_label: str | None  # 要套用的 Team:: label（None 表示白名單中查無對應）
    assignee_ids: list[int]
    note: str


def _resolve_team_label(label_index: LabelIndex, team_name: str | None) -> str | None:
    """把組名對應到既有 Team:: label（容忍有無「組」尾綴）。"""
    if not team_name:
        return None
    for candidate in (f"Team::{team_name}", f"Team::{team_name}組"):
        resolved = label_index.resolve(candidate)
        if resolved:
            return resolved
    return None


def resolve_team_assignment(
    team_name: str | None,
    roster: Roster | None,
    label_index: LabelIndex,
) -> TeamAssignment:
    label = _resolve_team_label(label_index, team_name)

    if label is not None:
        leader = roster.leader_of(team_name or "") if roster else None
        if leader is not None:
            note = f"自動指派「{label}」組長 {leader.nickname or ''}".rstrip()
            return TeamAssignment(label, [leader.gitlab_id], note)
        if roster is not None:
            chiefs = roster.chiefs()
            return TeamAssignment(label, [c.gitlab_id for c in chiefs], f"「{label}」查無組長，改指派總召")
        return TeamAssignment(label, [], f"套用「{label}」（名冊暫不可用，未自動指派）")

    # 無法判斷 → GL-3 總召組 fallback
    chief_label = _resolve_team_label(label_index, CHIEF_TEAM)
    if roster is not None:
        chiefs = roster.chiefs()
        return TeamAssignment(chief_label, [c.gitlab_id for c in chiefs], "組別無法判斷，落總召組並指派全部總召")
    return TeamAssignment(chief_label, [], "組別無法判斷，落總召組（名冊暫不可用，未自動指派）")
