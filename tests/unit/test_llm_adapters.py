"""T6：LLM adapter — tool call 往返、thinking/effort 對映、provider 切換。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sitcon_bot.services.llm.anthropic_adapter import AnthropicAdapter
from sitcon_bot.services.llm.base import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    build_llm_client,
)
from sitcon_bot.services.llm.openai_compat_adapter import OpenAICompatAdapter

TOOLS = [ToolSpec(name="gitlab_create_issue", description="建卡", input_schema={"type": "object"})]


# ------------------------------------------------------------------ #
# 假 SDK
# ------------------------------------------------------------------ #
class FakeAnthropicClient:
    def __init__(self, resp: Any) -> None:
        self._resp = resp
        self.last_params: dict[str, Any] | None = None
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **params: Any) -> Any:
        self.last_params = params
        return self._resp


def _anthropic_resp() -> Any:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="好，我來開卡"),
            SimpleNamespace(type="tool_use", id="tu1", name="gitlab_create_issue", input={"title": "官網倒數計時器"}),
        ],
        usage=SimpleNamespace(input_tokens=120, output_tokens=30),
        stop_reason="tool_use",
    )


class FakeOpenAIClient:
    def __init__(self, resp: Any) -> None:
        self._resp = resp
        self.last_params: dict[str, Any] | None = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **params: Any) -> Any:
        self.last_params = params
        return self._resp


def _openai_resp() -> Any:
    tc = SimpleNamespace(
        id="tc1",
        function=SimpleNamespace(name="gitlab_create_issue", arguments='{"title": "官網倒數計時器"}'),
    )
    msg = SimpleNamespace(content="好，我來開卡", tool_calls=[tc])
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
    return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30))


class FakeSecret:
    def __init__(self, v: str) -> None:
        self._v = v

    def get_secret_value(self) -> str:
        return self._v


# ------------------------------------------------------------------ #
# Anthropic adapter
# ------------------------------------------------------------------ #
async def test_anthropic_parses_text_and_tool_calls() -> None:
    client = FakeAnthropicClient(_anthropic_resp())
    adapter = AnthropicAdapter(api_key="x", model="claude-sonnet-4-6", client=client)
    r = await adapter.chat(
        system="你是小石", messages=[Message("user", [TextBlock("小石 幫我開卡")])], tools=TOOLS, thinking="high"
    )
    assert r.text == "好，我來開卡"
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].name == "gitlab_create_issue"
    assert r.tool_calls[0].arguments == {"title": "官網倒數計時器"}
    assert r.usage.input_tokens == 120


async def test_anthropic_thinking_high_maps_to_adaptive_effort() -> None:
    client = FakeAnthropicClient(_anthropic_resp())
    adapter = AnthropicAdapter(api_key="x", model="claude-sonnet-4-6", client=client)
    await adapter.chat(system="s", messages=[Message("user", [TextBlock("hi")])], tools=TOOLS, thinking="high")
    assert client.last_params["thinking"] == {"type": "adaptive"}
    assert client.last_params["output_config"] == {"effort": "high"}
    assert client.last_params["tools"][0]["name"] == "gitlab_create_issue"


async def test_anthropic_thinking_off_disables() -> None:
    client = FakeAnthropicClient(_anthropic_resp())
    adapter = AnthropicAdapter(api_key="x", model="claude-sonnet-4-6", client=client)
    await adapter.chat(system="s", messages=[Message("user", [TextBlock("hi")])], tools=[], thinking="off")
    assert client.last_params["thinking"] == {"type": "disabled"}
    assert "output_config" not in client.last_params
    assert "tools" not in client.last_params  # 無工具時不帶


async def test_anthropic_tool_loop_roundtrip_preserves_raw() -> None:
    resp = _anthropic_resp()
    client = FakeAnthropicClient(resp)
    adapter = AnthropicAdapter(api_key="x", model="claude-sonnet-4-6", client=client)
    r = await adapter.chat(system="s", messages=[Message("user", [TextBlock("開卡")])], tools=TOOLS, thinking="high")

    history = [
        Message("user", [TextBlock("開卡")]),
        r.assistant_message(),  # raw = resp.content（含潛在 thinking 區塊）
        Message("user", [ToolResultBlock("tu1", "#42 已建立")]),
    ]
    await adapter.chat(system="s", messages=history, tools=TOOLS, thinking="high")
    sent = client.last_params["messages"]
    assert sent[1] == {"role": "assistant", "content": resp.content}  # 原樣回填
    assert sent[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "#42 已建立"}],
    }


# ------------------------------------------------------------------ #
# OpenAI 相容 adapter
# ------------------------------------------------------------------ #
async def test_openai_parses_tool_calls_and_effort() -> None:
    client = FakeOpenAIClient(_openai_resp())
    adapter = OpenAICompatAdapter(api_key="x", model="anthropic/claude-sonnet-4.6", client=client)
    r = await adapter.chat(
        system="你是小石", messages=[Message("user", [TextBlock("開卡")])], tools=TOOLS, thinking="medium"
    )
    assert r.tool_calls[0].arguments == {"title": "官網倒數計時器"}
    assert client.last_params["reasoning_effort"] == "medium"
    assert client.last_params["messages"][0] == {"role": "system", "content": "你是小石"}
    assert client.last_params["tools"][0]["type"] == "function"


async def test_openai_thinking_off_omits_effort() -> None:
    client = FakeOpenAIClient(_openai_resp())
    adapter = OpenAICompatAdapter(api_key="x", model="gpt-x", client=client)
    await adapter.chat(system="s", messages=[Message("user", [TextBlock("hi")])], tools=[], thinking="off")
    assert "reasoning_effort" not in client.last_params


async def test_openai_tool_loop_roundtrip() -> None:
    client = FakeOpenAIClient(_openai_resp())
    adapter = OpenAICompatAdapter(api_key="x", model="gpt-x", client=client)
    r = await adapter.chat(system="s", messages=[Message("user", [TextBlock("開卡")])], tools=TOOLS, thinking="high")

    history = [
        Message("user", [TextBlock("開卡")]),
        r.assistant_message(),
        Message("user", [ToolResultBlock("tc1", "#42 已建立")]),
    ]
    await adapter.chat(system="s", messages=history, tools=TOOLS, thinking="high")
    sent = client.last_params["messages"]
    assert sent[0]["role"] == "system"
    assert sent[1] == {"role": "user", "content": "開卡"}
    assert sent[2] == r.raw_assistant  # assistant 原樣回填（含 tool_calls）
    assert sent[3] == {"role": "tool", "tool_call_id": "tc1", "content": "#42 已建立"}


# ------------------------------------------------------------------ #
# provider 切換
# ------------------------------------------------------------------ #
def test_factory_builds_anthropic() -> None:
    settings = SimpleNamespace(
        llm_provider="anthropic", llm_api_key=FakeSecret("k"), llm_model="claude-sonnet-4-6", llm_base_url=""
    )
    assert isinstance(build_llm_client(settings), AnthropicAdapter)


def test_factory_builds_openai_compat() -> None:
    settings = SimpleNamespace(
        llm_provider="openai_compat",
        llm_api_key=FakeSecret("k"),
        llm_model="anthropic/claude-sonnet-4.6",
        llm_base_url="https://openrouter.ai/api/v1",
    )
    assert isinstance(build_llm_client(settings), OpenAICompatAdapter)


# ------------------------------------------------------------------ #
# base_url（Anthropic-compatible gateway）
# ------------------------------------------------------------------ #
def test_anthropic_adapter_forwards_base_url() -> None:
    # 未注入 client → 建真實 AsyncAnthropic，須帶入自訂 base_url。
    adapter = AnthropicAdapter(api_key="k", model="claude-sonnet-4-6", base_url="https://ai.kot.gg")
    assert str(adapter._client.base_url).rstrip("/") == "https://ai.kot.gg"


def test_anthropic_adapter_default_base_url_when_unset() -> None:
    adapter = AnthropicAdapter(api_key="k", model="claude-sonnet-4-6")
    assert "api.anthropic.com" in str(adapter._client.base_url)


def test_factory_anthropic_passes_base_url() -> None:
    settings = SimpleNamespace(
        llm_provider="anthropic",
        llm_api_key=FakeSecret("k"),
        llm_model="claude-sonnet-4-6",
        llm_base_url="https://ai.kot.gg",
    )
    client = build_llm_client(settings)
    assert isinstance(client, AnthropicAdapter)
    assert str(client._client.base_url).rstrip("/") == "https://ai.kot.gg"
