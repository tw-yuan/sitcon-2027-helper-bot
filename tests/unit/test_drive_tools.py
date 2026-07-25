"""T10：Drive 搜尋工具回覆格式（DR-4/5/7）。"""

from __future__ import annotations

from sitcon_bot.agent.tools.base import ToolContext
from sitcon_bot.agent.tools.drive_tools import DriveSearchArgs, DriveSearchTool
from sitcon_bot.services.drive_client import DriveFile, SearchResult

CTX = ToolContext(chat_id=-1, thread_id=None, user_id=1, username="yuan", text="x")


class FakeService:
    def __init__(self, result: SearchResult) -> None:
        self._result = result
        self.last_call: dict | None = None

    async def search(self, keywords, *, scope_names=None, limit=10, offset=0) -> SearchResult:
        self.last_call = {"keywords": keywords, "scope_names": scope_names, "offset": offset}
        return self._result


async def test_format_results_metadata_only() -> None:
    result = SearchResult(
        files=[DriveFile("場地合約.pdf", "SITCON 2027/合約/場地合約.pdf", "https://drive/1", "application/pdf")],
        total=1, offset=0, has_more=False, keywords=["合約"],
    )
    tool = DriveSearchTool(FakeService(result))
    reply = await tool.run(DriveSearchArgs(keywords=["合約"]), CTX)
    assert "場地合約.pdf" in reply
    assert "SITCON 2027/合約/場地合約.pdf" in reply
    assert "https://drive/1" in reply


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
