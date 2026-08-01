"""T11：HackMD 工具——年度根限縮、確認後的歸檔規則、子資料夾自動建、tag 合併、搜尋/讀取/編輯。"""

from __future__ import annotations

from typing import Any

from sitcon_bot.agent.tools.base import ToolContext
from sitcon_bot.agent.tools.hackmd_tools import (
    CreateNoteArgs,
    GetNoteArgs,
    HackmdCreateNoteTool,
    HackmdGetNoteTool,
    HackmdMoveNoteTool,
    HackmdSearchNotesTool,
    HackmdUpdateNoteTool,
    MoveNoteArgs,
    SearchNotesArgs,
    UpdateNoteArgs,
    merge_tags,
)
from sitcon_bot.domain.templates import TemplateStore
from sitcon_bot.services.hackmd_client import Folder, Note, NoteMeta

CTX = ToolContext(chat_id=-1, thread_id=None, user_id=1, username="yuan", text="x")
TEMPLATES = TemplateStore(
    {
        "summit": (
            "---\ntitle: {{title}}\ntags: 會議記錄, SITCON 2027, SITCON, 2027\n---\n"
            "# {{title}}\n{{meeting_type}} {{date}} {{location}}"
        ),
        "team": (
            "---\ntitle: {{title}}\ntags: 會議記錄, SITCON 2027, SITCON, 2027, {{team}}\n---\n"
            "# {{title}}\n{{team}}"
        ),
    }
)
YEAR = "SITCON 2027"


class FakeHackMD:
    def __init__(self, folders: list[Folder] | None = None, notes: list[NoteMeta] | None = None) -> None:
        self.default_read_perm = "signed_in"
        self.default_write_perm = "signed_in"
        self._folders = folders or []
        self._notes = notes or []
        self.created: list[dict[str, Any]] = []
        self.created_folders: list[Folder] = []
        self.updated: list[dict[str, Any]] = []
        self.moved: list[dict[str, Any]] = []
        self.note_contents: dict[str, str] = {}
        self._n = 0

    async def find_folder(self, name: str, parent_id: str | None = None) -> Folder | None:
        for f in self._folders:
            if f.name == name and f.parent_folder_id == parent_id:  # 精確 parent（None=頂層）
                return f
        return None

    async def ensure_meeting_subfolder(self, team_folder_id: str, subfolder_name: str) -> Folder:
        existing = await self.find_folder(subfolder_name, parent_id=team_folder_id)
        if existing:
            return existing
        self._n += 1
        newf = Folder(id=f"sub{self._n}", name=subfolder_name, parent_folder_id=team_folder_id)
        self._folders.append(newf)
        self.created_folders.append(newf)
        return newf

    async def create_note(
        self, *, title, content, tags, parent_folder_id=None, read_perm=None, write_perm=None
    ) -> Note:
        self.created.append({"title": title, "content": content, "tags": tags, "parent": parent_folder_id})
        return Note(id="n1", title=title, content=content, tags=tags, url="https://hackmd.io/n1")

    async def list_notes(self) -> list[NoteMeta]:
        return self._notes

    async def get_note(self, note_id: str) -> Note:
        content = self.note_contents.get(note_id, "內文")
        return Note(id=note_id, title="某筆記", content=content, tags=["SITCON 2027"], url="u")

    async def update_note(self, note_id: str, *, content=None, tags=None) -> None:
        self.updated.append({"id": note_id, "content": content, "tags": tags})

    async def move_note(self, note_id: str, parent_folder_id: str) -> None:
        self.moved.append({"id": note_id, "parent": parent_folder_id})


def _tool(folders: list[Folder] | None = None) -> tuple[HackmdCreateNoteTool, FakeHackMD]:
    hm = FakeHackMD(folders=folders)
    return HackmdCreateNoteTool(hm, TEMPLATES, YEAR, "會議文件", "會議文件", "Asia/Taipei"), hm


def _year() -> Folder:
    return Folder(id="year", name=YEAR, parent_folder_id=None)


