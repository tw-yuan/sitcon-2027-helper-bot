"""網路搜尋：子呼叫解析、伺服器端錯誤分支、pause_turn 續跑、工具層資料圍欄。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sitcon_bot.agent.tools.base import ToolContext
from sitcon_bot.agent.tools.search_tools import WebSearchArgs, WebSearchTool, build_search_tools
from sitcon_bot.services.web_search import (
    FETCH_TOOL_TYPE,
    SEARCH_TOOL_TYPE,
    WebSearchError,
    WebSearchService,
    build_web_search_service,
)


# ------------------------------------------------------------------ #
# 假 SDK
# ------------------------------------------------------------------ #
class FakeAnthropicClient:
    """依序回傳預設好的 responses；記錄每次送出的參數。"""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **params: Any) -> Any:
        self.calls.append(params)
        return self._responses.pop(0)


class ExplodingClient:
    def __init__(self) -> None:
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **params: Any) -> Any:
        raise RuntimeError("gateway 掛了")


def _citation(url: str, title: str, cited: str) -> Any:
    return SimpleNamespace(type="web_search_result_location", url=url, title=title, cited_text=cited)


def _ok_response(stop: str = "end_turn") -> Any:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="我來查一下。", citations=None),
            SimpleNamespace(type="server_tool_use", name="web_search", input={"query": "SITCON 2027"}),
            SimpleNamespace(
                type="web_search_tool_result",
                content=[SimpleNamespace(type="web_search_result", url="https://sitcon.org", title="SITCON")],
            ),
            SimpleNamespace(
                type="text",
                text="SITCON 2027 預計三月舉辦。",
                citations=[_citation("https://sitcon.org", "SITCON 官網", "2027 年 3 月")],
            ),
        ],
        stop_reason=stop,
    )


def _error_response() -> Any:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="server_tool_use", name="web_search", input={"query": "x"}),
            SimpleNamespace(
                type="web_search_tool_result",
                # 出錯時 content 是單一物件而非 list（HTTP 仍是 200）
                content=SimpleNamespace(type="web_search_tool_result_error", error_code="too_many_requests"),
            ),
            SimpleNamespace(type="text", text="", citations=None),
        ],
        stop_reason="end_turn",
    )


def _fetch_response() -> Any:
    """web_fetch 的引用是 char_location，沒有 url，只有 document_title。"""
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="server_tool_use", name="web_fetch", input={"url": "https://sitcon.org/2026/"}),
            SimpleNamespace(
                type="web_fetch_tool_result",
                content=SimpleNamespace(
                    type="web_fetch_result",
                    url="https://sitcon.org/2026/",
                    content=SimpleNamespace(type="document", title="Jam the Chaos - SITCON 2026"),
                ),
            ),
            SimpleNamespace(
                type="text",
                text="這頁在講 SITCON 2026。",
                citations=[
                    SimpleNamespace(
                        type="char_location", document_title="Jam the Chaos - SITCON 2026", cited_text="2026/03/28"
                    )
                ],
            ),
        ],
        stop_reason="end_turn",
    )


def _service(client: Any, **kw: Any) -> WebSearchService:
    return WebSearchService(api_key="x", model="claude-opus-4-8", client=client, **kw)


# ------------------------------------------------------------------ #
# 服務層
# ------------------------------------------------------------------ #
async def test_parses_text_and_citations() -> None:
    client = FakeAnthropicClient(_ok_response())
    outcome = await _service(client).search("SITCON 2027 什麼時候")

    assert "三月舉辦" in outcome.text
    assert outcome.searches == 1
    assert outcome.tool_errors == []
    assert [c.url for c in outcome.citations] == ["https://sitcon.org"]
    assert outcome.citations[0].cited_text == "2027 年 3 月"


async def test_tools_include_search_and_fetch_without_max_uses() -> None:
    client = FakeAnthropicClient(_ok_response())
    await _service(client).search("問題")

    tools = client.calls[0]["tools"]
    assert [t["type"] for t in tools] == [SEARCH_TOOL_TYPE, FETCH_TOOL_TYPE]
    # max_uses=0（預設）＝不帶此欄位，等於不限次數
    assert "max_uses" not in tools[0]
    assert tools[0]["user_location"]["country"] == "TW"
    assert tools[1]["citations"] == {"enabled": True}


async def test_max_uses_sent_when_configured() -> None:
    client = FakeAnthropicClient(_ok_response())
    await _service(client, max_uses=3).search("問題")
    assert client.calls[0]["tools"][0]["max_uses"] == 3


async def test_urls_are_put_into_prompt_so_fetch_may_read_them() -> None:
    """web_fetch 只能抓對話中出現過的網址，故指定網址必須寫進 user message。"""
    client = FakeAnthropicClient(_ok_response())
    await _service(client).search("這篇在講什麼", urls=["https://example.com/a"])

    prompt = client.calls[0]["messages"][0]["content"]
    assert "https://example.com/a" in prompt


async def test_fetch_citation_gets_url_from_fetch_result() -> None:
    """抓取的引用本身沒有 url，要用該次 web_fetch 的網址補上，否則來源清單點不進去。"""
    client = FakeAnthropicClient(_fetch_response())
    outcome = await _service(client).search("這頁在講什麼", urls=["https://sitcon.org/2026/"])

    assert outcome.fetches == 1
    assert [c.url for c in outcome.citations] == ["https://sitcon.org/2026/"]
    assert outcome.citations[0].title == "Jam the Chaos - SITCON 2026"


async def test_server_tool_error_is_collected_not_raised() -> None:
    client = FakeAnthropicClient(_error_response())
    outcome = await _service(client).search("問題")

    assert outcome.tool_errors == ["too_many_requests"]
    assert outcome.text == ""


async def test_pause_turn_resumes_with_untouched_assistant_content() -> None:
    paused = _ok_response(stop="pause_turn")
    client = FakeAnthropicClient(paused, _ok_response())
    outcome = await _service(client).search("問題")

    assert len(client.calls) == 2
    # 續跑時把原本的 assistant content 原樣回填
    resumed = client.calls[1]["messages"][-1]
    assert resumed["role"] == "assistant"
    assert resumed["content"] is paused.content
    assert outcome.searches == 2  # 兩回合各一次搜尋


async def test_sdk_failure_becomes_web_search_error() -> None:
    with pytest.raises(WebSearchError):
        await _service(ExplodingClient()).search("問題")


# ------------------------------------------------------------------ #
# 工具層
# ------------------------------------------------------------------ #
def _ctx() -> ToolContext:
    return ToolContext(chat_id=-100, thread_id=None, user_id=1, username="yoru", text="小石 查一下")


async def test_tool_wraps_result_in_external_data_and_lists_sources() -> None:
    tool = WebSearchTool(_service(FakeAnthropicClient(_ok_response())))
    out = await tool.run(WebSearchArgs(query="SITCON 2027 什麼時候"), _ctx())

    assert "<external_data>" in out and "</external_data>" in out
    assert "三月舉辦" in out
    assert "https://sitcon.org" in out


async def test_tool_reports_service_failure_readably() -> None:
    tool = WebSearchTool(_service(ExplodingClient()))
    out = await tool.run(WebSearchArgs(query="問題"), _ctx())

    assert "不可用" in out
    assert "<external_data>" not in out


async def test_tool_handles_empty_result() -> None:
    tool = WebSearchTool(_service(FakeAnthropicClient(_error_response())))
    out = await tool.run(WebSearchArgs(query="問題"), _ctx())
    assert "too_many_requests" in out


# ------------------------------------------------------------------ #
# 組裝
# ------------------------------------------------------------------ #
def test_tools_not_registered_without_credential() -> None:
    assert build_search_tools(None) == []


def test_build_service_returns_none_without_key(required_env: dict[str, str], monkeypatch: Any) -> None:
    from sitcon_bot.settings import Settings

    for k, v in required_env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert build_web_search_service(settings) is None


async def test_refusal_fallback_params_and_refusal_reported() -> None:
    refused = SimpleNamespace(content=[], stop_reason="refusal")
    client = FakeAnthropicClient(refused)
    outcome = await _service(client, refusal_fallback=True).search("某個 CVE 的細節")

    assert client.calls[0]["extra_headers"] == {"anthropic-beta": "server-side-fallback-2026-07-01"}
    assert client.calls[0]["extra_body"] == {"fallbacks": "default"}
    assert outcome.tool_errors == ["refusal"]
    assert outcome.text == ""
