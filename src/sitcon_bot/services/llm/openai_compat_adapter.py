"""OpenAI 相容格式 adapter（OpenAI、OpenRouter；差 base_url 與 model 字串）。

thinking 對映：low/medium/high → reasoning_effort；off → 不帶。
工具結果在此格式為獨立的 role=tool 訊息；assistant 回合以 raw dict 原樣回填。

service_tier：留空則不帶（由 provider 決定）。Codex 的「fast mode」對應 service_tier="fast"，
官方 API 另有 priority／flex；能不能用取決於帳號與所走的 gateway，故做成純直通設定，
不支援時 provider 會回 400，改回留空即可。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .base import (
    DEFAULT_MAX_TOKENS,
    LLMClient,
    LLMResponse,
    Message,
    TextBlock,
    ThinkingLevel,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

log = logging.getLogger(__name__)


class OpenAICompatAdapter(LLMClient):
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        client: Any | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        service_tier: str = "",
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._service_tier = service_tier
        if client is not None:
            self._client = client
        else:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        thinking: ThinkingLevel,
    ) -> LLMResponse:
        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            oai_messages.extend(self._to_messages(m))

        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": oai_messages,
        }
        if tools:
            params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
            params["tool_choice"] = "auto"
        if thinking != "off":
            params["reasoning_effort"] = thinking
        if self._service_tier:
            params["service_tier"] = self._service_tier

        started = time.monotonic()
        resp = await self._client.chat.completions.create(**params)
        latency = time.monotonic() - started

        choice = resp.choices[0]
        msg = choice.message
        tool_calls: list[ToolCall] = []
        raw_tool_calls: list[dict[str, Any]] = []
        for tc in msg.tool_calls or []:
            args_raw = tc.function.arguments or "{}"
            try:
                arguments = json.loads(args_raw)
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=arguments))
            raw_tool_calls.append(
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": args_raw}}
            )

        usage = Usage(
            input_tokens=getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0,
            output_tokens=getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0,
        )
        log.info(
            "LLM openai_compat model=%s in=%d out=%d latency=%.2fs finish=%s tools=%d",
            self._model, usage.input_tokens, usage.output_tokens, latency, choice.finish_reason, len(tool_calls),
        )

        raw_assistant: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if raw_tool_calls:
            raw_assistant["tool_calls"] = raw_tool_calls

        return LLMResponse(
            text=msg.content or None,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=choice.finish_reason or "",
            model=self._model,
            raw_assistant=raw_assistant,
        )

    @staticmethod
    def _to_messages(m: Message) -> list[dict[str, Any]]:
        # assistant 回合原樣回填
        if m.role == "assistant" and m.raw is not None:
            return [m.raw]

        out: list[dict[str, Any]] = []
        text_parts: list[str] = []
        tool_use_blocks: list[ToolUseBlock] = []
        for b in m.content:
            if isinstance(b, TextBlock):
                text_parts.append(b.text)
            elif isinstance(b, ToolResultBlock):
                # 工具結果為獨立的 role=tool 訊息
                out.append({"role": "tool", "tool_call_id": b.tool_call_id, "content": b.content})
            elif isinstance(b, ToolUseBlock):
                tool_use_blocks.append(b)

        if m.role == "assistant":
            am: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_use_blocks:
                am["tool_calls"] = [
                    {
                        "id": t.id,
                        "type": "function",
                        "function": {"name": t.name, "arguments": json.dumps(t.input, ensure_ascii=False)},
                    }
                    for t in tool_use_blocks
                ]
            out.insert(0, am)
        elif text_parts:
            out.insert(0, {"role": "user", "content": "".join(text_parts)})
        return out
