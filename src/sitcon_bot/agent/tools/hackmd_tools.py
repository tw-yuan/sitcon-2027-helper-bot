"""HackMD 工具（HM-1～HM-16，已依實際 team 結構調整）。

同一 team 內多年度並存，以頂層「年度資料夾」（HACKMD_YEAR_FOLDER，如「SITCON 2027」）區分。
所有筆記歸檔一律限縮於年度根子樹（避免抓到別年的同名組別資料夾）：
  一般筆記（HM-1）  → <年度根>/<組別>；組別資料夾不存在 → 年度根＋提示
  大籌/站立（HM-2） → <年度根>/會議文件
  組會（HM-3）      → <年度根>/<組別>/會議文件（會議文件子夾自動補建；組別資料夾不建立）

tags：模板 frontmatter 的 tags 與程式自動 tags 合併，兩者皆帶（frontmatter + API tags 欄位）。
搜尋兩階段（HM-10~12）、編輯整份寫回（HM-13~15）、不刪除（HM-16）。
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from ...domain.dates import display_date, to_mmdd
from ...domain.templates import TemplateStore
from ...services.hackmd_client import Folder, HackMDClient, HackMDCredentialError, HackMDError
from .base import Tool, ToolContext
from .external_data import wrap_external

log = logging.getLogger(__name__)

# 所有新建筆記固定帶的 tag（必加、不可經編輯移除）
REQUIRED_BASE_TAGS = ["SITCON 2027", "SITCON", "2027"]
CONTENT_LIMIT = 20000
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def merge_tags(content: str, auto_tags: list[str]) -> tuple[str, list[str]]:
    """把 auto_tags 併入 content frontmatter 的 tags 行；回傳（新 content, 合併後 tags）。

    無 frontmatter 時 content 不變、tags = auto_tags（供 API tags 欄位）。
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return content, list(auto_tags)
    lines = m.group(1).split("\n")
    idx = next((i for i, line in enumerate(lines) if line.strip().lower().startswith("tags:")), None)
    if idx is None:
        merged = list(auto_tags)
        lines.append("tags: " + ", ".join(merged))
    else:
        existing = [t.strip() for t in lines[idx].split(":", 1)[1].split(",") if t.strip()]
        merged = existing + [t for t in auto_tags if t not in existing]
        lines[idx] = "tags: " + ", ".join(merged)
    joined = "\n".join(lines)
    return f"---\n{joined}\n---\n" + content[m.end() :], merged


class _HackMDToolBase(Tool):
    def __init__(
        self,
        client: HackMDClient,
        templates: TemplateStore | None = None,
        year_folder: str = "SITCON 2027",
        meeting_folder: str = "會議文件",
        subfolder: str = "會議文件",
        tz: str = "Asia/Taipei",
        search_folders: list[str] | None = None,
    ) -> None:
        self._hm = client
        self._templates = templates
        self._year_folder = year_folder
        self._meeting_folder = meeting_folder
        self._subfolder = subfolder
        self._tz = tz
        self._search_folders = search_folders or []


# --------------------------------------------------------------------------- #
# 建立筆記
# --------------------------------------------------------------------------- #
class CreateNoteArgs(BaseModel):
    kind: Literal["general", "summit", "team_meeting"] = Field(
        description="general=一般筆記；summit=大籌/站立會議；team_meeting=組會（會議類型由你判斷）"
    )
    team: str | None = Field(None, description="組別名（general/team_meeting 用；general 無法判斷時留空落總召組）")
    meeting_type: str | None = Field(None, description="summit 用：『籌會』或『站立會議』")
    meeting_name: str | None = Field(None, description="summit 用會議名稱（如『一籌』），作為標題")
    title: str | None = Field(None, description="general 用筆記標題")
    content: str | None = Field(None, description="general 用內文")
    date: str | None = Field(None, description="日期 YYYY-MM-DD；未給用今日")
    location: str | None = Field(None, description="會議地點（填入模板 {{location}}）")
    extra_tags: list[str] = Field(default_factory=list, description="額外 tag（附加）")
    read_perm: str | None = Field(None, description="覆蓋讀取權限（owner/signed_in/guest）")
    write_perm: str | None = Field(None, description="覆蓋寫入權限")


