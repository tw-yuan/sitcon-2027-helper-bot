"""糾錯觸發的本機預篩（純邏輯）：判斷一則一般訊息是否值得交給 LLM 檢查。

兩種檢查：
  sitcon —— 訊息裡出現非全大寫的「sitcon」（網址、網域、@帳號、#hashtag、sitcon-tw 這類識別字除外）。
  zai    —— 訊息含「在」或「再」（是否真的用錯交給 LLM 判斷）。
預篩只決定「要不要問 LLM」；真正要不要糾正由 LLM 決定，沒錯就完全不回。
"""

from __future__ import annotations

import re

CHECK_SITCON = "sitcon"
CHECK_ZAI = "zai"

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\S+@\S+\.\S+")
# 前後緊鄰英數、底線、連字號、@、#、/、. 的都視為識別字的一部分（sitcon_tw、sitcon-tw/2027、#sitcon）；
# 後接數字照算（sitcon2027）；後接「.字母」視為網域（sitcon.org），句尾句點不算。
# 只看 ASCII，中文緊鄰（「的sitcon」）照樣命中。
_SITCON_RE = re.compile(
    r"(?<![A-Za-z0-9_@#/.\-])sitcon(?![A-Za-z_/\-]|\.[A-Za-z])",
    re.IGNORECASE,
)


def lowercase_sitcon(text: str) -> bool:
    """是否含大小寫不正確的 SITCON（一般文字中，非網址／帳號／識別字）。"""
    cleaned = _EMAIL_RE.sub(" ", _URL_RE.sub(" ", text))
    return any(m.group(0) != "SITCON" for m in _SITCON_RE.finditer(cleaned))


def has_zai(text: str) -> bool:
    return "在" in text or "再" in text


def proofread_targets(text: str, *, sitcon: bool = True, zai: bool = True) -> frozenset[str]:
    """回傳這則訊息需要 LLM 檢查的項目；空集合代表不觸發。"""
    if not text:
        return frozenset()
    found: set[str] = set()
    if sitcon and lowercase_sitcon(text):
        found.add(CHECK_SITCON)
    if zai and has_zai(text):
        found.add(CHECK_ZAI)
    return frozenset(found)
