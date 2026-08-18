"""T7：Agent loop — 多輪 tool-calling、迭代上限、反問→續接、參數驗證、脈絡指代。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sitcon_bot.agent.core import Agent, AgentRequest
from sitcon_bot.agent.prompts import PromptBuilder, PromptData
from sitcon_bot.agent.tools.base import Tool, ToolContext, ToolRegistry
from sitcon_bot.services.llm.base import (
    LLMClient,
    LLMResponse,
    Message,
    TextBlock,
    ThinkingLevel,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    Usage,
)


class ScriptedLLM(LLMClient):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec], thinking: ThinkingLevel
    ) -> LLMResponse:
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        return self._responses.pop(0)


def _tool_resp(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        text=None, tool_calls=list(calls), usage=Usage(0, 0), stop_reason="tool_use", model="m", raw_assistant=["raw"]
    )


def _text_resp(text: str) -> LLMResponse:
    return LLMResponse(
        text=text, tool_calls=[], usage=Usage(0, 0), stop_reason="end_turn", model="m", raw_assistant=[{"t": text}]
    )


class EchoArgs(BaseModel):
    value: str


class RecordTool(Tool):
    name = "record"
    description = "記錄一個值"
    args_model = EchoArgs

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, EchoArgs)
        self.calls.append((args.value, ctx.user_id))
        return f"已記錄「{args.value}」"


async def _provider() -> PromptData:
    return PromptData(labels=["Status::Inbox", "Team::開發組"], roster_rows=[{"nickname": "Yuan"}], charter=None)


async def _no_sleep(_delay: float) -> None:
    return None


def _agent(llm: LLMClient, tools: list[Tool], max_iterations: int = 8) -> tuple[Agent, None]:
    agent = Agent(
        llm=llm,
        tools=ToolRegistry(tools),
        prompt_builder=PromptBuilder(_provider),
        thinking="high",
        max_iterations=max_iterations,
        sleep=_no_sleep,
    )
    return agent, None


def _req(
    text: str,
    chat_id: int = -100,
    thread: int | None = None,
    resume: object | None = None,
    history: list[Message] | None = None,
    reply_context: str | None = None,
) -> AgentRequest:
    return AgentRequest(
        chat_id=chat_id, thread_id=thread, user_id=7, username="yuan", text=text,
        resume=resume, history=history, reply_context=reply_context,
    )


def _tool_results(messages: list[Message]) -> list[ToolResultBlock]:
    return [b for m in messages for b in m.content if isinstance(b, ToolResultBlock)]


# ------------------------------------------------------------------ #
async def test_single_tool_then_final() -> None:
    llm = ScriptedLLM([_tool_resp(ToolCall("t1", "record", {"value": "官網倒數計時器"})), _text_resp("卡片已建立")])
    tool = RecordTool()
    agent, _ = _agent(llm, [tool])

    result = await agent.handle(_req("小石 幫我開卡：官網倒數計時器"))
    assert result.reply == "卡片已建立"
    assert result.status == "ok"
    assert tool.calls == [("官網倒數計時器", 7)]
    # 工具結果有回填給第二次 LLM 呼叫
    fed = _tool_results(llm.calls[1]["messages"])
    assert fed and fed[0].content == "已記錄「官網倒數計時器」"


async def test_multi_round_tool_calls() -> None:
    llm = ScriptedLLM(
        [
            _tool_resp(ToolCall("t1", "record", {"value": "A"})),
            _tool_resp(ToolCall("t2", "record", {"value": "B"})),
            _text_resp("兩件都完成"),
        ]
    )
    tool = RecordTool()
    agent, _ = _agent(llm, [tool])
    result = await agent.handle(_req("做兩件事"))
    assert result.reply == "兩件都完成"
    assert [v for v, _ in tool.calls] == ["A", "B"]
    assert len(llm.calls) == 3


async def test_iteration_limit_stops() -> None:
    # 每輪都回工具呼叫，永不結束
    llm = ScriptedLLM([_tool_resp(ToolCall(f"t{i}", "record", {"value": str(i)})) for i in range(5)])
    tool = RecordTool()
    agent, _ = _agent(llm, [tool], max_iterations=2)
    result = await agent.handle(_req("跑不完的需求"))
    assert "步驟太多" in result.reply
    assert len(llm.calls) == 2  # 未超過上限
    assert len(tool.calls) == 2


async def test_ask_user_then_resume() -> None:
    llm = ScriptedLLM(
        [
            _tool_resp(ToolCall("a1", "ask_user", {"question": "要改哪一張卡？", "options": ["#42 官網", "#43 報名"]})),
            _text_resp("已把 #42 改成 Doing"),
        ]
    )
    agent, _ = _agent(llm, [RecordTool()])

    r1 = await agent.handle(_req("把它改成 Doing"))
    assert r1.status == "clarify"
    assert "要改哪一張卡？" in r1.reply
    assert "1. #42 官網" in r1.reply
    assert r1.pending is not None  # 待答狀態回傳給 gateway（以問句 message_id 保存）

    # 純 reply-chain：使用者回覆問句 → gateway 以 resume 帶回待答狀態續接
    r2 = await agent.handle(_req("#42", resume=r1.pending))
    assert r2.status == "ok"
    assert r2.reply == "已把 #42 改成 Doing"
    # 續接時把使用者回答當成 ask_user 的 tool_result 回填
    fed = _tool_results(llm.calls[1]["messages"])
    assert any(b.tool_call_id == "a1" and b.content == "#42" for b in fed)


async def test_validation_error_fed_back() -> None:
    llm = ScriptedLLM(
        [
            _tool_resp(ToolCall("t1", "record", {"wrong_field": "x"})),  # 缺 value
            _text_resp("我調整了做法"),
        ]
    )
    tool = RecordTool()
    agent, _ = _agent(llm, [tool])
    result = await agent.handle(_req("觸發驗證錯誤"))
    assert result.reply == "我調整了做法"
    assert tool.calls == []  # run 未被呼叫
    fed = _tool_results(llm.calls[1]["messages"])
    assert fed and "參數不正確" in fed[0].content


async def test_reply_context_injected_as_data() -> None:
    # 純 reply-chain：被回覆訊息的內容當脈絡注入，且包在 <external_data> 內（視為資料非指令）
    llm = ScriptedLLM([_text_resp("已更新 #42")])
    agent, _ = _agent(llm, [RecordTool()])
    await agent.handle(_req("把它改成 Doing", reply_context="✅ 已建立 #42：官網倒數計時器"))
    texts = " ".join(b.text for m in llm.calls[0]["messages"] for b in m.content if isinstance(b, TextBlock))
    assert "#42" in texts and "官網倒數計時器" in texts  # 被回覆內容進入脈絡
    assert "<external_data>" in texts  # 但標記為資料
    assert "把它改成 Doing" in texts


async def test_no_context_without_reply() -> None:
    # 無回覆時完全無狀態：兩則獨立訊息，第二則看不到第一則
    llm = ScriptedLLM([_text_resp("回一"), _text_resp("回二")])
    agent, _ = _agent(llm, [RecordTool()])
    await agent.handle(_req("第一件事"))
    await agent.handle(_req("第二件事"))
    second = " ".join(b.text for m in llm.calls[1]["messages"] for b in m.content if isinstance(b, TextBlock))
    assert "第一件事" not in second and "回一" not in second


# ------------------------------------------------------------------ #
# 回覆續接（2026-08-11 修訂）：完成回合交回完整 transcript，回覆該則時以其續接
# ------------------------------------------------------------------ #
async def test_result_carries_full_transcript() -> None:
    llm = ScriptedLLM([_tool_resp(ToolCall("t1", "record", {"value": "官網倒數"})), _text_resp("卡片已建立")])
    agent, _ = _agent(llm, [RecordTool()])
    result = await agent.handle(_req("開卡：官網倒數"))
    assert result.history is not None
    roles = [m.role for m in result.history]
    assert roles == ["user", "assistant", "user", "assistant"]  # 發話→tool_use→tool_result→最終回覆
    assert any(isinstance(b, ToolResultBlock) for b in result.history[2].content)  # 工具結果在 transcript 內
    final = result.history[-1]
    assert any(isinstance(b, TextBlock) and b.text == "卡片已建立" for b in final.content)
    assert final.raw is not None  # raw（thinking）保留供回填


async def test_history_continuation_feeds_prior_tool_records() -> None:
    llm1 = ScriptedLLM([_tool_resp(ToolCall("t1", "record", {"value": "A 案"})), _text_resp("查到 A 案")])
    agent1, _ = _agent(llm1, [RecordTool()])
    first = await agent1.handle(_req("查一下"))

    # 使用者回覆了「查到 A 案」→ gateway 以 history 帶回 transcript
    llm2 = ScriptedLLM([_text_resp("A 案的日期是 3/13")])
    agent2, _ = _agent(llm2, [RecordTool()])
    r2 = await agent2.handle(_req("那它日期是？", history=first.history))
    assert r2.reply == "A 案的日期是 3/13"

    sent = llm2.calls[0]["messages"]
    assert sent[: len(first.history)] == first.history  # 完整前情（含工具紀錄）原樣在前
    fed = _tool_results(sent)
    assert any("已記錄「A 案」" in b.content for b in fed)  # 上一輪工具結果看得到
    last = sent[-1]
    assert last.role == "user"
    texts = " ".join(b.text for b in last.content if isinstance(b, TextBlock))
    assert "那它日期是？" in texts
    assert "發話者" in texts  # 續接回合一樣附發話者身分（回覆者可能不是原提問者）
    # 續接回合自己也交回更長的 transcript，供再往下回覆
    assert r2.history is not None and len(r2.history) == len(first.history) + 2


async def test_clarify_turn_has_no_history() -> None:
    llm = ScriptedLLM([_tool_resp(ToolCall("a1", "ask_user", {"question": "哪一張？"}))])
    agent, _ = _agent(llm, [RecordTool()])
    result = await agent.handle(_req("改卡"))
    assert result.status == "clarify"
    assert result.history is None  # 反問輪走 pending 續接，不存 history


async def test_unknown_tool_returns_notice() -> None:
    llm = ScriptedLLM([_tool_resp(ToolCall("t1", "nonexistent", {})), _text_resp("好")])
    agent, _ = _agent(llm, [RecordTool()])
    await agent.handle(_req("呼叫不存在的工具"))
    fed = _tool_results(llm.calls[1]["messages"])
    assert fed and "未知工具" in fed[0].content


# ------------------------------------------------------------------ #
# T12：LLM 呼叫錯誤處理（EC-15/EC-9/EC-10）與稽核動作（LOG-1）
# ------------------------------------------------------------------ #
class _AuthError(Exception):
    status_code = 401


class FlakyLLM(LLMClient):
    def __init__(self, responses: list[LLMResponse], fail_times: int = 0, exc: Exception | None = None) -> None:
        self._responses = list(responses)
        self._fail = fail_times
        self._exc = exc or RuntimeError("boom")
        self.calls = 0

    async def chat(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec], thinking: ThinkingLevel
    ) -> LLMResponse:
        self.calls += 1
        if self.calls <= self._fail:
            raise self._exc
        return self._responses.pop(0)


async def test_llm_retry_then_succeed() -> None:
    llm = FlakyLLM([_text_resp("ok")], fail_times=5)
    agent, _ = _agent(llm, [])
    r = await agent.handle(_req("hi"))
    assert r.reply == "ok"
    assert llm.calls == 6  # 失敗五次後第六次成功（EC-15：最多重試五次）


async def test_llm_unavailable_after_retries() -> None:
    llm = FlakyLLM([], fail_times=10, exc=RuntimeError("Connection timed out"))
    agent, _ = _agent(llm, [])
    r = await agent.handle(_req("hi"))
    assert r.status == "error"
    assert r.error == "llm_unavailable"
    assert "稍後再試" in r.reply
    # 回覆附上可讓管理員辨認的診斷資訊（例外類型＋截短訊息）
    assert "錯誤資訊" in r.reply
    assert "RuntimeError" in r.reply
    assert "Connection timed out" in r.reply
    assert r.detail == {"llm_error": "RuntimeError，Connection timed out"}
    assert llm.calls == 6  # 最多重試五次


async def test_llm_credential_error_not_retried() -> None:
    llm = FlakyLLM([], fail_times=5, exc=_AuthError())
    agent, _ = _agent(llm, [])
    r = await agent.handle(_req("hi"))
    assert r.status == "error"
    assert r.error == "llm_credential"
    assert "憑證" in r.reply
    assert llm.calls == 1  # 憑證錯誤不重試（EC-10）


async def test_result_action_reflects_last_tool() -> None:
    llm = ScriptedLLM([_tool_resp(ToolCall("t1", "record", {"value": "x"})), _text_resp("done")])
    agent, _ = _agent(llm, [RecordTool()])
    r = await agent.handle(_req("hi"))
    assert r.action == "record"  # LOG-1：稽核動作反映實際工具
    assert r.detail == {"tools": ["record"]}


async def test_result_action_chat_when_no_tool() -> None:
    llm = ScriptedLLM([_text_resp("純聊天")])
    agent, _ = _agent(llm, [RecordTool()])
    r = await agent.handle(_req("hi"))
    assert r.action == "chat"


# ------------------------------------------------------------------ #
# 發話者身分注入（「我／幫我」自動解析 RO-5）
# ------------------------------------------------------------------ #
def _roster(rows: list[list[str]]):
    from sitcon_bot.services.sheets_roster import Roster, parse_roster

    header = ["nickname", "gitlab_username", "gitlab_id", "telegram_username", "telegram_id", "role", "position"]
    return Roster(parse_roster(header, rows).members)


class _StubRosterService:
    def __init__(self, roster) -> None:
        self._r = roster

    async def get(self):
        return self._r


def _agent_with_roster(llm: LLMClient, roster) -> Agent:
    return Agent(
        llm=llm,
        tools=ToolRegistry([]),
        prompt_builder=PromptBuilder(_provider),
        roster=_StubRosterService(roster),
        thinking="high",
    )


def _first_user_texts(llm: ScriptedLLM) -> list[str]:
    msg = llm.calls[0]["messages"][-1]
    return [b.text for b in msg.content if isinstance(b, TextBlock)]


async def test_requester_note_resolves_self_from_roster() -> None:
    # _req 的 user_id=7 → 對到名冊 telegram_id=7 的 Yuan
    roster = _roster([["Yuan", "yuan_tw", "13267906", "@yuan", "7", "行政組", "組長"]])
    llm = ScriptedLLM([_text_resp("好")])
    await _agent_with_roster(llm, roster).handle(_req("幫我開一張卡並指派給我"))
    texts = _first_user_texts(llm)
    joined = "\n".join(texts)
    assert "gitlab_id=13267906" in joined  # 直接給出本人 gitlab_id
    assert "Yuan" in joined
    assert "幫我開一張卡並指派給我" in texts  # 原文獨立成塊，未被污染


async def test_same_turn_tools_run_concurrently_in_order() -> None:
    import asyncio

    started = asyncio.Event()

    class SlowTool(Tool):
        name = "slow"
        description = "先等對方開始"
        args_model = EchoArgs

        async def run(self, args: BaseModel, ctx: ToolContext) -> str:
            # 若工具是序列執行（slow 在 fast 之前），這裡會等不到而逾時 → 併發才會通過
            await asyncio.wait_for(started.wait(), timeout=1.0)
            return "slow_done"

    class FastTool(Tool):
        name = "fast"
        description = "先開始"
        args_model = EchoArgs

        async def run(self, args: BaseModel, ctx: ToolContext) -> str:
            started.set()
            return "fast_done"

    llm = ScriptedLLM(
        [_tool_resp(ToolCall("t1", "slow", {"value": "a"}), ToolCall("t2", "fast", {"value": "b"})), _text_resp("ok")]
    )
    agent, _ = _agent(llm, [SlowTool(), FastTool()])
    r = await agent.handle(_req("同時做兩件事"))
    assert r.reply == "ok"
    # 結果回填順序需與 tool_calls 相同（slow 在前），且兩者都真的完成（證明併發）
    fed = _tool_results(llm.calls[1]["messages"])
    assert [b.content for b in fed] == ["slow_done", "fast_done"]


async def test_requester_note_unknown_user_asks_for_username() -> None:
    roster = _roster([["別人", "other", "999", "@other", "888", "開發組", "組長"]])
    llm = ScriptedLLM([_text_resp("好")])
    await _agent_with_roster(llm, roster).handle(_req("指派給我"))  # user_id=7 不在名冊
    joined = "\n".join(_first_user_texts(llm))
    assert "查無" in joined and "GitLab username" in joined


# ------------------------------------------------------------------ #
# 串流：on_partial 一路傳到 LLM 呼叫（Telegram 草稿即時預覽）
# ------------------------------------------------------------------ #
async def test_on_partial_plumbed_to_llm_as_on_text() -> None:
    class StreamingLLM(LLMClient):
        async def chat(
            self,
            *,
            system: str,
            messages: list[Message],
            tools: list[ToolSpec],
            thinking: ThinkingLevel,
            on_text: Any = None,
        ) -> LLMResponse:
            assert on_text is not None
            await on_text("卡")
            await on_text("卡片已建立")
            return _text_resp("卡片已建立")

    seen: list[str] = []

    async def on_partial(t: str) -> None:
        seen.append(t)

    agent, _ = _agent(StreamingLLM(), [])
    req = _req("hi")
    req.on_partial = on_partial
    r = await agent.handle(req)
    assert seen == ["卡", "卡片已建立"]
    assert r.reply == "卡片已建立"


async def test_no_on_partial_keeps_legacy_chat_signature() -> None:
    # 未串流時不得傳 on_text——不認識該參數的 LLMClient 替身／舊實作必須維持可用
    llm = ScriptedLLM([_text_resp("好")])
    agent, _ = _agent(llm, [])
    r = await agent.handle(_req("hi"))
    assert r.reply == "好"