class HackmdCreateNoteTool(_HackMDToolBase):
    name = "hackmd_create_note"
    description = (
        "在 HackMD team 的年度資料夾（SITCON 2027）底下建立筆記。一般筆記放組別資料夾並附組別 tag；"
        "大籌/站立會議放『會議文件』、組會放『<組別>/會議文件』（子夾自動補建），皆套用對應模板與自動 tags。"
    )
    args_model = CreateNoteArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, CreateNoteArgs)
        try:
            year_root = await self._hm.find_folder(self._year_folder)  # 頂層年度資料夾
            if args.kind == "summit":
                return await self._create_summit(args, year_root)
            if args.kind == "team_meeting":
                return await self._create_team_meeting(args, year_root)
            return await self._create_general(args, year_root)
        except HackMDCredentialError as exc:
            return str(exc)
        except HackMDError as exc:
            return f"建立筆記失敗：{exc}"

    async def _finish(
        self, title: str, content: str, tags: list[str], folder_id: str | None, args: CreateNoteArgs, hint: str
    ) -> str:
        note = await self._hm.create_note(
            title=title,
            content=content,
            tags=tags,
            parent_folder_id=folder_id,
            read_perm=args.read_perm,
            write_perm=args.write_perm,
        )
        parts = [f"✅ 已建立筆記「{note.title}」", f"tags：{'、'.join(note.tags)}", note.url]
        if hint:
            parts.append(hint)
        return "\n".join(p for p in parts if p)

    def _no_year_hint(self) -> str:
        return f"（找不到「{self._year_folder}」年度資料夾，已放 team root）"

    async def _create_summit(self, args: CreateNoteArgs, year_root: Folder | None) -> str:
        mtype = "站立會議" if (args.meeting_type and "站立" in args.meeting_type) else "籌會"
        mmdd = to_mmdd(args.date, self._tz)
        title = f"{mmdd} {args.meeting_name or mtype}"
        auto = [*REQUIRED_BASE_TAGS, "會議文件", mtype, f"{mmdd}{mtype}", *args.extra_tags]
        rendered = self._render(
            "summit", title=title, date=display_date(args.date, self._tz), meeting_type=mtype,
            location=args.location or "",
        )
        content, tags = merge_tags(rendered, auto)
        if year_root is None:
            return await self._finish(title, content, tags, None, args, self._no_year_hint())
        mf = await self._hm.ensure_meeting_subfolder(year_root.id, self._meeting_folder)  # <年度根>/會議文件
        return await self._finish(title, content, tags, mf.id, args, "")

    async def _create_team_meeting(self, args: CreateNoteArgs, year_root: Folder | None) -> str:
        if not args.team:
            return "請指定是哪一組的會議。"
        mmdd = to_mmdd(args.date, self._tz)
        title = f"{mmdd} {args.team}會議"
        auto = [*REQUIRED_BASE_TAGS, args.team, "會議文件", *args.extra_tags]
        rendered = self._render(
            "team", title=title, date=display_date(args.date, self._tz), team=args.team, location=args.location or ""
        )
        content, tags = merge_tags(rendered, auto)
        if year_root is None:
            return await self._finish(title, content, tags, None, args, self._no_year_hint())
        team_folder = await self._hm.find_folder(args.team, parent_id=year_root.id)
        if team_folder is None:  # 組別資料夾不建立（HM-9）
            hint = f"（{self._year_folder} 下找不到「{args.team}」資料夾，已放 {self._year_folder} 根）"
            return await self._finish(title, content, tags, year_root.id, args, hint)
        sub = await self._hm.ensure_meeting_subfolder(team_folder.id, self._subfolder)  # <組別>/會議文件 自動補建
        return await self._finish(title, content, tags, sub.id, args, "")

    async def _create_general(self, args: CreateNoteArgs, year_root: Folder | None) -> str:
        if not args.title:
            return "請提供筆記標題。"
        team = args.team or "總召組"
        content, tags = merge_tags(args.content or "", [*REQUIRED_BASE_TAGS, team, *args.extra_tags])
        if year_root is None:
            return await self._finish(args.title, content, tags, None, args, self._no_year_hint())
        team_folder = await self._hm.find_folder(team, parent_id=year_root.id)
        if team_folder is None:
            hint = f"（{self._year_folder} 下找不到「{team}」資料夾，已放 {self._year_folder} 根）"
            return await self._finish(args.title, content, tags, year_root.id, args, hint)
        return await self._finish(args.title, content, tags, team_folder.id, args, "")

    def _render(self, kind: str, **kw: str) -> str:
        if self._templates is None:
            return f"# {kw.get('title', '')}\n\n- 日期：{kw.get('date', '')}\n"
        return self._templates.render(kind, **kw)


# --------------------------------------------------------------------------- #
# 搜尋
# --------------------------------------------------------------------------- #
class SearchNotesArgs(BaseModel):
    title_keywords: list[str] = Field(default_factory=list, description="標題關鍵字")
    tags: list[str] = Field(default_factory=list, description="需包含的 tag")


