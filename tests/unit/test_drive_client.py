"""T10：Drive 唯讀搜尋——範圍過濾、路徑組裝、搜尋僅回 metadata、限縮、分頁、query；
以及 read_file 讀內容（範圍檢查、只讀得出文字、截斷）。"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from sitcon_bot.services.drive_client import (
    CONTENT_LIMIT,
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
FOLDERS_META: dict[str, dict[str, Any]] = {
    "s27": {"id": "s27", "name": "SITCON 2027", "parents": []},
    "s26": {"id": "s26", "name": "SITCON 2026", "parents": []},
    "c1": {"id": "c1", "name": "合約", "parents": ["s27"]},
    "v1": {"id": "v1", "name": "場地", "parents": ["c1"]},
    "c2": {"id": "c2", "name": "舊合約", "parents": ["s26"]},
    "o1": {"id": "o1", "name": "其他", "parents": []},
}

FILES = [
    {"id": "f1", "name": "場地租借合約.pdf", "parents": ["v1"], "mimeType": "application/pdf", "webViewLink": "u1"},
    {"id": "f2", "name": "去年場地合約.pdf", "parents": ["c2"], "mimeType": "application/pdf", "webViewLink": "u2"},
    {"id": "f3", "name": "無關合約.pdf", "parents": ["o1"], "mimeType": "application/pdf", "webViewLink": "u3"},
    {"id": "f4", "name": "SITCON2027簡介.pdf", "parents": ["s27"], "mimeType": "application/pdf", "webViewLink": "u4"},
]


class FakeDriveGateway:
    def __init__(self, folders_meta: dict[str, dict[str, Any]], files: list[dict[str, Any]]) -> None:
        self._meta = folders_meta
        self._files = files
        self.last_query: str | None = None
        self.get_calls: list[str] = []
        self.fetch_calls: list[tuple[str, str | None]] = []
        self.content = "檔案內文"

    async def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        self.get_calls.append(folder_id)
        return self._meta.get(folder_id)

    async def search_files(self, query: str) -> list[dict[str, Any]]:
        self.last_query = query
        return list(self._files)

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        return next((f for f in self._files if f["id"] == file_id), None)

    async def fetch_text(self, file_id: str, export_mime: str | None) -> str:
        self.fetch_calls.append((file_id, export_mime))
        return self.content


def _service(meta=FOLDERS_META, files=FILES) -> DriveSearchService:
    return DriveSearchService(FakeDriveGateway(meta, files), SCOPE, ttl_seconds=1800)


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
    """搜尋結果本身仍只有 metadata（內容要另外呼叫 read_file 才拿得到）。"""
    for f in (await _service().search(["合約"])).files:
        assert set(dataclasses.asdict(f).keys()) == {"name", "path", "url", "mime", "file_id"}
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
# read_file：讀內容（供相關性判斷）——範圍檢查、型別限制、截斷
# --------------------------------------------------------------------------- #
GDOC = "application/vnd.google-apps.document"
GSHEET = "application/vnd.google-apps.spreadsheet"

READ_FILES = [
    {"id": "d1", "name": "場地評估.gdoc", "parents": ["v1"], "mimeType": GDOC, "webViewLink": "u1"},
    {"id": "d2", "name": "預算.gsheet", "parents": ["c1"], "mimeType": GSHEET, "webViewLink": "u2"},
    {"id": "d3", "name": "筆記.txt", "parents": ["s27"], "mimeType": "text/plain", "webViewLink": "u3"},
    {"id": "d4", "name": "合約.pdf", "parents": ["v1"], "mimeType": "application/pdf", "webViewLink": "u4"},
    {"id": "d5", "name": "範圍外.gdoc", "parents": ["o1"], "mimeType": GDOC, "webViewLink": "u5"},
]


def _read_service() -> tuple[DriveSearchService, FakeDriveGateway]:
    gw = FakeDriveGateway(FOLDERS_META, READ_FILES)
    return DriveSearchService(gw, SCOPE, ttl_seconds=1800), gw


async def test_read_google_doc_exports_as_text() -> None:
    svc, gw = _read_service()
    gw.content = "場地租金 12 萬"
    content = await svc.read_file("d1")
    assert content.text == "場地租金 12 萬"
    assert content.file.path == "SITCON 2027/合約/場地/場地評估.gdoc"
    assert gw.fetch_calls == [("d1", "text/plain")]  # Google 文件走 export


async def test_read_spreadsheet_exports_as_csv() -> None:
    svc, gw = _read_service()
    await svc.read_file("d2")
    assert gw.fetch_calls == [("d2", "text/csv")]


async def test_read_plain_text_downloads_directly() -> None:
    svc, gw = _read_service()
    await svc.read_file("d3")
    assert gw.fetch_calls == [("d3", None)]  # 非 Google 原生檔走 get_media


async def test_read_rejects_binary_type() -> None:
    svc, gw = _read_service()
    with pytest.raises(DriveReadError, match="無法取出文字"):
        await svc.read_file("d4")
    assert gw.fetch_calls == []


async def test_read_rejects_out_of_scope_file() -> None:
    """DR-1：範圍外的檔案讀不到——連內容都不會去抓。"""
    svc, gw = _read_service()
    with pytest.raises(DriveReadError, match="不在可搜尋的範圍"):
        await svc.read_file("d5")
    assert gw.fetch_calls == []


async def test_read_missing_file() -> None:
    svc, _ = _read_service()
    with pytest.raises(DriveReadError, match="找不到"):
        await svc.read_file("nope")


async def test_read_truncates_long_content() -> None:
    svc, gw = _read_service()
    gw.content = "字" * (CONTENT_LIMIT + 500)
    content = await svc.read_file("d1")
    assert len(content.text) == CONTENT_LIMIT
    assert content.truncated is True


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


def test_query_escapes_quotes() -> None:
    assert "Yuan\\'s 檔" in build_search_query(["Yuan's 檔"])
