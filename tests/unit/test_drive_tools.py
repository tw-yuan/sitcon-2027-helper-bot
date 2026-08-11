"""T10：Drive 工具回覆格式（DR-4/5/7/12）與讀取工具（（私）路徑內容僅供判斷、不外流；其餘可引用；
各檔型完整讀取＋offset 續讀＋widened 註記）。"""

from __future__ import annotations

from sitcon_bot.agent.tools.base import ToolContext
from sitcon_bot.agent.tools.drive_tools import (
    DriveListFolderArgs,
    DriveListFolderTool,
    DriveReadDocArgs,
    DriveReadDocTool,
    DriveReadFileArgs,
    DriveReadFileTool,
    DriveReadSheetArgs,
    DriveReadSheetTool,
    DriveSearchArgs,
    DriveSearchTool,
    type_label,
)
from sitcon_bot.services.drive_client import (
    DriveContent,
    DriveFile,
    DriveReadError,
    FolderListing,
    SearchResult,
)

CTX = ToolContext(chat_id=-1, thread_id=None, user_id=1, username="yuan", text="x")

MIME_SHEET = "application/vnd.google-apps.spreadsheet"
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"


class FakeSearchService:
    def __init__(self, result: SearchResult | None = None, listing: FolderListing | Exception | None = None):
        self._result = result
        self._listing = listing
        self.last_call: dict | None = None
        self.list_targets: list[str] = []

    async def search(self, keywords, *, scope_names=None, limit=10, offset=0) -> SearchResult:
        self.last_call = {"keywords": keywords, "scope_names": scope_names, "offset": offset}
        assert self._result is not None
        return self._result

    async def list_folder(self, target: str = "") -> FolderListing:
        self.list_targets.append(target)
        if isinstance(self._listing, Exception):
            raise self._listing
        assert self._listing is not None
        return self._listing


class FakeContentService:
    def __init__(self, content: DriveContent | Exception) -> None:
        self._content = content
        self.calls: list[dict] = []

    async def read(self, **kwargs) -> DriveContent:
        self.calls.append(kwargs)
        if isinstance(self._content, Exception):
            raise self._content
        return self._content


EMPTY = SearchResult(files=[], total=0, offset=0, has_more=False, keywords=[])


async def test_format_results_metadata_only() -> None:
    result = SearchResult(
        files=[
            DriveFile("場地合約.pdf", "SITCON 2027/合約/場地合約.pdf", "https://drive/1", "application/pdf", "f1")
        ],
        total=1, offset=0, has_more=False, keywords=["合約"],
    )
    tool = DriveSearchTool(FakeSearchService(result))
    reply = await tool.run(DriveSearchArgs(keywords=["合約"]), CTX)
    assert "場地合約.pdf" in reply
    assert "SITCON 2027/合約/場地合約.pdf" in reply
    assert "https://drive/1" in reply
    assert "[f1]" in reply  # 檔案 ID 供讀取工具使用
    assert "｜PDF" in reply  # 類型以中文標籤呈現


async def test_no_results_message() -> None:
    result = SearchResult(files=[], total=0, offset=0, has_more=False, keywords=["不存在"])
    tool = DriveSearchTool(FakeSearchService(result))
    reply = await tool.run(DriveSearchArgs(keywords=["不存在"]), CTX)
    assert "找不到" in reply
    assert "不存在" in reply  # DR-7：附上關鍵字
    assert "drive_list_folder" in reply  # 引導改用資料夾瀏覽


async def test_widened_note() -> None:
    result = SearchResult(
        files=[DriveFile("f.pdf", "SITCON 2027/f.pdf", "u", "application/pdf", "f1")],
        total=1, offset=0, has_more=False, keywords=["a", "b"], widened=True,
    )
    reply = await DriveSearchTool(FakeSearchService(result)).run(DriveSearchArgs(keywords=["a", "b"]), CTX)
    assert "放寬" in reply


async def test_has_more_note() -> None:
    files = [DriveFile(f"f{i}.pdf", f"SITCON 2027/f{i}", f"u{i}") for i in range(10)]
    result = SearchResult(files=files, total=25, offset=0, has_more=True, keywords=["f"])
    tool = DriveSearchTool(FakeSearchService(result))
    reply = await tool.run(DriveSearchArgs(keywords=["f"]), CTX)
    assert "共 25 筆" in reply
    assert "下一批" in reply


async def test_scope_passed_through() -> None:
    service = FakeSearchService(EMPTY)
    tool = DriveSearchTool(service)
    await tool.run(DriveSearchArgs(keywords=["合約"], scope=["SITCON 2026"], offset=10), CTX)
    assert service.last_call["scope_names"] == ["SITCON 2026"]
    assert service.last_call["offset"] == 10


async def test_service_not_configured() -> None:
    tool = DriveSearchTool(None)
    reply = await tool.run(DriveSearchArgs(keywords=["x"]), CTX)
    assert "未設定" in reply


