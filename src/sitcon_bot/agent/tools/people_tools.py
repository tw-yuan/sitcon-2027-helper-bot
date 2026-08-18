"""人名解析工具（RO-5）。把使用者提及的人名對應到名冊成員的 gitlab_id，供指派使用。

命中多人時列出候選（LLM 依 TRIG-7 反問）；查無此人時建議直接給 GitLab username。
（名冊完整欄位已在 system prompt 對照表中，此工具只回指派所需的識別欄位。）
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...services.sheets_roster import RosterService, RosterUnavailableError
from .base import Tool, ToolContext


class ResolvePersonArgs(BaseModel):
    query: str = Field(description="使用者提及的人名（暱稱、gitlab_username 或 telegram_username）")


class ResolvePersonTool(Tool):
    name = "resolve_person"
    description = (
        "把人名解析為名冊成員並回傳 gitlab_id（供 assignee 使用）。命中多人時會列出候選讓你反問；"
        "查無此人時建議請使用者直接提供 GitLab username。"
    )
    args_model = ResolvePersonArgs

    def __init__(self, roster: RosterService | None) -> None:
        self._roster = roster

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, ResolvePersonArgs)
        if self._roster is None:
            return "名冊未設定，無法解析人名；請直接提供 GitLab username。"
        try:
            roster = await self._roster.get()
        except RosterUnavailableError:
            return "名冊暫不可用，無法解析人名；請直接提供 GitLab username。"

        hits = roster.search_by_name(args.query)
        if not hits:
            return f"名冊查無「{args.query}」。建議請使用者直接提供其 GitLab username。"
        if len(hits) == 1:
            m = hits[0]
            return (
                f"命中：{m.nickname or m.gitlab_username}"
                f"（gitlab_username={m.gitlab_username}，gitlab_id={m.gitlab_id}）"
            )
        lines = [f"「{args.query}」命中 {len(hits)} 人，請向使用者確認要哪一位："]
        lines += [f"- {m.nickname or '(無暱稱)'}（{m.gitlab_username}，gitlab_id={m.gitlab_id}）" for m in hits]
        return "\n".join(lines)


def build_people_tools(roster: RosterService | None) -> list[Tool]:
    return [ResolvePersonTool(roster)]
