"""Google Drive 搜尋工具（DR-1～DR-9）。只回傳 metadata（DR-4）。"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ...services.drive_client import DriveSearchService
from .base import Tool, ToolContext
from .external_data import wrap_external

log = logging.getLogger(__name__)


class DriveSearchArgs(BaseModel):
    keywords: list[str] = Field(description="檔名關鍵字（1～3 個為佳；由你從使用者敘述萃取）")
    scope: list[str] | None = Field(
        None, description="限縮範圍資料夾名（如「去年」→ ['SITCON 2026']）；未指定則兩者皆搜"
    )
    offset: int = Field(0, description="要跳過的筆數（要求下一批時用）")


class DriveSearchTool(Tool):
    name = "drive_search"
    description = (
        "在共用雲端硬碟的 SITCON 2025／2026／2027 範圍內搜尋檔案，只回傳檔名、路徑、連結、類型"
        "（絕不回傳檔案內容）。可用 scope 限縮年度、offset 取下一批。"
    )
    args_model = DriveSearchArgs

    def __init__(self, service: DriveSearchService | None) -> None:
        self._service = service

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, DriveSearchArgs)
        if self._service is None:
            return "雲端硬碟搜尋未設定，請通知管理員。"
        kws = "、".join(k for k in args.keywords if k.strip()) or "（無）"
        try:
            result = await self._service.search(args.keywords, scope_names=args.scope, offset=args.offset)
        except Exception as exc:  # 憑證/網路等
            log.warning("Drive 搜尋失敗", exc_info=True)
            return f"雲端硬碟搜尋暫時失敗（{exc}），請稍後再試或通知管理員。"

        if result.total == 0:
            return f"找不到符合的檔案（關鍵字：{kws}）。可換個關鍵字再試一次。"  # DR-7

        # 檔名與資料夾路徑由共用雲端硬碟任何可寫者操控 → 整份清單包進 <external_data>（NFR-6，DR-4 仍只回 metadata）。
        lines = [f"{f.name}｜{f.path}｜{f.url}" + (f"｜{f.mime}" if f.mime else "") for f in result.files]
        header = f"共 {result.total} 筆（關鍵字：{kws}）"
        if result.has_more:
            header += f"，顯示第 {result.offset + 1}–{result.offset + len(result.files)} 筆，可要求下一批"
        return header + "：\n" + wrap_external("\n".join(lines))


def build_drive_tools(service: DriveSearchService | None) -> list[Tool]:
    return [DriveSearchTool(service)]
