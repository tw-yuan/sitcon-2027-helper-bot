"""日期解析輔助（Asia/Taipei）。

供 HackMD 會議筆記標題／tag 用的 MMDD 取值。自然語言日期（「下週五」）由 LLM 解析為
YYYY-MM-DD 後傳入；此處只負責格式擷取與預設今日（HM-4／NFR-7）。
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Taipei"


def today_taipei(tz: str = DEFAULT_TZ) -> datetime:
    return datetime.now(ZoneInfo(tz))


def to_mmdd(date_str: str | None, tz: str = DEFAULT_TZ) -> str:
    """把日期字串轉為 MMDD（補零）。None／無法解析 → 今日。"""
    if date_str:
        s = date_str.strip()
        m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)  # YYYY-MM-DD
        if m:
            return f"{int(m.group(2)):02d}{int(m.group(3)):02d}"
        m = re.match(r"^(\d{1,2})[-/.](\d{1,2})$", s)  # MM-DD
        if m:
            return f"{int(m.group(1)):02d}{int(m.group(2)):02d}"
        m = re.match(r"^(\d{2})(\d{2})$", s)  # MMDD
        if m:
            return s
    d = today_taipei(tz)
    return f"{d.month:02d}{d.day:02d}"


def display_date(date_str: str | None, tz: str = DEFAULT_TZ) -> str:
    """供模板 {{date}} 用的顯示日期（YYYY-MM-DD）。"""
    if date_str and date_str.strip():
        return date_str.strip()
    return today_taipei(tz).strftime("%Y-%m-%d")
