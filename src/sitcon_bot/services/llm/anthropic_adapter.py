"""Anthropic Messages API adapter。

thinking 對映：off → thinking disabled；low/medium/high → adaptive thinking + effort。
（不使用 budget_tokens——該機制已於 Opus 4.7+/Sonnet 5/Fable 5 移除並回 400。）
帶 tool_use 的 assistant 回合以 raw 內容原樣回填，保住 thinking 區塊。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import (
    DEFAULT_MAX_TOKENS,
    LLMClient,
    LLMResponse,
    Message,
    TextBlock,
    TextStreamHandler,
    ThinkingLevel,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

log = logging.getLogger(__name__)


class AnthropicAdapter(LLMClient):
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        client: Any | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        auth_bearer: bool = False,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic

            # base_url 支援 Anthropic-compatible gateway（如自架 proxy）；
            # SDK 會在其後接 /v1/messages。None 則走官方 api.anthropic.com。
            # auth_bearer：以 Authorization: Bearer 送出憑證（對應 ANTHROPIC_AUTH_TOKEN），
            # 供只認 Bearer 的 gateway；False 則走官方的 x-api-key。兩者擇一，不可同時送。
            if auth_bearer:
                self._client = AsyncAnthropic(auth_token=api_key, base_url=base_url)
            else:
                self._client = AsyncAnthropic(api_key=api_key, base_url=base_url)

    async def chat(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        thinking: ThinkingLevel,
        on_text: TextStreamHandler | None = None,
    ) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [self._to_message(m) for m in messages],
        }
        if tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        if thinking == "off":
            params["thinking"] = {"type": "disabled"}
        else:
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": thinking}

        started = time.monotonic()
        if on_text is None:
            resp = await self._client.messages.create(**params)
        else:
            # 串流模式：text delta 以「累積全文」回呼（gateway 節流送 Telegram 草稿），
            # 最終仍取完整 message，後續解析與非串流路徑完全相同。
            async with self._client.messages.stream(**params) as stream:
                acc: list[str] = []
                async for delta in stream.text_stream:
                    acc.append(delta)
                    await on_text("".join(acc))
                resp = await stream.get_final_message()
        latency = time.monotonic() - started

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))

        usage = Usage(
            input_tokens=getattr(resp.usage, "input_tokens", 0),
            output_tokens=getattr(resp.usage, "output_tokens", 0),
        )
        log.info(
            "LLM anthropic model=%s in=%d out=%d latency=%.2fs stop=%s tools=%d",
            self._model, usage.input_tokens, usage.output_tokens, latency, resp.stop_reason, len(tool_calls),
        )
        return LLMResponse(
            text="".join(text_parts) or None,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=resp.stop_reason or "",
            model=self._model,
            raw_assistant=resp.content,  # 原樣回填（含 thinking 區塊）
        )

    @staticmethod
    def _to_message(m: Message) -> dict[str, Any]:
        if m.role == "assistant" and m.raw is not None:
            return {"role": "assistant", "content": m.raw}
        content: list[dict[str, Any]] = []
        for b in m.content:
            if isinstance(b, TextBlock):
                content.append({"type": "text", "text": b.text})
            elif isinstance(b, ToolUseBlock):
                content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            elif isinstance(b, ToolResultBlock):
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": b.tool_call_id,
                    "content": b.content,
                }
                if b.is_error:
                    block["is_error"] = True
                content.append(block)
        return {"role": m.role, "content": content}
