"""應用生命週期組裝。

T2 SQLite ✅｜T3 gateway ✅｜T6 LLM ✅｜T7 agent ✅｜T8 GitLab 工具接線 ✅
（Drive／HackMD 工具於 T10／T11 再加入 ToolRegistry。）
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from .agent.context import PendingStore
from .agent.core import Agent, AgentRequest
from .agent.prompts import PromptBuilder, PromptData
from .agent.tools.base import ToolRegistry
from .agent.tools.drive_tools import build_drive_tools
from .agent.tools.gitlab_tools import build_gitlab_tools
from .agent.tools.hackmd_tools import build_hackmd_tools
from .agent.tools.people_tools import build_people_tools
from .auth.groups import GroupStore
from .domain.templates import load_template_store
from .services.drive_client import build_drive_service
from .services.gitlab_client import build_gitlab_client
from .services.hackmd_client import build_hackmd_client
from .services.llm.base import build_llm_client
from .services.sheets_roster import RosterUnavailableError, build_roster_service
from .settings import Settings
from .storage.audit import AuditLog
from .storage.db import Database
from .telegram.commands import CommandHandlers
from .telegram.gateway import BusinessRequest, BusinessResult, Gateway

log = logging.getLogger(__name__)


def _load_charter(path: str) -> str | None:
    """讀取職掌文件（RO-8）；缺檔或讀取失敗回 None。"""
    try:
        p = Path(path)
        if p.exists():
            return p.read_text(encoding="utf-8")
    except OSError:
        log.warning("讀取職掌文件失敗：%s", path, exc_info=True)
    return None


async def run(settings: Settings) -> None:
    """啟動並常駐。收到 SIGINT/SIGTERM 時優雅結束。"""
    log.info("小石啟動中…")

    db = await Database.connect(settings.db_path)
    groups = GroupStore(db)
    await groups.load()
    audit = AuditLog(db)

    llm = build_llm_client(settings)
    gitlab = build_gitlab_client(settings)
    roster = build_roster_service(
        settings.google_sa_json_path,
        settings.roster_sheet_id,
        settings.roster_sheet_gid,
        settings.cache_ttl_roster,
    )
    drive = build_drive_service(
        settings.google_sa_json_path,
        settings.drive_scope_map,
        settings.cache_ttl_drive_tree,
    )
    hackmd = build_hackmd_client(settings)
    templates = await asyncio.to_thread(load_template_store)

    charter = {"text": await asyncio.to_thread(_load_charter, settings.team_charter_path)}
    if charter["text"] is None:
        log.info("未載入職掌文件（%s 缺），改以 Team:: label 判斷組別（RO-8）", settings.team_charter_path)
    else:
        log.info("已載入職掌文件：%s（%d 字）", settings.team_charter_path, len(charter["text"]))

    async def prompt_provider() -> PromptData:
        labels: list[str] = []
        try:
            labels = (await gitlab.get_label_index()).names
        except Exception:
            log.warning("label 白名單載入失敗", exc_info=True)
        rows: list[dict[str, object]] = []
        available = True
        try:
            rows = (await roster.get()).to_llm_rows()
        except RosterUnavailableError:
            available = False
        except Exception:
            log.warning("名冊載入失敗", exc_info=True)
            available = False
        return PromptData(labels=labels, roster_rows=rows, charter=charter["text"], roster_available=available)

    tools = ToolRegistry(
        [
            *build_gitlab_tools(gitlab, roster),
            *build_people_tools(roster),
            *build_drive_tools(drive),
            *build_hackmd_tools(
                hackmd,
                templates,
                settings.hackmd_year_folder,
                settings.hackmd_meeting_folder,
                settings.hackmd_team_meeting_subfolder,
                settings.tz,
                settings.hackmd_search_folder_list,
            ),
        ]
    )
    prompt_builder = PromptBuilder(prompt_provider, tz=settings.tz)
    pending_store = PendingStore(settings.context_ttl_seconds)
    agent = Agent(
        llm=llm,
        tools=tools,
        prompt_builder=prompt_builder,
        roster=roster,
        thinking=settings.llm_thinking,
        max_iterations=settings.llm_max_tool_iterations,
    )

    async def reload_cb() -> str:
        try:
            n_labels = await gitlab.reload_labels()
        except Exception:
            log.warning("label 重載失敗", exc_info=True)
            n_labels = 0
        try:
            n_roster = len(await roster.reload())
        except RosterUnavailableError:
            n_roster = 0
        try:
            await drive.reload()
        except Exception:
            log.warning("Drive 資料夾樹重載失敗", exc_info=True)
        hackmd.reload()
        await asyncio.to_thread(templates.reload)
        charter["text"] = await asyncio.to_thread(_load_charter, settings.team_charter_path)
        charter_state = "已載入" if charter["text"] else "（缺）"
        return f"label {n_labels} 個、名冊 {n_roster} 人、Drive／HackMD 快取已重載、職掌文件{charter_state}"

    commands = CommandHandlers(settings, groups, reload_cb=reload_cb)

    async def business_handler(req: BusinessRequest) -> BusinessResult:
        result = await agent.handle(
            AgentRequest(
                chat_id=req.chat_id,
                thread_id=req.thread_id,
                user_id=req.user_id,
                username=req.username,
                text=req.text,
                resume=req.resume,
                reply_context=req.reply_context,
            )
        )
        return BusinessResult(
            reply=result.reply,
            action=result.action,
            target=result.target,
            status=result.status,
            error=result.error,
            detail=result.detail,
            pending=result.pending,
        )

    gateway = Gateway(settings, groups, audit, commands, business_handler, pending_store=pending_store)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    try:
        await gateway.run(stop)
    finally:
        await hackmd.aclose()
        await db.close()
        log.info("小石結束。")