def test_type_label_shortcut_and_media() -> None:
    assert type_label(MIME_SHEET) == "Google 試算表"
    assert type_label(SHORTCUT, FOLDER) == "捷徑→資料夾"
    assert type_label("image/jpeg") == "圖片"


# --------------------------------------------------------------------------- #
# drive_list_folder（DR-12）
# --------------------------------------------------------------------------- #
async def test_list_folder_formats_folders_then_files() -> None:
    listing = FolderListing(
        path="SITCON 2027/場務組",
        folder_id="x",
        folders=[DriveFile("物流股", "SITCON 2027/場務組/物流股", "", FOLDER, "sub1")],
        files=[DriveFile("細流.gdoc", "SITCON 2027/場務組/細流.gdoc", "u1",
                         "application/vnd.google-apps.document", "f9")],
    )
    service = FakeSearchService(listing=listing)
    reply = await DriveListFolderTool(service).run(DriveListFolderArgs(target="x"), CTX)
    assert service.list_targets == ["x"]
    assert "SITCON 2027/場務組" in reply
    assert "[sub1]｜📁 物流股" in reply
    assert "[f9]" in reply and "Google 文件" in reply
    assert "<external_data>" in reply


async def test_list_folder_private_path_reminder() -> None:
    listing = FolderListing(
        path="SITCON 2027/行政組（私）", folder_id="p",
        folders=[], files=[DriveFile("薪資.gsheet", "SITCON 2027/行政組（私）/薪資.gsheet", "u", MIME_SHEET, "f1")],
    )
    reply = await DriveListFolderTool(FakeSearchService(listing=listing)).run(DriveListFolderArgs(target="p"), CTX)
    assert "（私）路徑" in reply


async def test_list_folder_error_passthrough() -> None:
    service = FakeSearchService(listing=DriveReadError("這個資料夾不在可搜尋的範圍資料夾內。"))
    reply = await DriveListFolderTool(service).run(DriveListFolderArgs(target="z"), CTX)
    assert "不在可搜尋的範圍" in reply


async def test_list_folder_empty() -> None:
    listing = FolderListing(path="SITCON 2027/空的", folder_id="e", folders=[], files=[])
    reply = await DriveListFolderTool(FakeSearchService(listing=listing)).run(DriveListFolderArgs(target="e"), CTX)
    assert "是空的" in reply


# --------------------------------------------------------------------------- #
# 讀取工具：（私）內容只給 LLM 判斷用；非（私）可引用（DR-4 2026-08-03 修訂）
# --------------------------------------------------------------------------- #
def _content_tool(content) -> tuple[DriveReadFileTool, FakeContentService]:
    service = FakeContentService(content)
    return DriveReadFileTool(service), service


async def test_read_file_normal_content_is_shareable() -> None:
    content = DriveContent(
        file=DriveFile("場地評估.gdoc", "SITCON 2027/合約/場地評估.gdoc", "https://drive/1", None, "d1"),
        text="租金 12 萬",
        kind="Google 文件",
    )
    tool, service = _content_tool(content)
    reply = await tool.run(DriveReadFileArgs(file_id=" d1 "), CTX)
    assert service.calls == [{"file_id": "d1", "offset": 0}]  # 前後空白已清掉
    assert "租金 12 萬" in reply
    assert "<external_data>" in reply  # NFR-6：內容視為資料非指令
    assert "可正常引用" in reply  # 非（私）→ 可寫給使用者
    assert "不得轉述" not in reply
    assert "https://drive/1" in reply
    assert "Google 文件" in reply


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


async def test_read_file_truncation_and_offset_hint() -> None:
    content = DriveContent(
        file=DriveFile("長文.gdoc", "SITCON 2027/長文.gdoc", "u", None, "d1"),
        text="字" * 10, truncated=True, offset=0, total_len=25,
    )
    tool, _ = _content_tool(content)
    reply = await tool.run(DriveReadFileArgs(file_id="d1"), CTX)
    assert "顯示第 1–10 字" in reply
    assert "offset=10" in reply  # 續讀指引
    assert "共 25 字" in reply


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


async def test_read_sheet_passes_worksheet_and_range() -> None:
    content = DriveContent(
        file=DriveFile("預算.gsheet", "SITCON 2027/預算.gsheet", "u", MIME_SHEET, "s1"), text="…"
    )
    service = FakeContentService(content)
    tool = DriveReadSheetTool(service)
    await tool.run(DriveReadSheetArgs(file_id="s1", worksheet="總表", cell_range="A1:C9", offset=5), CTX)
    assert service.calls == [{"file_id": "s1", "worksheet": "總表", "cell_range": "A1:C9", "offset": 5}]


async def test_read_doc_passes_tab() -> None:
    content = DriveContent(
        file=DriveFile("企劃.gdoc", "SITCON 2027/企劃.gdoc", "u", None, "d9"), text="…"
    )
    service = FakeContentService(content)
    tool = DriveReadDocTool(service)
    await tool.run(DriveReadDocArgs(file_id="d9", tab="預算"), CTX)
    assert service.calls == [{"file_id": "d9", "tab": "預算", "offset": 0}]
