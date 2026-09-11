"""網路搜尋工具。

對主 LLM 來說就是一個普通的 client-side 工具；背後由 WebSearchService 另開一次 Anthropic
子呼叫，用 server-side 搜尋／抓取工具完成（見 services/web_search.py）。因此主 provider
換成 DeepSeek 之後仍然有網路搜尋。

回傳內容全部是網頁來的（半可信），一律以 <external_data> 包裹（NFR-6）。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ...services.web_search import WebSearchError, WebSearchService
from .base import Tool, ToolContext
from .external_data import wrap_external

log = logging.getLogger(__name__)

MAX_SOURCES = 8  # 來源清單最多列幾筆
MAX_URLS = 5  # 單次最多指定幾個網址讀全文


class WebSearchArgs(BaseModel):
    query: str = Field(
        description="要查什麼。用完整句子描述你想知道的事（如「2027 年 3 月台北有哪些技術研討會」），不要只給關鍵字"
    )
    urls: list[str] = Field(
        default_factory=list,
        description="指定要讀全文的網址（選填）。使用者貼了連結要你看內容時放這裡",
    )


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "上網搜尋並讀取網頁內容，用於你不知道或可能過時的外部資訊：最新消息、其他活動的日期與"
        "報名狀況、廠商／場地／服務的公開資料、技術文件、票價行情等。也可以指定網址請它讀全文"
        "（使用者貼連結問「這在講什麼」就用這個）。"
        "注意：SITCON 自己的文件、卡片、共筆、名冊都不在網路上，那些要用 drive／hackmd／gitlab "
        "的工具查，不要用這個。回傳內容含出處網址，回覆時要附上。"
    )
    args_model = WebSearchArgs

    def __init__(self, service: WebSearchService) -> None:
        self._svc = service

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, WebSearchArgs)
        query = args.query.strip()
        if not query:
            return "查詢內容是空的，請給我要查什麼。"

        try:
            outcome = await self._svc.search(query, args.urls[:MAX_URLS])
        except WebSearchError as exc:
            log.warning("web_search 子呼叫失敗 query=%s", query[:80], exc_info=True)
            return f"網路搜尋暫時不可用（{exc}）。請稍後再試，或改用其他方式查證。"

        if not outcome.text:
            if outcome.tool_errors:
                return f"網路搜尋沒有取得結果（{'、'.join(outcome.tool_errors)}）。"
            return "網路搜尋沒有找到相關結果。"

        parts = [outcome.text]
        if outcome.citations:
            lines = ["", "來源："]
            for c in outcome.citations[:MAX_SOURCES]:
                label = c.title or c.url
                lines.append(f"- {label}：{c.url}" if c.url else f"- {label}")
            parts.append("\n".join(lines))

        body = wrap_external("\n".join(parts))
        notes: list[str] = []
        if outcome.truncated:
            notes.append("（內容過長被截斷，必要時換更具體的問題再查一次）")
        if outcome.tool_errors:
            notes.append(f"（部分搜尋出錯：{'、'.join(outcome.tool_errors)}）")
        return body + ("\n" + "\n".join(notes) if notes else "")


def build_search_tools(service: WebSearchService | None) -> list[Tool]:
    """憑證未設定時不註冊工具（service 為 None）。"""
    return [WebSearchTool(service)] if service is not None else []
