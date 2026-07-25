"""字串比對輔助：label 正規化與近似候選（GL-12）。

正規化涵蓋全半形（NFKC）、空白、大小寫，供 label 精確對應；無法精確對應時
以近似排序列出既有 label 供選擇。
"""

from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher


def normalize_label(s: str) -> str:
    """正規化 label 字串以利比對：NFKC（全形→半形）、移除所有空白、casefold。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = "".join(s.split())  # 去掉所有空白（含中間）
    return s.casefold()


def nearest_labels(query: str, candidates: list[str], n: int = 5) -> list[str]:
    """回傳與 query 最接近的至多 n 個既有 label 名稱（GL-12）。"""
    nq = normalize_label(query)
    scored: list[tuple[float, str]] = []
    for c in candidates:
        nc = normalize_label(c)
        ratio = SequenceMatcher(None, nq, nc).ratio()
        if nq and (nq in nc or nc in nq):
            ratio = max(ratio, 0.9)  # 子字串命中給高分
        scored.append((ratio, c))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, c in scored[:n]]
