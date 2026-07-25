"""T10：Drive 唯讀搜尋——以範圍資料夾 ID 為根 BFS、範圍過濾、路徑組裝、僅 metadata、限縮、分頁、query。"""

from __future__ import annotations

import dataclasses
from typing import Any

from sitcon_bot.services.drive_client import (
    DriveFile,
    DriveSearchService,
    build_search_query,
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

    async def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        self.get_calls.append(folder_id)
        return self._meta.get(folder_id)

    async def search_files(self, query: str) -> list[dict[str, Any]]:
        self.last_query = query
        return list(self._files)


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


async def test_dr4_result_is_metadata_only() -> None:
    for f in (await _service().search(["合約"])).files:
        assert set(dataclasses.asdict(f).keys()) == {"name", "path", "url", "mime"}
    assert not hasattr(DriveFile("n", "p", "u"), "content")


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


def test_query_builds_name_and_fulltext() -> None:
    q = build_search_query(["合約", "場地"])
    assert "trashed = false" in q
    assert "name contains '合約'" in q
    assert "fullText contains '合約'" in q
    assert "name contains '場地'" in q


def test_query_escapes_quotes() -> None:
    assert "Yuan\\'s 檔" in build_search_query(["Yuan's 檔"])
