"""LLMClient 介面與共用型別（AGENTS 4.3）。

介面：`chat(system, messages, tools, thinking) -> LLMResponse`。
訊息採 provider 無關的內容區塊模型（text / tool_use / tool_result），adapter 各自轉為
provider 格式。assistant 回合另存 provider 原生內容（raw_assistant），以便在 tool loop
中原樣回填——extended/adaptive thinking 下，帶 tool_use 的 assistant 回合必須連同 thinking
區塊一併回傳，否則 Anthropic 會 400。

thinking 分級對映：延續 SPEC 的 off/low/medium/high，統一走「adaptive thinking + effort」
（而非已於新模型移除的 budget_tokens），對 Anthropic 與 OpenAI 相容格式皆適用。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

ThinkingLevel = Literal["off", "low", "medium", "high", "xhigh", "max"]

# 每次 LLM 呼叫的輸出上限（含 thinking）；非串流下需低於 SDK 逾時保護門檻。
DEFAULT_MAX_TOKENS = 16000


# --------------------------------------------------------------------------- #
# 內容區塊（provider 無關）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class TextBlock:
    text: str


@dataclass(slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class ToolResultBlock:
    tool_call_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(slots=True)
class Message:
    role: str  # "user" | "assistant"
    content: list[Block] = field(default_factory=list)
    # assistant 回合的 provider 原生內容；存在時 adapter 直接回填（保住 thinking 區塊）。
    raw: Any = None


# --------------------------------------------------------------------------- #
# 工具與回應
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(slots=True)
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    usage: Usage
    stop_reason: str
    model: str
    raw_assistant: Any  # 供 tool loop 原樣回填的 assistant 內容

    def assistant_message(self) -> Message:
        """把本回應組成可加入歷史的 assistant Message（保留 raw 供回填）。"""
        blocks: list[Block] = []
        if self.text:
            blocks.append(TextBlock(self.text))
        for tc in self.tool_calls:
            blocks.append(ToolUseBlock(id=tc.id, name=tc.name, input=tc.arguments))
        return Message(role="assistant", content=blocks, raw=self.raw_assistant)


class LLMClient(ABC):
    """LLM adapter 介面；每次呼叫記錄 model/tokens/latency（NFR-8）。"""

    @abstractmethod
    async def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        thinking: ThinkingLevel,
    ) -> LLMResponse: ...


def build_llm_client(settings: Any) -> LLMClient:
    """依 settings.llm_provider 建立對應 adapter（切換 provider 不需改碼）。"""
    provider = settings.llm_provider
    if provider == "anthropic":
        from .anthropic_adapter import AnthropicAdapter

        return AnthropicAdapter(
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            base_url=settings.llm_base_url or None,
        )
    if provider == "openai_compat":
        from .openai_compat_adapter import OpenAICompatAdapter

        return OpenAICompatAdapter(
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            base_url=settings.llm_base_url or None,
            service_tier=getattr(settings, "llm_service_tier", "") or "",
        )
    raise ValueError(f"未知的 LLM_PROVIDER：{provider}")
