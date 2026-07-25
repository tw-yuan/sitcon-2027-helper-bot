"""T3：HTML escape 與分段送出（TRIG-8）。"""

from __future__ import annotations

from sitcon_bot.telegram.formatting import escape_html, split_message


def test_escape_html() -> None:
    assert escape_html("<b>a</b> & c") == "&lt;b&gt;a&lt;/b&gt; &amp; c"


def test_escape_leaves_plain_text() -> None:
    assert escape_html("小石 #42 已更新") == "小石 #42 已更新"


def test_short_message_not_split() -> None:
    assert split_message("hello") == ["hello"]


def test_empty_message() -> None:
    assert split_message("") == [""]


def test_hard_split_no_separators_preserves_content() -> None:
    text = "a" * 5000
    chunks = split_message(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks) == text  # 無分隔字元時完整保留


def test_split_prefers_newline_boundary() -> None:
    lines = [f"第{i}行內容" for i in range(2000)]
    text = "\n".join(lines)
    chunks = split_message(text, limit=1000)
    assert len(chunks) >= 2
    assert all(len(c) <= 1000 for c in chunks)
    assert all(c for c in chunks)  # 無空片段
    # 每個邊界至多丟一個分隔字元，內容不遺失
    total = sum(len(c) for c in chunks)
    assert len(text) - (len(chunks) - 1) <= total <= len(text)


def test_split_at_limit_boundary() -> None:
    text = "x" * 3900
    assert split_message(text, limit=3900) == [text]
    assert len(split_message("x" * 3901, limit=3900)) == 2
