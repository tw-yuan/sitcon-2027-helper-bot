"""愛心 reaction 工具：react_heart 設定 ctx.reaction，經 AgentResult 帶回 gateway。"""

from __future__ import annotations

from sitcon_bot.agent.core import Agent, AgentRequest
from sitcon_bot.agent.prompts import PromptBuilder, PromptData
from sitcon_bot.agent.tools.base import ToolContext, ToolRegistry
from sitcon_bot.agent.tools.reaction_tools import HEART, ReactHeartArgs, ReactHeartTool, build_reaction_tools
from sitcon_bot.services.llm.base import (
    LLMClient,
    LLMResponse,
    Message,
    ThinkingLevel,
    ToolCall,
    ToolSpec,
    Usage,
)


def _ctx() -> ToolContext:
    return ToolContext(chat_id=-100, thread_id=None, user_id=7, username="yuan", text="hi")


async def test_run_sets_ctx_reaction() -> None:
    ctx = _ctx()
    out = await ReactHeartTool().run(ReactHeartArgs(), ctx)
    assert ctx.reaction == HEART
    assert "❤" in out


def test_heart_is_plain_u2764() -> None:
    # Telegram reaction 白名單是 U+2764 不帶 VS16；帶了會被 API 拒絕
    assert HEART == "❤"


class ScriptedLLM(LLMClient):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    async def chat(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec], thinking: ThinkingLevel
    ) -> LLMResponse:
        return self._responses.pop(0)


async def _provider() -> PromptData:
    return PromptData()


def _agent(llm: LLMClient) -> Agent:
    return Agent(llm=llm, tools=ToolRegistry(build_reaction_tools()), prompt_builder=PromptBuilder(_provider))


def _req(text: str) -> AgentRequest:
    return AgentRequest(chat_id=-100, thread_id=None, user_id=7, username="yuan", text=text)


def _tool_resp(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        text=None, tool_calls=list(calls), usage=Usage(0, 0), stop_reason="tool_use", model="m", raw_assistant=["raw"]
    )


def _text_resp(text: str) -> LLMResponse:
    return LLMResponse(
        text=text, tool_calls=[], usage=Usage(0, 0), stop_reason="end_turn", model="m", raw_assistant=[{"t": text}]
    )


async def test_agent_result_carries_heart() -> None:
    llm = ScriptedLLM([_tool_resp(ToolCall("t1", "react_heart", {})), _text_resp("謝謝你～")])
    r = await _agent(llm).handle(_req("小石謝謝你幫大忙！"))
    assert r.status == "ok"
    assert r.reaction == HEART


async def test_agent_result_no_reaction_by_default() -> None:
    llm = ScriptedLLM([_text_resp("好的")])
    r = await _agent(llm).handle(_req("hi"))
    assert r.reaction is None


async def test_heart_kept_on_clarify() -> None:
    # 同回合按了愛心又 ask_user → clarify 結果也帶回愛心
    llm = ScriptedLLM(
        [
            _tool_resp(ToolCall("t1", "react_heart", {}), ToolCall("a1", "ask_user", {"question": "要哪個？"})),
        ]
    )
    r = await _agent(llm).handle(_req("幫我處理那個"))
    assert r.status == "clarify"
    assert r.reaction == HEART
