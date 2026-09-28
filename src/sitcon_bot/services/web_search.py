"""網路搜尋服務：以獨立的 Anthropic 憑證做一次子呼叫，掛 server-side 搜尋／抓取工具。

主 LLM（2026-09-10 起是 DeepSeek，openai_compat）只看到一個普通的 client-side 工具
`web_search`；真正的搜尋在這裡另外開一次 Anthropic Messages 請求完成，結果以「子模型寫的
答案＋引用清單」回傳。搜尋能力因此與主 provider 脫鉤——換後端不會再把搜尋弄丟
（原本掛在 anthropic adapter 上的 LLM_WEB_SEARCH 在換 DeepSeek 後等同關閉）。

要點與坑：
- 搜尋結果的 `encrypted_content` 是密文，**本端讀不到**；只有 Anthropic 那側解得開餵給子模型。
  我們拿得到的是子模型寫出的答案，加上引用（url／title／cited_text 最多 150 字）。
- 需要整頁原文時靠一併掛上的 web_fetch：它回的是明文 document，但**只能抓對話中已出現過的
  網址**——搜尋結果的網址算數，呼叫端指定的網址則由 user message 帶入才可抓。
- 伺服器端工具出錯**不會丟例外**：HTTP 200，result 區塊的 content 由 list 變成單一錯誤物件，
  取值前必須先判型別。
- 長搜尋可能回 `stop_reason="pause_turn"`，要把該則 assistant 訊息**原樣**送回續跑。
- 工具版本釘在基本版：新版（動態過濾）經 sub2api gateway 會被轉成 code_execution 且拿不到
  結果（2026-08-24 實測）。換官方端點才可以考慮升版。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# 基本版工具：經 sub2api gateway 只有這組轉譯得動，勿改版本號（見模組 docstring）。
SEARCH_TOOL_TYPE = "web_search_20250305"
FETCH_TOOL_TYPE = "web_fetch_20250910"

MAX_TOKENS = 8000  # 子呼叫只要一段摘要，不需要長輸出
MAX_PAUSE_RESUMES = 5  # pause_turn 續跑上限，防無限迴圈
CITED_TEXT_LIMIT = 150  # 與 API 上限一致，僅作防禦性截斷

SUB_SYSTEM = """你是一個網路查證助手。使用者的問題來自一個台灣學生技術年會（SITCON）的工作人員聊天機器人。

規則：
- 一律用繁體中文（台灣用語）回答，任何情況都不要用英文或簡體字作答。
- 需要最新或你不確定的資訊就搜尋，必要時多搜幾次；能直接回答的常識不必搜。
- 使用者若提供網址，直接讀取該網址的內容再回答。
- 【重要】直接給結論。不要寫「我來查一下」「讓我搜尋看看」這類開場白或過程描述，
  第一句就是答案。
