"""T10：Drive 唯讀搜尋——範圍過濾、路徑組裝、搜尋僅回 metadata、限縮、分頁、query；
2026-08-11 修訂：AND→OR 放寬、排名、捷徑虛擬根、資料夾瀏覽（list_folder）、
讀取目標解析（resolve_for_read：範圍檢查＋捷徑跟隨＋（私）判定）。"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from sitcon_bot.services.drive_client import (
    DriveFile,
    DriveReadError,
    DriveSearchService,
    build_search_query,
    content_mode,
)

# 範圍資料夾（名稱 → id）
SCOPE = {"SITCON 2027": "s27", "SITCON 2026": "s26"}

# 資料夾樹（隨查隨解時以 get_folder 沿 parents 上溯）：
#   s27 └ 合約(c1) └ 場地(v1)
#   s26 └ 舊合約(c2)
#   o1(其他) 的 parents 空 → 走不到任何範圍根 → 範圍外
#   x1(外部共用硬碟資料夾) 只能靠捷徑 sc1 連進 s26 → 虛擬根
FOLDERS_META: dict[str, dict[str, Any]] = {
    "s27": {"id": "s27", "name": "SITCON 2027", "parents": []},
    "s26": {"id": "s26", "name": "SITCON 2026", "parents": []},
    "c1": {"id": "c1", "name": "合約", "parents": ["s27"]},
    "v1": {"id": "v1", "name": "場地", "parents": ["c1"]},
    "c2": {"id": "c2", "name": "舊合約", "parents": ["s26"]},
    "o1": {"id": "o1", "name": "其他", "parents": []},
    "p1": {"id": "p1", "name": "行政組（私）", "parents": ["s27"]},
    "x1": {"id": "x1", "name": "2026 議程組", "parents": ["ext-root"]},
}

FILES = [
    {"id": "f1", "name": "場地租借合約.pdf", "parents": ["v1"], "mimeType": "application/pdf", "webViewLink": "u1"},
    {"id": "f2", "name": "去年場地合約.pdf", "parents": ["c2"], "mimeType": "application/pdf", "webViewLink": "u2"},
    {"id": "f3", "name": "無關合約.pdf", "parents": ["o1"], "mimeType": "application/pdf", "webViewLink": "u3"},
    {"id": "f4", "name": "SITCON2027簡介.pdf", "parents": ["s27"], "mimeType": "application/pdf", "webViewLink": "u4"},
]

SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FOLDER_MIME = "application/vnd.google-apps.folder"


class FakeDriveGateway:
    def __init__(
        self,
        folders_meta: dict[str, dict[str, Any]],
        files: list[dict[str, Any]],
        shortcuts: list[dict[str, Any]] | None = None,
        children: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._meta = folders_meta
        self._files = files
        self._shortcuts = shortcuts or []
        self._children = children or {}
        self.queries: list[str] = []
        self.get_calls: list[str] = []
        self.fetch_calls: list[tuple[str, str | None]] = []
        self.content = "檔案內文"

    async def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        self.get_calls.append(folder_id)
        return self._meta.get(folder_id)

    async def search_files(self, query: str) -> list[dict[str, Any]]:
        if SHORTCUT_MIME in query:  # _ensure_aliases 的捷徑清單查詢
            return list(self._shortcuts)
        self.queries.append(query)
        return list(self._files)

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        for f in [*self._files, *self._shortcuts]:
            if f["id"] == file_id:
                return f
        meta = self._meta.get(file_id)  # 真實 API 的 files.get 對資料夾同樣有效
        if meta is not None:
            return {**meta, "mimeType": FOLDER_MIME}
        return None

    async def list_children(self, folder_id: str) -> list[dict[str, Any]]:
        return list(self._children.get(folder_id, []))

    async def fetch_text(self, file_id: str, export_mime: str | None) -> str:
        self.fetch_calls.append((file_id, export_mime))
        return self.content

    @property
    def last_query(self) -> str | None:
        return self.queries[-1] if self.queries else None


def _service(meta=FOLDERS_META, files=FILES, **kw) -> DriveSearchService:
    return DriveSearchService(FakeDriveGateway(meta, files, **kw), SCOPE, ttl_seconds=1800)


async def test_scope_filtering_excludes_out_of_scope() -> None:
    result = await _service().search(["合約"])
    names = {f.name for f in result.files}
    assert "無關合約.pdf" not in names  # 範圍外 o1 排除
    assert names == {"場地租借合約.pdf", "去年場地合約.pdf", "SITCON2027簡介.pdf"}
    assert result.total == 3


async def test_path_assembly_from_scope_root() -> None:
    paths = {f.name: f.path for f in (await _service().search(["合約"])).files}
    assert paths["場地租借合約.pdf"] == "SITCON 2027/合約/場地/場地租借合約.pdf"
    assert paths["去年場地合約.pdf"] == "SITCON 2026/舊合約/去年場地合約.pdf"
    assert paths["SITCON2027簡介.pdf"] == "SITCON 2027/SITCON2027簡介.pdf"


async def test_dr4_search_result_is_metadata_only() -> None:
    """搜尋結果本身仍只有 metadata（內容要另外呼叫讀取工具才拿得到）。"""
    expected = {"name", "path", "url", "mime", "file_id", "modified", "target_mime"}
    for f in (await _service().search(["合約"])).files:
        assert set(dataclasses.asdict(f).keys()) == expected
    assert not hasattr(DriveFile("n", "p", "u"), "content")


async def test_search_result_carries_file_id() -> None:
    ids = {f.name: f.file_id for f in (await _service().search(["合約"])).files}
    assert ids["場地租借合約.pdf"] == "f1"


async def test_dr2_narrow_to_last_year() -> None:
    result = await _service().search(["合約"], scope_names=["SITCON 2026"])
    assert {f.name for f in result.files} == {"去年場地合約.pdf"}


async def test_dr5_paging_more() -> None:
    many = [
        {"id": f"m{i}", "name": f"檔{i}.pdf", "parents": ["v1"], "mimeType": "application/pdf", "webViewLink": f"u{i}"}
        for i in range(12)
    ]
    result = await _service(files=many).search(["檔"])
    assert result.total == 12
    assert len(result.files) == 10
    assert result.has_more is True
    nxt = await _service(files=many).search(["檔"], offset=10)
    assert len(nxt.files) == 2 and nxt.has_more is False


async def test_dr7_no_results() -> None:
    result = await _service(files=[]).search(["不存在"])
    assert result.total == 0 and result.files == []


# --------------------------------------------------------------------------- #
# 2026-08-11 修訂：AND→OR 放寬與排名
# --------------------------------------------------------------------------- #
class TwoPhaseGateway(FakeDriveGateway):
    """AND 查詢回空、OR 查詢回結果——模擬全部符合 0 筆後的放寬重查。"""

    async def search_files(self, query: str) -> list[dict[str, Any]]:
        if SHORTCUT_MIME in query:
            return list(self._shortcuts)
        self.queries.append(query)
        return list(self._files) if ") or (" in query else []


async def test_widen_to_any_keyword_when_all_match_empty() -> None:
    gw = TwoPhaseGateway(FOLDERS_META, FILES)
    svc = DriveSearchService(gw, SCOPE, ttl_seconds=1800)
    result = await svc.search(["場地", "沒有的詞"])
    assert result.widened is True
    assert result.total > 0
    assert len(gw.queries) == 2
    assert ") or (" in gw.queries[1]  # 第二次為 OR 查詢


async def test_no_widen_for_single_keyword() -> None:
    gw = FakeDriveGateway(FOLDERS_META, [])
    svc = DriveSearchService(gw, SCOPE, ttl_seconds=1800)
    result = await svc.search(["找不到"])
    assert result.widened is False
    assert len(gw.queries) == 1  # 單關鍵字不重查


async def test_ranking_name_hits_then_modified() -> None:
    files = [
        {"id": "a", "name": "無關.pdf", "parents": ["s27"], "mimeType": "application/pdf",
         "webViewLink": "u", "modifiedTime": "2026-08-01T00:00:00Z"},
        {"id": "b", "name": "場佈手冊(舊).pdf", "parents": ["s27"], "mimeType": "application/pdf",
         "webViewLink": "u", "modifiedTime": "2025-01-01T00:00:00Z"},
        {"id": "c", "name": "場佈手冊.pdf", "parents": ["s27"], "mimeType": "application/pdf",
         "webViewLink": "u", "modifiedTime": "2026-07-01T00:00:00Z"},
    ]
    result = await _service(files=files).search(["場佈"])
    assert [f.file_id for f in result.files] == ["c", "b", "a"]  # 檔名命中在前，新→舊


# --------------------------------------------------------------------------- #
# 捷徑虛擬根：範圍內的資料夾捷徑，其目標子樹視同範圍內
# --------------------------------------------------------------------------- #
FOLDER_SHORTCUT = {
    "id": "sc1",
    "name": "z. 2026 議程組",
    "parents": ["s26"],
    "mimeType": SHORTCUT_MIME,
    "webViewLink": "usc",
    "shortcutDetails": {"targetId": "x1", "targetMimeType": FOLDER_MIME},
}
EXT_FILE = {
    "id": "xf1", "name": "議程表.gdoc", "parents": ["x1"],
    "mimeType": "application/vnd.google-apps.document", "webViewLink": "ux",
}


async def test_shortcut_alias_includes_external_subtree_in_search() -> None:
    svc = _service(files=[*FILES, EXT_FILE], shortcuts=[FOLDER_SHORTCUT])
    result = await svc.search(["議程"])
    paths = {f.name: f.path for f in result.files}
    assert paths["議程表.gdoc"] == "SITCON 2026/z. 2026 議程組/議程表.gdoc"


async def test_shortcut_alias_respects_scope_narrowing() -> None:
    svc = _service(files=[EXT_FILE], shortcuts=[FOLDER_SHORTCUT])
    result = await svc.search(["議程"], scope_names=["SITCON 2027"])
    assert result.total == 0  # 捷徑掛在 2026，縮到 2027 就不算


# --------------------------------------------------------------------------- #
# list_folder（DR-12）
# --------------------------------------------------------------------------- #
CHILDREN = {
    "s27": [
        {"id": "c1", "name": "合約", "mimeType": FOLDER_MIME},
        {"id": "f4", "name": "SITCON2027簡介.pdf", "mimeType": "application/pdf", "webViewLink": "u4"},
    ],
    "x1": [EXT_FILE],
}


async def test_list_folder_empty_target_lists_roots() -> None:
    listing = await _service().list_folder("")
    assert {f.name for f in listing.folders} == {"SITCON 2027", "SITCON 2026"}


async def test_list_folder_by_year_name() -> None:
    svc = _service(children=CHILDREN)
    listing = await svc.list_folder("SITCON 2027")
    assert listing.path == "SITCON 2027"
    assert [f.name for f in listing.folders] == ["合約"]
    assert [f.name for f in listing.files] == ["SITCON2027簡介.pdf"]
    assert listing.files[0].path == "SITCON 2027/SITCON2027簡介.pdf"


async def test_list_folder_by_folder_id() -> None:
    svc = _service(children={"c1": [{"id": "v1", "name": "場地", "mimeType": FOLDER_MIME}]})
    listing = await svc.list_folder("c1")
    assert listing.path == "SITCON 2027/合約"
    assert [f.name for f in listing.folders] == ["場地"]


async def test_list_folder_follows_folder_shortcut() -> None:
    svc = _service(files=[], shortcuts=[FOLDER_SHORTCUT], children=CHILDREN)
    listing = await svc.list_folder("sc1")
    assert listing.folder_id == "x1"
    assert listing.path == "SITCON 2026/z. 2026 議程組"
    assert [f.name for f in listing.files] == ["議程表.gdoc"]


async def test_list_folder_rejects_out_of_scope() -> None:
    svc = _service()
    with pytest.raises(DriveReadError, match="不在可搜尋的範圍"):
        await svc.list_folder("o1")


async def test_list_folder_rejects_plain_file_id() -> None:
    svc = _service()
    with pytest.raises(DriveReadError, match="drive_read_file"):
        await svc.list_folder("f1")


# --------------------------------------------------------------------------- #
# resolve_for_read：範圍檢查、（私）判定、捷徑跟隨
# --------------------------------------------------------------------------- #
GDOC = "application/vnd.google-apps.document"
GSHEET = "application/vnd.google-apps.spreadsheet"

READ_FILES = [
    {"id": "d1", "name": "場地評估.gdoc", "parents": ["v1"], "mimeType": GDOC, "webViewLink": "u1"},
    {"id": "d5", "name": "範圍外.gdoc", "parents": ["o1"], "mimeType": GDOC, "webViewLink": "u5"},
    {"id": "d6", "name": "薪資表.gsheet", "parents": ["p1"], "mimeType": GSHEET, "webViewLink": "u6"},
]


async def test_resolve_in_scope_file() -> None:
    meta, private = await _service(files=READ_FILES).resolve_for_read("d1")
    assert meta.path == "SITCON 2027/合約/場地/場地評估.gdoc"
    assert meta.mime == GDOC
    assert private is False


async def test_resolve_rejects_out_of_scope() -> None:
    with pytest.raises(DriveReadError, match="不在可搜尋的範圍"):
        await _service(files=READ_FILES).resolve_for_read("d5")


async def test_resolve_missing_file() -> None:
    with pytest.raises(DriveReadError, match="找不到"):
        await _service(files=READ_FILES).resolve_for_read("nope")


async def test_resolve_marks_private_by_path() -> None:
    meta, private = await _service(files=READ_FILES).resolve_for_read("d6")
    assert private is True
    assert meta.path == "SITCON 2027/行政組（私）/薪資表.gsheet"


async def test_resolve_follows_file_shortcut_to_target() -> None:
    sc = {
        "id": "sf1", "name": "重要文件（捷徑）", "parents": ["c1"], "mimeType": SHORTCUT_MIME,
        "webViewLink": "us", "shortcutDetails": {"targetId": "d1", "targetMimeType": GDOC},
    }
    svc = _service(files=[*READ_FILES, sc])
    meta, private = await svc.resolve_for_read("sf1")
    assert meta.file_id == "d1"  # 讀的是目標
    assert meta.mime == GDOC
    assert meta.path == "SITCON 2027/合約/場地/場地評估.gdoc"  # 目標自身可解析 → 用目標路徑
    assert private is False


async def test_resolve_shortcut_target_outside_scope_uses_shortcut_path() -> None:
    """目標在範圍外（例如另一個共用硬碟）→ 沿用捷徑所在路徑；（私）隨捷徑路徑判定。"""
    ext = {"id": "xd", "name": "外部文件.gdoc", "parents": ["ext"], "mimeType": GDOC, "webViewLink": "ux"}
    sc = {
        "id": "sf2", "name": "外部文件", "parents": ["p1"], "mimeType": SHORTCUT_MIME,
        "webViewLink": "us", "shortcutDetails": {"targetId": "xd", "targetMimeType": GDOC},
    }
    svc = _service(files=[ext, sc])
    meta, private = await svc.resolve_for_read("sf2")
    assert meta.file_id == "xd"
    assert meta.path == "SITCON 2027/行政組（私）/外部文件"  # 捷徑自身路徑
    assert private is True  # （私）沿捷徑路徑繼承


async def test_resolve_folder_shortcut_redirects_to_list() -> None:
    svc = _service(files=[], shortcuts=[FOLDER_SHORTCUT])
    with pytest.raises(DriveReadError, match="drive_list_folder"):
        await svc.resolve_for_read("sc1")


# --------------------------------------------------------------------------- #
# （私）標記（DR-4 2026-08-03 修訂）
# --------------------------------------------------------------------------- #
def test_is_private_path_markers() -> None:
    from sitcon_bot.services.drive_client import is_private_path

    assert is_private_path("SITCON 2027/行政組（私）/薪資表.gsheet") is True
    assert is_private_path("SITCON 2027/議程組(私)/稿件.gdoc") is True  # 半形也算
    assert is_private_path("SITCON 2027/機密名單（私）.gdoc") is True  # 檔名帶標記也算（寧枉勿縱）
    assert is_private_path("SITCON 2027/合約/場地評估.gdoc") is False
    assert is_private_path("SITCON 2027/私人物品清單.gdoc") is False  # 單一「私」字不算


def test_content_mode_table() -> None:
    assert content_mode(GDOC) == ("export", "text/plain")
    assert content_mode("text/markdown") == ("download", None)
    assert content_mode("application/pdf") is None
    assert content_mode(None) is None


def test_query_builds_name_and_fulltext() -> None:
    q = build_search_query(["合約", "場地"])
    assert "trashed = false" in q
    assert "name contains '合約'" in q
    assert "fullText contains '合約'" in q
    assert "name contains '場地'" in q


def test_query_any_mode_joins_with_or() -> None:
    q = build_search_query(["合約", "場地"], require_all=False)
    assert ") or (" in q  # 關鍵字群之間為 or
    assert q.count("name contains") == 2
    # 基本條件仍以 and 相接
    assert q.startswith("trashed = false and ")


def test_query_escapes_quotes() -> None:
    assert "Yuan\\'s 檔" in build_search_query(["Yuan's 檔"])
