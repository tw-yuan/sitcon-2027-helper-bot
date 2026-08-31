"""回覆組裝：HTML escape 與分段送出（TRIG-8）。

以 HTML parse mode 回覆（MarkdownV2 escape 地雷多，AGENTS 4.6）。
超過單則長度上限時分段，不得截斷（TRIG-8）。
"""

from __future__ import annotations

import html

# Telegram 單則訊息硬上限為 4096 字元；留些餘裕給潛在的 entity。
TELEGRAM_MAX_LEN = 4096
SPLIT_LIMIT = 3900


def escape_html(text: str) -> str:
    """escape 使用者/外部內容中的 & < >，避免破壞 HTML parse mode。"""
    return html.escape(text, quote=False)


def tg_mention_html(username: str | None, telegram_id: int | None, display: str) -> str | None:
    """組出會跳通知的 mention 片段：優先 @username，其次 tg://user?id= 點擊式連結。

    兩者皆無（通知不到）回 None，由呼叫端決定退化呈現。
    """
    if username:
        return f"@{escape_html(username)}"
    if telegram_id is not None:
        return f'<a href="tg://user?id={telegram_id}">{escape_html(display)}</a>'
    return None


def split_message(text: str, limit: int = SPLIT_LIMIT) -> list[str]:
    """把長訊息切成 ≤ limit 的片段，優先在換行、其次空白處斷開；絕不截斷內容。

    單一超長「詞」（無空白）才硬切。回傳至少一個片段（空字串亦回傳 ['']）。
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        # 優先在最後一個換行斷開
        cut = window.rfind("\n")
        if cut <= 0:
            # 沒有換行，退而求其次在最後一個空白斷開
            cut = window.rfind(" ")
        if cut <= 0:
            # 連空白都沒有：硬切
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
        # 斷點若在換行/空白，去掉該前導分隔字元
        if remaining[:1] in ("\n", " "):
            remaining = remaining[1:]
    if remaining:
        chunks.append(remaining)
    return chunks