# ------------------------------------------------------------------ #
# merge_tags
# ------------------------------------------------------------------ #
def test_merge_tags_into_frontmatter() -> None:
    content = "---\ntitle: t\ntags: 會議記錄, SITCON 2027\n---\n# body"
    new, tags = merge_tags(content, ["SITCON 2027", "會議文件", "籌會"])
    assert tags == ["會議記錄", "SITCON 2027", "會議文件", "籌會"]  # 既有在前、去重
    assert "tags: 會議記錄, SITCON 2027, 會議文件, 籌會" in new


def test_merge_tags_no_frontmatter() -> None:
    new, tags = merge_tags("純內容無 frontmatter", ["SITCON 2027", "開發組"])
    assert new == "純內容無 frontmatter"
    assert tags == ["SITCON 2027", "開發組"]


# ------------------------------------------------------------------ #
# 一般筆記（HM-1）：<年度根>/<組別>
# ------------------------------------------------------------------ #
async def test_general_in_team_folder_under_year() -> None:
    tool, hm = _tool([_year(), Folder(id="dev", name="開發組", parent_folder_id="year")])
    reply = await tool.run(CreateNoteArgs(kind="general", team="開發組", title="架構筆記", content="內容"), CTX)
    assert "已建立筆記" in reply
    assert hm.created[0]["parent"] == "dev"
    assert hm.created[0]["tags"] == ["SITCON 2027", "SITCON", "2027", "開發組"]


async def test_general_no_team_folder_uses_year_root() -> None:
    tool, hm = _tool([_year()])  # 年度根有、組別資料夾無
    reply = await tool.run(CreateNoteArgs(kind="general", team="開發組", title="X"), CTX)
    assert "找不到「開發組」" in reply
    assert hm.created[0]["parent"] == "year"  # 放年度根（HM-9：組別資料夾不建立）
    assert hm.created_folders == []


async def test_general_no_year_folder_uses_team_root() -> None:
    tool, hm = _tool([])  # 連年度根都沒有
    reply = await tool.run(CreateNoteArgs(kind="general", team="開發組", title="X"), CTX)
    assert "找不到「SITCON 2027」年度資料夾" in reply
    assert hm.created[0]["parent"] is None


async def test_general_requires_title() -> None:
    tool, hm = _tool([_year()])
    reply = await tool.run(CreateNoteArgs(kind="general", team="開發組"), CTX)
    assert "標題" in reply
    assert hm.created == []


# ------------------------------------------------------------------ #
# 大籌/站立（HM-2）：<年度根>/會議文件
# ------------------------------------------------------------------ #
async def test_summit_into_meeting_folder() -> None:
    tool, hm = _tool([_year(), Folder(id="mf", name="會議文件", parent_folder_id="year")])
    reply = await tool.run(
        CreateNoteArgs(kind="summit", meeting_name="一籌", meeting_type="籌會", date="2027-09-13"), CTX
    )
    assert "已建立筆記" in reply
    c = hm.created[0]
    assert c["title"] == "0913 一籌"
    assert c["parent"] == "mf"
    # tag：模板 frontmatter + 自動 tag 合併（三個必加 base tag 都在）
    assert set(c["tags"]) >= {"會議記錄", "SITCON 2027", "SITCON", "2027", "會議文件", "籌會", "0913籌會"}
    assert "0913籌會" in c["content"]  # 併入 frontmatter


async def test_summit_autocreates_meeting_folder_when_missing() -> None:
    tool, hm = _tool([_year()])  # 會議文件 尚未建
    await tool.run(CreateNoteArgs(kind="summit", meeting_type="站立會議", date="0110"), CTX)
    assert hm.created_folders and hm.created_folders[0].name == "會議文件"
    c = hm.created[0]
    assert c["title"] == "0110 站立會議"
    assert "0110站立會議" in c["tags"]


