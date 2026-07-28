"""photo_search 工具：列出結果（含 external_data 包裹）、代表縮圖塞入 ctx.media、無結果/未設定。"""

from __future__ import annotations

from sitcon_bot.agent.tools.base import ToolContext
from sitcon_bot.agent.tools.photo_tools import PhotoSearchArgs, PhotoSearchTool
from sitcon_bot.services.photo_index import PhotoIndex, parse_photos

HEADER = [
    "photo_id", "photo_url", "image_preview_url", "album_title", "subject_type", "photographer",
    "scene_tags", "mood_tags", "recommended_uses", "orientation", "visual_description", "people_count",
]


def _row(pid):
    return [pid, f"https://flickr/{pid}", "p_q.jpg", "Camp 2026", "people", "康喔",
            "講者;螢幕", "歡樂", "簡報", "landscape", f"台上講者{pid}", "2"]


def _ctx():
    return ToolContext(chat_id=-1, thread_id=None, user_id=7, username="yuan", text="x")


class _Svc:
    def __init__(self, index):
        self._i = index

    async def get(self):
        return self._i


def _tool(rows):
    return PhotoSearchTool(_Svc(PhotoIndex(parse_photos(HEADER, rows))))


async def test_lists_and_registers_representative_thumbs() -> None:
    ctx = _ctx()
    reply = await _tool([_row(str(i)) for i in range(5)]).run(PhotoSearchArgs(keywords=["講者"]), ctx)
    assert "共 5 張" in reply
    assert "<external_data>" in reply and "flickr" in reply  # 清單包在資料圍欄
    # 前 3 張縮圖交給 gateway 送出，_q → _z 較大預覽
    assert len(ctx.media) == 3
    assert ctx.media[0].url.endswith("_z.jpg")
    assert "Camp 2026" in ctx.media[0].caption and "flickr" in ctx.media[0].caption


async def test_no_results_registers_no_media() -> None:
    ctx = _ctx()
    reply = await _tool([_row("1")]).run(PhotoSearchArgs(keywords=["不存在關鍵字zzz"]), ctx)
    assert "找不到" in reply
    assert ctx.media == []


async def test_unconfigured() -> None:
    ctx = _ctx()
    reply = await PhotoSearchTool(None).run(PhotoSearchArgs(keywords=["x"]), ctx)
    assert "未設定" in reply