class HackmdSearchNotesTool(_HackMDToolBase):
    name = "hackmd_search_notes"
    description = (
        "在 SITCON 2026／2027 的筆記中搜尋（以標題與 tag 過濾）。命中 2～10 筆時可再用 "
        "hackmd_get_note 讀候選內文挑選最符合者。"
    )
    args_model = SearchNotesArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, SearchNotesArgs)
        try:
            notes = await self._hm.list_notes()
        except HackMDCredentialError as exc:
            return str(exc)
        except HackMDError as exc:
            return f"搜尋失敗：{exc}"

        title_kws = [k.strip().lower() for k in args.title_keywords if k.strip()]
        tag_kws = [t.strip() for t in args.tags if t.strip()]
        scope = set(self._search_folders)  # 限縮於年度資料夾（如 2026/2027）；空集合表示不限縮

        def match(n: object) -> bool:
            if scope and getattr(n, "folder", None) not in scope:
                return False  # 限縮搜尋範圍於指定年度資料夾
            title = getattr(n, "title", "").lower()
            tags = getattr(n, "tags", [])
            if title_kws and not all(k in title for k in title_kws):
                return False
            return not (tag_kws and not all(t in tags for t in tag_kws))

        hits = [n for n in notes if match(n)]
        kws = "、".join(args.title_keywords + args.tags) or "（無）"
        if not hits:
            return f"找不到符合的筆記（關鍵字：{kws}）。可換個關鍵字。"
        # 筆記 id 與 url 為受信任識別資訊留在圍欄外（供 LLM 據以讀取）；標題與 tags 為 team 任一成員
        # 可寫的自由文字，包進 <external_data>（NFR-6）。
        if len(hits) == 1:
            n = hits[0]
            return f"命中 [{n.id}]｜{n.url}\n" + wrap_external(f"標題：{n.title}｜tags：{'、'.join(n.tags)}")

        shown = hits[:10]
        lines = [
            f"- [{n.id}]｜{n.url}\n  " + wrap_external(f"標題：{n.title}｜tags：{'、'.join(n.tags)}") for n in shown
        ]
        if len(hits) > 10:
            return f"命中 {len(hits)} 筆（>10），請縮小條件。前 10 筆：\n" + "\n".join(lines)  # HM-12
        return f"命中 {len(hits)} 筆，可讀內文挑選：\n" + "\n".join(lines)  # HM-11


# --------------------------------------------------------------------------- #
# 讀取
# --------------------------------------------------------------------------- #
class GetNoteArgs(BaseModel):
    note_id: str


class HackmdGetNoteTool(_HackMDToolBase):
    name = "hackmd_get_note"
    description = "讀取指定 HackMD 筆記的標題、tags 與內文（供挑選或編輯）。"
    args_model = GetNoteArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, GetNoteArgs)
        try:
            note = await self._hm.get_note(args.note_id)
        except HackMDCredentialError as exc:
            return str(exc)
        except HackMDError as exc:
            return f"讀取失敗：{exc}"
        content = note.content
        if len(content) > CONTENT_LIMIT:
            content = content[:CONTENT_LIMIT] + "\n…（內文過長已截斷）"
        # 標題、tags、內文皆為 team 任一成員可寫的外部內容 → 全部包進 <external_data>；url 留在外供操作。
        return f"{note.url}\n" + wrap_external(
            f"標題：{note.title}｜tags：{'、'.join(note.tags)}\n內文：\n{content}"
        )


# --------------------------------------------------------------------------- #
# 編輯
# --------------------------------------------------------------------------- #
class UpdateNoteArgs(BaseModel):
    note_id: str
    content: str | None = Field(None, description="修改後的完整內文（整份寫回）")
    set_tags: list[str] | None = Field(None, description="覆蓋 tags（必加 SITCON 2027 會自動保留）")


class HackmdUpdateNoteTool(_HackMDToolBase):
    name = "hackmd_update_note"
    description = (
        "編輯既有 HackMD 筆記：整份內文寫回、或更新 tags。必加 tag「SITCON 2027／SITCON／2027」不會被移除。不刪除筆記。"
    )
    args_model = UpdateNoteArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, UpdateNoteArgs)
        tags = args.set_tags
        if tags is not None:
            missing = [t for t in REQUIRED_BASE_TAGS if t not in tags]
            tags = [*missing, *tags]  # HM-15：必加 tag 不可移除
        try:
            await self._hm.update_note(args.note_id, content=args.content, tags=tags)
            note = await self._hm.get_note(args.note_id)
        except HackMDCredentialError as exc:
            return str(exc)
        except HackMDError as exc:
            return f"編輯失敗：{exc}"
        return f"✅ 已更新筆記「{note.title}」\n{note.url}\n（HackMD 保有版本紀錄，可回溯）"


def build_hackmd_tools(
    client: HackMDClient,
    templates: TemplateStore | None,
    year_folder: str,
    meeting_folder: str,
    subfolder: str,
    tz: str,
    search_folders: list[str] | None = None,
) -> list[Tool]:
    args = (client, templates, year_folder, meeting_folder, subfolder, tz, search_folders)
    return [
        HackmdCreateNoteTool(*args),
        HackmdSearchNotesTool(*args),
        HackmdGetNoteTool(*args),
        HackmdUpdateNoteTool(*args),
    ]