# ------------------------------------------------------------------ #
# 組會（HM-3/HM-9）：<年度根>/<組別>/會議文件
# ------------------------------------------------------------------ #
async def test_team_meeting_creates_subfolder_under_team() -> None:
    tool, hm = _tool([_year(), Folder(id="admin", name="行政組", parent_folder_id="year")])
    reply = await tool.run(CreateNoteArgs(kind="team_meeting", team="行政組", date="0110"), CTX)
    assert "已建立筆記" in reply
    assert hm.created_folders and hm.created_folders[0].name == "會議文件"
    assert hm.created_folders[0].parent_folder_id == "admin"  # 建在組別資料夾底下
    c = hm.created[0]
    assert c["parent"] == hm.created_folders[0].id
    assert c["title"] == "0110 行政組會議"
    assert set(c["tags"]) >= {"SITCON 2027", "行政組", "會議文件"}


async def test_team_meeting_no_team_folder_uses_year_root() -> None:
    tool, hm = _tool([_year()])  # 組別資料夾不存在
    reply = await tool.run(CreateNoteArgs(kind="team_meeting", team="行政組", date="0110"), CTX)
    assert "找不到「行政組」" in reply
    assert hm.created_folders == []  # 組別資料夾本身不建立（HM-9）
    assert hm.created[0]["parent"] == "year"


async def test_team_meeting_requires_team() -> None:
    tool, hm = _tool([_year()])
    reply = await tool.run(CreateNoteArgs(kind="team_meeting", date="0110"), CTX)
    assert "哪一組" in reply
    assert hm.created == []


# ------------------------------------------------------------------ #
# 搜尋（HM-10~12）
# ------------------------------------------------------------------ #
def _search(notes: list[NoteMeta], search_folders: list[str] | None = None) -> HackmdSearchNotesTool:
    return HackmdSearchNotesTool(
        FakeHackMD(notes=notes), TEMPLATES, YEAR, "會議文件", "會議文件", "Asia/Taipei", search_folders
    )


async def test_search_no_hit() -> None:
    reply = await _search([NoteMeta(id="n1", title="其他", tags=[])]).run(
        SearchNotesArgs(title_keywords=["贊助"]), CTX
    )
    assert "找不到" in reply


async def test_search_single_hit() -> None:
    reply = await _search([NoteMeta(id="n1", title="贊助方案討論", tags=["SITCON 2027"], url="u1")]).run(
        SearchNotesArgs(title_keywords=["贊助"]), CTX
    )
    assert "贊助方案討論" in reply  # 標題包進 <external_data>（NFR-6）
    assert "<external_data>" in reply


async def test_search_multi_hits() -> None:
    notes = [NoteMeta(id=f"n{i}", title=f"贊助方案 {i}", tags=[], url=f"u{i}") for i in range(3)]
    reply = await _search(notes).run(SearchNotesArgs(title_keywords=["贊助"]), CTX)
    assert "命中 3 筆" in reply and "[n0]" in reply


async def test_search_over_ten() -> None:
    notes = [NoteMeta(id=f"n{i}", title=f"會議 {i}", tags=[]) for i in range(12)]
    reply = await _search(notes).run(SearchNotesArgs(title_keywords=["會議"]), CTX)
    assert "縮小" in reply


async def test_search_scoped_to_year_folders() -> None:
    notes = [
        NoteMeta(id="a", title="贊助方案", tags=[], folder="SITCON 2027"),
        NoteMeta(id="b", title="贊助方案", tags=[], folder="SITCON 2026"),
        NoteMeta(id="c", title="贊助方案", tags=[], folder="SITCON 2024"),  # 範圍外年度
        NoteMeta(id="d", title="贊助方案", tags=[], folder=None),  # root 範圍外
    ]
    reply = await _search(notes, ["SITCON 2027", "SITCON 2026"]).run(
        SearchNotesArgs(title_keywords=["贊助"]), CTX
    )
    assert "命中 2 筆" in reply  # 僅 2027/2026
    assert "[a]" in reply and "[b]" in reply
    assert "[c]" not in reply and "[d]" not in reply


