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
    TextStreamHandler,
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
        on_text: TextStreamHandler | None = None,
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
        if on_text is None:
            resp = await self._client.chat.completions.create(**params)
            choice = resp.choices[0]
            content: str | None = choice.message.content
            finish_reason: str = choice.finish_reason or ""
            raw_usage = resp.usage
            msg_tool_calls = [
                (tc.id, tc.function.name, tc.function.arguments or "{}") for tc in choice.message.tool_calls or []
            ]
        else:
            content, finish_reason, raw_usage, msg_tool_calls = await self._chat_streamed(params, on_text)
        latency = time.monotonic() - started

        tool_calls: list[ToolCall] = []
        raw_tool_calls: list[dict[str, Any]] = []
        for tc_id, tc_name, args_raw in msg_tool_calls:
            try:
                arguments = json.loads(args_raw)
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(ToolCall(id=tc_id, name=tc_name, arguments=arguments))
            raw_tool_calls.append(
                {"id": tc_id, "type": "function", "function": {"name": tc_name, "arguments": args_raw}}
            )

        usage = Usage(
            input_tokens=getattr(raw_usage, "prompt_tokens", 0) if raw_usage else 0,
            output_tokens=getattr(raw_usage, "completion_tokens", 0) if raw_usage else 0,
        )
        log.info(
            "LLM openai_compat model=%s in=%d out=%d latency=%.2fs finish=%s tools=%d",
            self._model, usage.input_tokens, usage.output_tokens, latency, finish_reason, len(tool_calls),
        )

        raw_assistant: dict[str, Any] = {"role": "assistant", "content": content}
        if raw_tool_calls:
            raw_assistant["tool_calls"] = raw_tool_calls

        return LLMResponse(
            text=content or None,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=finish_reason,
            model=self._model,
            raw_assistant=raw_assistant,
        )

    async def _chat_streamed(
        self, params: dict[str, Any], on_text: TextStreamHandler
    ) -> tuple[str | None, str, Any, list[tuple[str, str, str]]]:
        """串流模式：text delta 以「累積全文」回呼；tool_call 依 index 累積 arguments 片段。

        回傳 (content, finish_reason, usage, tool_calls)，與非串流路徑同構。
        stream_options.include_usage 不被部分 gateway 支援時 usage 為 None（記 0）。
        """
        params = {**params, "stream": True, "stream_options": {"include_usage": True}}
        acc: list[str] = []
        finish_reason = ""
        raw_usage: Any = None
        pending: dict[int, dict[str, Any]] = {}  # index → {id, name, arguments 片段}
        stream = await self._client.chat.completions.create(**params)
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                raw_usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                acc.append(delta.content)
                await on_text("".join(acc))
            for tc in delta.tool_calls or []:
                slot = pending.setdefault(tc.index, {"id": "", "name": "", "arguments": []})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["arguments"].append(tc.function.arguments)
        tool_calls = [
            (slot["id"], slot["name"], "".join(slot["arguments"]) or "{}")
            for _, slot in sorted(pending.items())
        ]
        return "".join(acc) or None, finish_reason, raw_usage, tool_calls

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
