"""Anthropic Messages API adapter。

thinking 對映：off → thinking disabled（Sonnet 5.5 改送 between_tools）；low/medium/high → adaptive thinking + effort。
（不使用 budget_tokens——該機制已於 Opus 4.7+/Sonnet 5/Fable 5 移除並回 400。）
帶 tool_use 的 assistant 回合以 raw 內容原樣回填，保住 thinking 區塊。
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

from .base import (
    DEFAULT_MAX_TOKENS,
    ImageBlock,
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

# 安全分類器拒答時由伺服器端改用其他模型重跑；"default" 依拒答類別選模型
# （Sonnet 5.5：cyber／frontier_llm → Sonnet 5；bio 等類別不重跑）。
FALLBACK_BETA = "server-side-fallback-2026-07-01"
REFUSAL_NOTICE = "（這個請求被模型的安全機制擋下了，請換個說法再試一次。）"

# 這些模型的 thinking {"type": "disabled"} 會回 400，關閉思考要改送 between_tools（不可帶其他欄位）。
_BETWEEN_TOOLS_MODELS = ("claude-sonnet-5-5",)


def _thinking_off(model: str) -> dict[str, Any]:
    if model.startswith(_BETWEEN_TOOLS_MODELS):
        return {"type": "between_tools"}
    return {"type": "disabled"}


def _is_thinking_binding_error(exc: Exception) -> bool:
    """回填的 thinking 區塊因對話前綴改變而被拒（400，訊息含 bound to a different conversation）。"""
    return getattr(exc, "status_code", None) == 400 and "different conversation" in str(exc)


def _strip_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """拿掉 assistant 歷史中的 thinking／redacted_thinking 區塊；只剩 thinking 的訊息整則略過。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if m.get("role") != "assistant" or not isinstance(content, list):
            out.append(m)
            continue
        kept = [b for b in content if _block_type(b) not in ("thinking", "redacted_thinking")]
        if kept:
            out.append({**m, "content": kept})
    return out


def after_fallback(content: Any) -> Any:
    """伺服器端 fallback 後可回填的內容；沒有 fallback 就原物件奉還。

    最後一個 fallback 區塊之前只留 text 與成對的 server tool 區塊。

    中途拒答再由其他模型接手時，邊界前的 thinking／tool_use 是被中斷那次的產物——回填會被拒，
    tool_use 也不能執行；text 則是接手模型的續寫起點，要保留。fallback 區塊本身只是稽核標記，
    一律拿掉（非 beta 的 SDK 型別會把它解析成 text=None 的 TextBlock，原樣回填不安全）。
    """
    idx = max((i for i, b in enumerate(content) if _block_type(b) == "fallback"), default=-1)
    if idx < 0:
        return content
    head = content[:idx]
    result_ids = {getattr(b, "tool_use_id", None) for b in head if (_block_type(b) or "").endswith("_tool_result")}
    kept = [
        b
        for b in head
        if _block_type(b) == "text"
        or (_block_type(b) or "").endswith("_tool_result")
        or (_block_type(b) == "server_tool_use" and getattr(b, "id", None) in result_ids)
    ]
    return kept + [b for b in content[idx + 1 :] if _block_type(b) != "fallback"]


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


class AnthropicAdapter(LLMClient):
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        client: Any | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        auth_bearer: bool = False,
        web_search: bool = False,
        refusal_fallback: bool = False,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._web_search = web_search
        self._refusal_fallback = refusal_fallback
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
                {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
            ]
        if self._web_search:
            # Server-side 工具：Anthropic 端執行，結果以 server_tool_use／web_search_tool_result
            # 區塊出現在回應中（raw 回填即保留於歷史）。用基本版 20250305——實測本 gateway
            # 對新版 20260209（動態過濾）轉譯不穩。搜尋中途可能回 stop_reason=pause_turn。
            params.setdefault("tools", []).append({"type": "web_search_20250305", "name": "web_search", "max_uses": 5})
        if thinking == "off":
            params["thinking"] = _thinking_off(self._model)
        else:
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": thinking}
        if self._refusal_fallback:
            params["extra_headers"] = {"anthropic-beta": FALLBACK_BETA}
            params["extra_body"] = {"fallbacks": "default"}

        started = time.monotonic()
        try:
            resp = await self._send(params, on_text)
        except Exception as exc:
            if not _is_thinking_binding_error(exc):
                raise
            # Sonnet 5.5／Opus 5.5 起 thinking 區塊綁定產生它的對話前綴（system／tools／先前訊息）。
            # reply-chain 續接時 system prompt 會重組（名冊、label、群組記憶變動就不同），回填的
            # thinking 因此被拒。依官方建議拿掉歷史中的 thinking 區塊重送一次（text／tool_use 保留）。
            log.warning("thinking 區塊與對話前綴不符，拿掉歷史 thinking 重送：%s", exc)
            params["messages"] = _strip_thinking(params["messages"])
            resp = await self._send(params, on_text)
        latency = time.monotonic() - started

        content = after_fallback(resp.content)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text or "")
            elif btype == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))

        usage = Usage(
            input_tokens=getattr(resp.usage, "input_tokens", 0),
            output_tokens=getattr(resp.usage, "output_tokens", 0),
        )
        served = getattr(resp, "model", None) or self._model  # fallback 接手時是接手的模型
        log.info(
            "LLM anthropic model=%s in=%d out=%d latency=%.2fs stop=%s tools=%d",
            served,
            usage.input_tokens,
            usage.output_tokens,
            latency,
            resp.stop_reason,
            len(tool_calls),
        )
        if resp.stop_reason == "refusal":
            # 連 fallback 都拒（或該類別不重跑）：丟掉可能的半截輸出，回固定說明；不帶 raw，
            # 歷史只留這段說明文字，續接時不會回填被中斷的內容。
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            log.warning("LLM 拒答 model=%s category=%s", served, category)
            return LLMResponse(
                text=REFUSAL_NOTICE, tool_calls=[], usage=usage, stop_reason="refusal", model=served, raw_assistant=None
            )
        return LLMResponse(
            text="".join(text_parts) or None,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=resp.stop_reason or "",
            model=served,
            raw_assistant=content,  # 原樣回填（含 thinking 區塊；fallback 時已清掉被中斷的部分）
        )

    async def _send(self, params: dict[str, Any], on_text: TextStreamHandler | None) -> Any:
        if on_text is None:
            return await self._client.messages.create(**params)
        # 串流模式：text delta 以「累積全文」回呼（gateway 節流送 Telegram 草稿），
        # 最終仍取完整 message，後續解析與非串流路徑完全相同。
        async with self._client.messages.stream(**params) as stream:
            acc: list[str] = []
            async for delta in stream.text_stream:
                acc.append(delta)
                await on_text("".join(acc))
            return await stream.get_final_message()

    @staticmethod
    def _to_message(m: Message) -> dict[str, Any]:
        if m.role == "assistant" and m.raw is not None:
            return {"role": "assistant", "content": m.raw}
        content: list[dict[str, Any]] = []
        for b in m.content:
            if isinstance(b, TextBlock):
                content.append({"type": "text", "text": b.text})
            elif isinstance(b, ImageBlock):
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": b.media_type,
                            "data": base64.b64encode(b.data).decode("ascii"),
                        },
                    }
                )
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
