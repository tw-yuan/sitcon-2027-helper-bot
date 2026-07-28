"""照片索引：解析、tag 切分、關鍵字 AND 搜尋、結構化過濾、offset 分頁。"""

from __future__ import annotations

from sitcon_bot.services.photo_index import PhotoIndex, parse_photos

HEADER = [
    "photo_id", "photo_url", "image_preview_url", "album_title", "subject_type", "photographer",
    "scene_tags", "mood_tags", "recommended_uses", "orientation", "visual_description", "people_count",
]


def _row(pid, album="Camp 2026", subj="people", scene="講者;螢幕", desc="台上講者",
         orient="landscape", people="1", preview="p_q.jpg"):
    return [pid, f"https://flickr/{pid}", preview, album, subj, "康喔", scene,
            "歡樂", "簡報", orient, desc, people]


def _index(rows):
    return PhotoIndex(parse_photos(HEADER, rows))


def test_parse_and_tag_split() -> None:
    p = parse_photos(HEADER, [_row("1", desc="綠色布旗")])[0]
    assert p.photo_id == "1" and p.photo_url == "https://flickr/1"
    assert p.scene_tags == ["講者", "螢幕"]
    assert p.people_count == 1
    assert "camp 2026" in p.blob and "講者" in p.blob  # blob 為小寫、含相簿與 tag


def test_parse_skips_rows_without_id() -> None:
    photos = parse_photos(HEADER, [_row("1"), ["", "", "", "", "", "", "", "", "", "", "", ""]])
    assert [p.photo_id for p in photos] == ["1"]


def test_search_keyword_and() -> None:
    idx = _index([
        _row("1", scene="講者;螢幕", desc="台上講者演講"),
        _row("2", scene="工作坊;交流", desc="分組討論"),
    ])
    assert [p.photo_id for p in idx.search(["講者"]).photos] == ["1"]
    assert idx.search(["工作坊", "交流"]).total == 1  # 多關鍵字 AND
    assert idx.search(["講者", "工作坊"]).total == 0


def test_search_filters() -> None:
    idx = _index([
        _row("1", subj="people", orient="landscape", people="3"),
        _row("2", subj="screen", orient="portrait", people="0"),
    ])
    assert [p.photo_id for p in idx.search([], orientation="portrait").photos] == ["2"]
    assert [p.photo_id for p in idx.search([], subject_type="people").photos] == ["1"]
    assert [p.photo_id for p in idx.search([], has_people=True).photos] == ["1"]
    assert [p.photo_id for p in idx.search([], has_people=False).photos] == ["2"]


def test_search_offset_pagination() -> None:
    idx = _index([_row(str(i)) for i in range(15)])
    r0 = idx.search(["講者"], offset=0, limit=10)
    assert len(r0.photos) == 10 and r0.total == 15 and r0.has_more
    r1 = idx.search(["講者"], offset=10, limit=10)
    assert len(r1.photos) == 5 and not r1.has_more
