"""T8：人名解析工具（RO-5）。"""

from __future__ import annotations

from sitcon_bot.agent.tools.base import ToolContext
from sitcon_bot.agent.tools.people_tools import ResolvePersonArgs, ResolvePersonTool
from sitcon_bot.services.sheets_roster import Member, Roster, RosterUnavailableError

CTX = ToolContext(chat_id=-1, thread_id=None, user_id=1, username="yuan", text="x")


class FakeRosterService:
    def __init__(self, roster: Roster | None, unavailable: bool = False) -> None:
        self._r = roster
        self._unavailable = unavailable

    async def get(self) -> Roster:
        if self._unavailable:
            raise RosterUnavailableError("down")
        assert self._r is not None
        return self._r


def _roster() -> Roster:
    return Roster(
        [
            Member(gitlab_id=1, nickname="Yuan", gitlab_username="yuan_tw"),
            Member(gitlab_id=2, nickname="Yuchen", gitlab_username="yuchen"),
            Member(gitlab_id=3, nickname="Leaf", gitlab_username="leaf"),
        ]
    )


async def test_single_hit_returns_gitlab_id() -> None:
    tool = ResolvePersonTool(FakeRosterService(_roster()))
    reply = await tool.run(ResolvePersonArgs(query="leaf"), CTX)
    assert "gitlab_id=3" in reply


async def test_multiple_hits_lists_candidates() -> None:
    tool = ResolvePersonTool(FakeRosterService(_roster()))
    reply = await tool.run(ResolvePersonArgs(query="yu"), CTX)  # Yuan + Yuchen
    assert "命中 2 人" in reply
    assert "gitlab_id=1" in reply and "gitlab_id=2" in reply


async def test_no_hit_suggests_username() -> None:
    tool = ResolvePersonTool(FakeRosterService(_roster()))
    reply = await tool.run(ResolvePersonArgs(query="nobody"), CTX)
    assert "GitLab username" in reply


async def test_roster_unavailable() -> None:
    tool = ResolvePersonTool(FakeRosterService(None, unavailable=True))
    reply = await tool.run(ResolvePersonArgs(query="yuan"), CTX)
    assert "暫不可用" in reply


async def test_no_roster_configured() -> None:
    tool = ResolvePersonTool(None)
    reply = await tool.run(ResolvePersonArgs(query="yuan"), CTX)
    assert "未設定" in reply
