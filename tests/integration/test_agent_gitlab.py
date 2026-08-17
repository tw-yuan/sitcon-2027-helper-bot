"""T8 整合：agent + 真實 GitLab 工具 + 假 backend/名冊 + 腳本 LLM，打通 UC-1 開卡自動分派。"""

from __future__ import annotations

from typing import Any

from sitcon_bot.agent.core import Agent, AgentRequest
from sitcon_bot.agent.prompts import PromptBuilder, PromptData
from sitcon_bot.agent.tools.base import ToolRegistry
from sitcon_bot.agent.tools.gitlab_tools import build_gitlab_tools
from sitcon_bot.agent.tools.people_tools import build_people_tools
from sitcon_bot.services.gitlab_client import GitLabClient
from sitcon_bot.services.llm.base import (
    LLMClient,
    LLMResponse,
    Message,
    ThinkingLevel,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    Usage,
)
from sitcon_bot.services.sheets_roster import Member, Roster

LABELS = ["Team::開發組", "Team::總召組"]
STATUSES = ["Inbox", "Doing", "Review"]  # native status（2026-08-17 修訂）


class FakeBackend:
    def __init__(self) -> None:
        self.last_create_payload: dict[str, Any] | None = None
        self.issue_statuses: dict[int, str] = {}

    def list_labels(self) -> list[str]:
        return list(LABELS)

    def list_statuses(self) -> list[str]:
        return list(STATUSES)

    def set_issue_status(self, iid: int, status: str) -> str:
        self.issue_statuses[iid] = status
        return status

    def create_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.last_create_payload = payload
        return {
            "iid": 100,
            "web_url": "https://gitlab/100",
            "title": payload["title"],
            "description": payload.get("description"),
            "labels": payload["labels"].split(","),
            "assignees": [{"id": i, "username": f"u{i}"} for i in payload["assignee_ids"]],
            "due_date": None,
            "state": "opened",
        }


class FakeRosterService:
    def __init__(self, roster: Roster) -> None:
        self._r = roster

    async def get(self) -> Roster:
        return self._r


class ScriptedLLM(LLMClient):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    async def chat(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec], thinking: ThinkingLevel
    ) -> LLMResponse:
        self.calls.append(list(messages))
        return self._responses.pop(0)


async def test_uc1_create_issue_auto_dispatch() -> None:
    backend = FakeBackend()
    roster = Roster([Member(gitlab_id=1, nickname="Yuan", role="開發組", position="組長")])

    async def _noop(_: float) -> None:
        return None

    gitlab = GitLabClient(backend, sleep=_noop)
    roster_service = FakeRosterService(roster)
    tools = ToolRegistry([*build_gitlab_tools(gitlab, roster_service), *build_people_tools(roster_service)])

    async def provider() -> PromptData:
        return PromptData(labels=LABELS, statuses=STATUSES, roster_rows=roster.to_llm_rows(), charter=None)

    llm = ScriptedLLM(
        [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall("c1", "gitlab_create_issue", {"title": "官網倒數計時器壞了", "team": "開發組"})],
                usage=Usage(0, 0), stop_reason="tool_use", model="m", raw_assistant=["raw"],
            ),
            LLMResponse(
                text="✅ 已建立 #100，指派給開發組組長 Yuan",
                tool_calls=[], usage=Usage(0, 0), stop_reason="end_turn", model="m", raw_assistant=["t"],
            ),
        ]
    )
    agent = Agent(llm, tools, PromptBuilder(provider), thinking="high")

    result = await agent.handle(
        AgentRequest(
            chat_id=-100, thread_id=None, user_id=42, username="yuan", text="小石 幫我開卡：官網倒數計時器壞了"
        )
    )

    assert result.status == "ok"
    assert "#100" in result.reply
    # 卡片以正確 label + 預設 native status + 組長 assignee 建立
    labels = set(backend.last_create_payload["labels"].split(","))
    assert labels == {"Team::開發組"}
    assert backend.issue_statuses[100] == "Inbox"  # GL-5：預設狀態走 native status
    assert backend.last_create_payload["assignee_ids"] == [1]
    # 工具結果（含 #100）有回填給第二次 LLM 呼叫
    fed = [b for m in llm.calls[1] for b in m.content if isinstance(b, ToolResultBlock)]
    assert fed and "#100" in fed[0].content