# ------------------------------------------------------------------ #
# 移動
# ------------------------------------------------------------------ #
def _move_tool(folders: list[Folder]) -> tuple[HackmdMoveNoteTool, FakeHackMD]:
    hm = FakeHackMD(folders=folders)
    return HackmdMoveNoteTool(hm, TEMPLATES, YEAR, "會議文件", "會議文件", "Asia/Taipei"), hm


async def test_move_to_team_folder() -> None:
    tool, hm = _move_tool([_year(), Folder(id="dev", name="開發組", parent_folder_id="year")])
    reply = await tool.run(MoveNoteArgs(note_id="n1", team="開發組"), CTX)
    assert "已把筆記" in reply and "SITCON 2027/開發組" in reply
    assert hm.moved == [{"id": "n1", "parent": "dev"}]


async def test_move_to_team_meeting_subfolder_autocreates() -> None:
    tool, hm = _move_tool([_year(), Folder(id="dev", name="開發組", parent_folder_id="year")])
    reply = await tool.run(MoveNoteArgs(note_id="n1", team="開發組", meeting_docs=True), CTX)
    assert "SITCON 2027/開發組/會議文件" in reply
    assert hm.created_folders and hm.created_folders[0].parent_folder_id == "dev"  # 子夾自動補建
    assert hm.moved[0]["parent"] == hm.created_folders[0].id


async def test_move_to_year_meeting_folder() -> None:
    tool, hm = _move_tool([_year(), Folder(id="mf", name="會議文件", parent_folder_id="year")])
    reply = await tool.run(MoveNoteArgs(note_id="n1", meeting_docs=True), CTX)
    assert "SITCON 2027/會議文件" in reply
    assert hm.moved == [{"id": "n1", "parent": "mf"}]


async def test_move_missing_team_folder_does_not_move_or_create() -> None:
    tool, hm = _move_tool([_year()])
    reply = await tool.run(MoveNoteArgs(note_id="n1", team="開發組"), CTX)
    assert "找不到「開發組」" in reply and "未移動" in reply
    assert hm.moved == [] and hm.created_folders == []  # 組別資料夾不自動建立


async def test_move_missing_year_root_does_not_move() -> None:
    tool, hm = _move_tool([])
    reply = await tool.run(MoveNoteArgs(note_id="n1", team="開發組"), CTX)
    assert "年度資料夾" in reply and "未移動" in reply
    assert hm.moved == []


async def test_move_without_target_asks() -> None:
    tool, hm = _move_tool([_year()])
    reply = await tool.run(MoveNoteArgs(note_id="n1"), CTX)
    assert "哪個資料夾" in reply
    assert hm.moved == []


# ------------------------------------------------------------------ #
# 讀取 / 編輯
# ------------------------------------------------------------------ #
async def test_get_note_wraps_and_truncates() -> None:
    hm = FakeHackMD()
    hm.note_contents["big"] = "字" * 25000
    tool = HackmdGetNoteTool(hm, TEMPLATES, YEAR, "會議文件", "會議文件", "Asia/Taipei")
    reply = await tool.run(GetNoteArgs(note_id="big"), CTX)
    assert "<external_data>" in reply and "已截斷" in reply


async def test_update_preserves_required_tag() -> None:
    hm = FakeHackMD()
    tool = HackmdUpdateNoteTool(hm, TEMPLATES, YEAR, "會議文件", "會議文件", "Asia/Taipei")
    await tool.run(UpdateNoteArgs(note_id="n1", content="新內文", set_tags=["會議文件"]), CTX)
    assert hm.updated[0]["tags"] == ["SITCON 2027", "SITCON", "2027", "會議文件"]  # HM-15：三個必加 tag 保留


async def test_update_reports_version_hint() -> None:
    hm = FakeHackMD()
    tool = HackmdUpdateNoteTool(hm, TEMPLATES, YEAR, "會議文件", "會議文件", "Asia/Taipei")
    reply = await tool.run(UpdateNoteArgs(note_id="n1", content="加一行"), CTX)
    assert "版本紀錄" in reply
