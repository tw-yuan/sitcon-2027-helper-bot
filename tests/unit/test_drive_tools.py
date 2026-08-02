"""T10：Drive 工具回覆格式（DR-4/5/7）與 drive_read_file（（私）路徑內容僅供判斷、不外流；其餘可引用）。"""

from __future__ import annotations

from sitcon_bot.agent.tools.base import ToolContext
from sitcon_bot.agent.tools.drive_tools import (
    DriveReadFileArgs,
    DriveReadFileTool,
    DriveSearchArgs,
    DriveSearchTool,
)
from sitcon_bot.services.drive_client import DriveContent, DriveFile, DriveReadError, SearchResult

CTX = ToolContext(chat_id=-1, thread_id=None, user_id=1, username="yuan", text="x")


class FakeService:
    def __init__(self, result: SearchResult, content: DriveContent | Exception | None = None) -> None:
        self._result = result
        self._content = content
        self.last_call: dict | None = None
        self.read_ids: list[str] = []

    async def search(self, keywords, *, scope_names=None, limit=10, offset=0) -> SearchResult:
        self.last_call = {"keywords": keywords, "scope_names": scope_names, "offset": offset}
        return self._result

    async def read_file(self, file_id: str) -> DriveContent:
        self.read_ids.append(file_id)
        if isinstance(self._content, Exception):
            raise self._content
        assert self._content is not None
        return self._content


EMPTY = SearchResult(files=[], total=0, offset=0, has_more=False, keywords=[])


async def test_format_results_metadata_only() -> None:
    result = SearchResult(
        files=[
            DriveFile("場地合約.pdf", "SITCON 2027/合約/場地合約.pdf", "https://drive/1", "application/pdf", "f1")
        ],
        total=1, offset=0, has_more=False, keywords=["合約"],
    )
    tool = DriveSearchTool(FakeService(result))
    reply = await tool.run(DriveSearchArgs(keywords=["合約"]), CTX)
    assert "場地合約.pdf" in reply
    assert "SITCON 2027/合約/場地合約.pdf" in reply
    assert "https://drive/1" in reply
    assert "[f1]" in reply  # 檔案 ID 供 drive_read_file 使用


async def test_no_results_message() -> None:
    result = SearchResult(files=[], total=0, offset=0, has_more=False, keywords=["不存在"])
    tool = DriveSearchTool(FakeService(result))
    reply = await tool.run(DriveSearchArgs(keywords=["不存在"]), CTX)
    assert "找不到" in reply
    assert "不存在" in reply  # DR-7：附上關鍵字


async def test_has_more_note() -> None:
    files = [DriveFile(f"f{i}.pdf", f"SITCON 2027/f{i}", f"u{i}") for i in range(10)]
    result = SearchResult(files=files, total=25, offset=0, has_more=True, keywords=["f"])
    tool = DriveSearchTool(FakeService(result))
    reply = await tool.run(DriveSearchArgs(keywords=["f"]), CTX)
    assert "共 25 筆" in reply
    assert "下一批" in reply


async def test_scope_passed_through() -> None:
    service = FakeService(SearchResult(files=[], total=0, offset=0, has_more=False, keywords=["合約"]))
    tool = DriveSearchTool(service)
    await tool.run(DriveSearchArgs(keywords=["合約"], scope=["SITCON 2026"], offset=10), CTX)
    assert service.last_call["scope_names"] == ["SITCON 2026"]
    assert service.last_call["offset"] == 10


async def test_service_not_configured() -> None:
    tool = DriveSearchTool(None)
    reply = await tool.run(DriveSearchArgs(keywords=["x"]), CTX)
    assert "未設定" in reply


# --------------------------------------------------------------------------- #
# drive_read_file：（私）內容只給 LLM 判斷用；非（私）可引用（DR-4 2026-08-03 修訂）
# --------------------------------------------------------------------------- #
def _content_tool(content) -> tuple[DriveReadFileTool, FakeService]:
    service = FakeService(EMPTY, content)
    return DriveReadFileTool(service), service


async def test_read_file_normal_content_is_shareable() -> None:
    content = DriveContent(
        file=DriveFile("場地評估.gdoc", "SITCON 2027/合約/場地評估.gdoc", "https://drive/1", None, "d1"),
        text="租金 12 萬",
    )
    tool, service = _content_tool(content)
    reply = await tool.run(DriveReadFileArgs(file_id=" d1 "), CTX)
    assert service.read_ids == ["d1"]  # 前後空白已清掉
    assert "租金 12 萬" in reply
    assert "<external_data>" in reply  # NFR-6：內容視為資料非指令
    assert "可正常引用" in reply  # 非（私）→ 可寫給使用者
    assert "不得轉述" not in reply
    assert "https://drive/1" in reply


async def test_read_file_private_path_keeps_internal_only_note() -> None:
    content = DriveContent(
        file=DriveFile("薪資表.gsheet", "SITCON 2027/行政組（私）/薪資表.gsheet", "https://drive/2", None, "d2"),
        text="時薪 300",
        private=True,
    )
    tool, _ = _content_tool(content)
    reply = await tool.run(DriveReadFileArgs(file_id="d2"), CTX)
    assert "【（私）檔案" in reply
    assert "不得轉述" in reply  # 明示不可寫給使用者
    assert "可正常引用" not in reply


async def test_read_file_truncation_note() -> None:
    content = DriveContent(
        file=DriveFile("長文.gdoc", "SITCON 2027/長文.gdoc", "u", None, "d1"), text="字" * 10, truncated=True
    )
    tool, _ = _content_tool(content)
    reply = await tool.run(DriveReadFileArgs(file_id="d1"), CTX)
    assert "截斷" in reply


async def test_read_file_out_of_scope_message() -> None:
    tool, _ = _content_tool(DriveReadError("這個檔案不在可搜尋的範圍資料夾內，不能讀取。"))
    reply = await tool.run(DriveReadFileArgs(file_id="x"), CTX)
    assert "不在可搜尋的範圍" in reply
    assert "<external_data>" not in reply


async def test_read_file_transient_failure_falls_back() -> None:
    tool, _ = _content_tool(RuntimeError("boom"))
    reply = await tool.run(DriveReadFileArgs(file_id="x"), CTX)
    assert "暫時失敗" in reply


async def test_read_file_service_not_configured() -> None:
    reply = await DriveReadFileTool(None).run(DriveReadFileArgs(file_id="x"), CTX)
    assert "未設定" in reply