- 只陳述搜尋結果支持的事實，不要腦補細節。查不到就明講查不到。
- 回答控制在 400 字內，重點先講。不要加客套話或結尾攬客語。
- 不要在結尾問「需不需要我再查別的」或主動提議延伸查證；查到的講完就結束。
  （這段文字會被下游的機器人當素材，攬客語會被原封不動帶到使用者面前。）"""


@dataclass(slots=True)
class Citation:
    """一則引用來源。cited_text 為原文片段（API 上限 150 字）。"""

    url: str
    title: str
    cited_text: str = ""


@dataclass(slots=True)
class WebSearchOutcome:
    """一次搜尋的結果。text 是子模型寫的答案；citations 供回覆標註出處。"""

    text: str
    citations: list[Citation] = field(default_factory=list)
    searches: int = 0
    fetches: int = 0
    truncated: bool = False
    tool_errors: list[str] = field(default_factory=list)


class WebSearchError(RuntimeError):
    """子呼叫本身失敗（網路、認證、模型不存在等）。工具層轉成可讀訊息。"""


def _text_of(block: Any) -> str:
    return getattr(block, "text", "") or ""


def _citations_of(block: Any, fallback: tuple[str, str] = ("", "")) -> list[Citation]:
    """從 text 區塊取出引用。

    web_search 的引用自帶 url／title；web_fetch 的是 char_location，**沒有 url**，只有
    document_title。所以抓取時要用 fallback（該次 web_fetch 的網址與標題）補上，否則來源
    清單會出現沒有連結的項目，使用者點不進去。
    """
    fb_url, fb_title = fallback
    out: list[Citation] = []
    for c in getattr(block, "citations", None) or []:
        url = getattr(c, "url", "") or fb_url
        title = getattr(c, "title", "") or getattr(c, "document_title", "") or fb_title
        cited = (getattr(c, "cited_text", "") or "")[:CITED_TEXT_LIMIT]
        if url or title:
            out.append(Citation(url=url, title=title, cited_text=cited))
    return out


def _fetched_source(block: Any) -> tuple[str, str]:
    """從 web_fetch_tool_result 取出該次抓到的網址與標題；失敗回空。"""
    content = getattr(block, "content", None)
    if content is None or isinstance(content, list):
        return ("", "")
    url = getattr(content, "url", "") or ""
    doc = getattr(content, "content", None)
    title = getattr(doc, "title", "") or ""
    return (url, title)


def _dedupe(citations: list[Citation]) -> list[Citation]:
    """同一網址只留第一筆（引文最長的那筆優先）。"""
    best: dict[str, Citation] = {}
    for c in citations:
        key = c.url or c.title
        cur = best.get(key)
        if cur is None or len(c.cited_text) > len(cur.cited_text):
            best[key] = c
    return list(best.values())


class WebSearchService:
    """以自己的 Anthropic 憑證執行一次帶 server-side 搜尋工具的子呼叫。"""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        auth_bearer: bool = False,
        client: Any | None = None,
        max_uses: int = 0,
        max_content_tokens: int = 40000,
        country: str = "TW",
        timezone: str = "Asia/Taipei",
        timeout: float = 180.0,
    ) -> None:
        self._model = model
        self._max_uses = max_uses
        self._max_content_tokens = max_content_tokens
        self._country = country
        self._timezone = timezone
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic

            # 與 AnthropicAdapter 同樣支援只認 Bearer 的 gateway（ANTHROPIC_AUTH_TOKEN 形式）。
            if auth_bearer:
                self._client = AsyncAnthropic(auth_token=api_key, base_url=base_url, timeout=timeout)
            else:
                self._client = AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=timeout)

    # ------------------------------------------------------------------ #
    # 工具定義
    # ------------------------------------------------------------------ #
    def _tools(self) -> list[dict[str, Any]]:
        search: dict[str, Any] = {"type": SEARCH_TOOL_TYPE, "name": "web_search"}
        # max_uses 省略＝不限次數，由子模型自行決定要搜幾次（客戶指定）。
        if self._max_uses > 0:
            search["max_uses"] = self._max_uses
        search["user_location"] = {
            "type": "approximate",
            "country": self._country,
            "timezone": self._timezone,
        }
        fetch: dict[str, Any] = {
            "type": FETCH_TOOL_TYPE,
            "name": "web_fetch",
            "citations": {"enabled": True},  # 抓取的引用預設關閉，明確打開才有出處
            "max_content_tokens": self._max_content_tokens,
        }
        return [search, fetch]

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    async def search(self, query: str, urls: list[str] | None = None) -> WebSearchOutcome:
        """執行一次查詢。urls 會寫進 user message，讓 web_fetch 得以抓取（URL 驗證要求）。"""
        prompt = query.strip()
        if urls:
            listed = "\n".join(f"- {u}" for u in urls)
            prompt = f"{prompt}\n\n請一併讀取以下網址的內容：\n{listed}"

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        text_parts: list[str] = []
        citations: list[Citation] = []
        searches = fetches = 0
        tool_errors: list[str] = []
        truncated = False

        started = time.monotonic()
        for attempt in range(MAX_PAUSE_RESUMES + 1):
            try:
                resp = await self._client.messages.create(
                    model=self._model,
                    max_tokens=MAX_TOKENS,
                    system=SUB_SYSTEM,
                    messages=messages,
                    tools=self._tools(),
                )
            except Exception as exc:  # SDK 例外一律轉成工具層看得懂的錯誤
                raise WebSearchError(f"{type(exc).__name__}: {exc}") from exc

            # 區塊依序出現：抓取結果在引用它的 text 之前，故用「最近一次抓取」補 url。
            last_fetch: tuple[str, str] = ("", "")
            for block in resp.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    text_parts.append(_text_of(block))
                    citations.extend(_citations_of(block, last_fetch))
                elif btype == "server_tool_use":
                    if getattr(block, "name", "") == "web_search":
                        searches += 1
                    elif getattr(block, "name", "") == "web_fetch":
                        fetches += 1
                elif btype in ("web_search_tool_result", "web_fetch_tool_result"):
                    err = _tool_result_error(block)
                    if err:
                        tool_errors.append(err)
                    elif btype == "web_fetch_tool_result":
                        last_fetch = _fetched_source(block)

            stop = getattr(resp, "stop_reason", "") or ""
            if stop == "max_tokens":
                truncated = True
            if stop != "pause_turn":
                break
            # pause_turn：把該則 assistant 訊息原樣送回續跑（內容不可修改）。
            messages.append({"role": "assistant", "content": resp.content})
            if attempt == MAX_PAUSE_RESUMES:
                truncated = True

        latency = time.monotonic() - started
        log.info(
            "web_search model=%s searches=%d fetches=%d latency=%.2fs errors=%s",
            self._model,
            searches,
            fetches,
            latency,
            ",".join(tool_errors) or "-",
        )
        return WebSearchOutcome(
            text="".join(text_parts).strip(),
            citations=_dedupe(citations),
            searches=searches,
            fetches=fetches,
            truncated=truncated,
            tool_errors=tool_errors,
        )


def _tool_result_error(block: Any) -> str:
    """伺服器端工具的錯誤碼；成功時回空字串。

    成功的 web_search 結果 content 是 list；出錯時變成單一錯誤物件。web_fetch 兩種情況
    都是物件，靠 type 分辨。所以只能先判型別再取值（EC：不會丟例外，HTTP 一樣是 200）。
    """
    content = getattr(block, "content", None)
    if isinstance(content, list):
        return ""
    ctype = getattr(content, "type", "") or ""
    if ctype.endswith("_error"):
        return getattr(content, "error_code", "") or "unknown"
    return ""


def build_web_search_service(settings: Any) -> WebSearchService | None:
    """憑證未設定時回 None（工具不註冊），比照 calendar 的停用方式。"""
    key = settings.web_search_api_key.get_secret_value()
    if not key:
        return None
    return WebSearchService(
        api_key=key,
        model=settings.web_search_model,
        base_url=settings.web_search_base_url or None,
        auth_bearer=bool(settings.web_search_auth_bearer),
        max_uses=settings.web_search_max_uses,
        max_content_tokens=settings.web_search_max_content_tokens,
        country=settings.web_search_country,
        timezone=settings.tz,
        timeout=settings.web_search_timeout,
    )
