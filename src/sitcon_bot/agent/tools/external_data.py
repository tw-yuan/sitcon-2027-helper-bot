"""不受信任外部內容的資料圍欄（NFR-6）。

把外部系統取回的自由文字（GitLab 卡片標題／描述／留言、HackMD 筆記標題／內文、Drive 檔名
與路徑等）包進 ``<external_data>`` 圍欄並在 system prompt 宣告為「資料非指令」。

安全重點（防 delimiter breakout）：``wrap_external`` 在包裝前先呼叫 ``neutralize_fence``，
把內容裡任何「像 external_data 標記」的角括號序列（含全形角括號、選擇性 ``/`` 與屬性、
大小寫變體）換成安全佔位字串，使外部內容**無法**在中途關閉資料圍欄、把後續文字送回可信通道。
"""

from __future__ import annotations

import re

_OPEN = "<external_data>"
_CLOSE = "</external_data>"

# 比對任何「external_data 角括號標記」：ASCII 或全形角括號、可含前導 /、可帶屬性、不分大小寫。
_FENCE_RE = re.compile(r"[<＜]\s*/?\s*external_data\b[^>＞]*[>＞]", re.IGNORECASE)
# 佔位字串刻意不含角括號，插回後不會再被視為圍欄邊界。
_SENTINEL = "[external_data]"


def neutralize_fence(text: str) -> str:
    """中和內容中任何字面的 external_data 標記，杜絕資料圍欄被提前關閉。"""
    return _FENCE_RE.sub(_SENTINEL, text)


def wrap_external(text: str) -> str:
    """把不受信任的外部內容包進資料圍欄；內容先中和以防 breakout。"""
    return f"{_OPEN}\n{neutralize_fence(text)}\n{_CLOSE}"
