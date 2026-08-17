"""應用生命週期組裝。

T2 SQLite ✅｜T3 gateway ✅｜T6 LLM ✅｜T7 agent ✅｜T8 GitLab 工具接線 ✅
（Drive／HackMD 工具於 T10／T11 再加入 ToolRegistry。）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from functools import partial
from pathlib import Path

from .agent.context import PendingStore
from .agent.core import Agent, AgentRequest
from .agent.prompts import PromptBuilder, PromptData
from .agent.tools.base import ToolRegistry
from .agent.tools.calendar_tools import build_calendar_tools
from .agent.tools.drive_tools import build_drive_tools
from .agent.tools.gitlab_tools import build_gitlab_tools
from .agent.tools.hackmd_tools import build_hackmd_tools
from .agent.tools.memory_tools import build_memory_tools
from .agent.tools.people_tools import build_people_tools
from .agent.tools.photo_tools import build_photo_tools
from .agent.tools.reaction_tools import build_reaction_tools
from .auth.groups import GroupStore
from .domain.templates import load_template_store
from .notify.cards import collect_due_cards
from .notify.scheduler import MilestoneNotifier
from .notify.subscriptions import NotifyStateStore, SubscriptionStore
from .services.calendar_client import build_calendar_service
from .services.drive_client import build_drive_service
from .services.drive_content import build_drive_content_service
from .services.gitlab_client import build_gitlab_client
from .services.hackmd_client import build_hackmd_client
from .services.llm.base import build_llm_client
from .services.milestone_schedule import build_milestone_schedule_service
from .services.photo_index import build_photo_index_service
from .services.sheets_roster import RosterUnavailableError, build_roster_service
from .settings import Settings
from .storage.audit import AuditLog
from .storage.db import Database
from .storage.memories import GroupMemoryStore
from .telegram.commands import CommandHandlers, MilestoneDeps
from .telegram.gateway import BusinessRequest, BusinessResult, Gateway

log = logging.getLogger(__name__)


def _load_doc(path: str) -> str | None:
    """讀取整份文字文件（職掌 RO-8、背景知識）；缺檔或讀取失敗回 None。"""
    try:
        p = Path(path)
        if p.exists():
            return p.read_text(encoding="utf-8")
    except OSError:
        log.warning("讀取文件失敗：%s", path, exc_info=True)
    return None


async def run(settings: Settings) -> None:
    """啟動並常駐。收到 SIGINT/SIGTERM 時優雅結束。"""
    log.info("小石啟動中…")

    db = await Database.connect(settings.db_path)
    groups = GroupStore(db)
    await groups.load()
    audit = AuditLog(db)
    memories = GroupMemoryStore(db)

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
    drive_content = build_drive_content_service(settings.google_sa_json_path, drive)
    photos = build_photo_index_service(
        settings.google_sa_json_path,
        settings.photo_index_sheet_id,
        settings.photo_index_tab,
        settings.cache_ttl_photos,
    )
    milestones = build_milestone_schedule_service(
        settings.google_sa_json_path,
        settings.milestone_sheet_id,
        settings.milestone_sheet_gid,
        settings.cache_ttl_milestones,
    )
    calendar = None
    if settings.google_dwd_subject:
        calendar = build_calendar_service(
            settings.google_sa_json_path,
            settings.google_dwd_subject,
            settings.calendar_id,
            settings.tz,
            settings.calendar_send_updates,
        )
        log.info(
            "Calendar（DWD）啟用：subject=%s calendar=%s sendUpdates=%s",
            settings.google_dwd_subject, settings.calendar_id, settings.calendar_send_updates,
        )
    else:
        log.info("Calendar（DWD）停用（GOOGLE_DWD_SUBJECT 未設定）")
    hackmd = build_hackmd_client(settings)
    templates = await asyncio.to_thread(load_template_store)

    charter = {"text": await asyncio.to_thread(_load_doc, settings.team_charter_path)}
    if charter["text"] is None:
        log.info("未載入職掌文件（%s 缺），改以 Team:: label 判斷組別（RO-8）", settings.team_charter_path)
    else:
        log.info("已載入職掌文件：%s（%d 字）", settings.team_charter_path, len(charter["text"]))

    knowledge = {"text": await asyncio.to_thread(_load_doc, settings.knowledge_path)}
    if knowledge["text"] is None:
        log.info("未載入背景知識（%s 缺），不影響運作", settings.knowledge_path)
    else:
        log.info("已載入背景知識：%s（%d 字）", settings.knowledge_path, len(knowledge["text"]))

    async def _labels() -> list[str]:
        try:
            return (await gitlab.get_label_index()).names
        except Exception:
            log.warning("label 白名單載入失敗", exc_info=True)
            return []

    async def _statuses() -> list[str]:
        try:
            return (await gitlab.get_status_index()).names
        except Exception:
            log.warning("狀態白名單載入失敗", exc_info=True)
            return []

    async def _roster_rows() -> tuple[list[dict[str, object]], bool]:
        try:
            return (await roster.get()).to_llm_rows(), True
        except RosterUnavailableError:
            return [], False
        except Exception:
            log.warning("名冊載入失敗", exc_info=True)
            return [], False

    async def prompt_provider() -> PromptData:
        # 三者都吃快取，但冷快取／TTL 到期時是多趟外部 I/O；併發拿可省掉等待。
        labels, statuses, (rows, available) = await asyncio.gather(_labels(), _statuses(), _roster_rows())
        return PromptData(
            labels=labels,
            statuses=statuses,
            roster_rows=rows,
            charter=charter["text"],
            knowledge=knowledge["text"],
            roster_available=available,
        )

    tools = ToolRegistry(
        [
            *build_gitlab_tools(gitlab, roster, settings.gitlab_url),
            *build_people_tools(roster),
            *build_drive_tools(drive, drive_content),
            *build_photo_tools(photos),
            *build_reaction_tools(),
            *build_memory_tools(memories),
            *build_hackmd_tools(
                hackmd,
                templates,
                settings.hackmd_year_folder,
                settings.hackmd_meeting_folder,
                settings.hackmd_team_meeting_subfolder,
                settings.tz,
            ),
            *(build_calendar_tools(calendar) if calendar is not None else []),
        ]
    )
    prompt_builder = PromptBuilder(prompt_provider, tz=settings.tz, memories_provider=memories.list_for)
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
            n_statuses = await gitlab.reload_statuses()
        except Exception:
            log.warning("狀態白名單重載失敗", exc_info=True)
            n_statuses = 0
        try:
            n_roster = len(await roster.reload())
        except RosterUnavailableError:
            n_roster = 0
        try:
            await drive.reload()
        except Exception:
            log.warning("Drive 資料夾樹重載失敗", exc_info=True)
        n_photos = 0
        try:
            n_photos = len(await photos.reload())
        except Exception:
            log.warning("照片索引重載失敗", exc_info=True)
        n_milestones = 0
        try:
            n_milestones = len(await milestones.reload())
        except Exception:
            log.warning("籌備時程表重載失敗", exc_info=True)
        hackmd.reload()
        await asyncio.to_thread(templates.reload)
        charter["text"] = await asyncio.to_thread(_load_doc, settings.team_charter_path)
        charter_state = "已載入" if charter["text"] else "（缺）"
        knowledge["text"] = await asyncio.to_thread(_load_doc, settings.knowledge_path)
        knowledge_state = "已載入" if knowledge["text"] else "（缺）"
        return (
            f"label {n_labels} 個、狀態 {n_statuses} 個、名冊 {n_roster} 人、照片索引 {n_photos} 張、"
            f"里程碑 {n_milestones} 筆、Drive／HackMD 快取已重載、"
            f"職掌文件{charter_state}、背景知識{knowledge_state}"
        )

    # 里程碑預告（NT-*）：notifier 需要 gateway 送訊、gateway 需要 commands、commands 需要 notifier，
    # 三者互相依賴 → 送訊以「晚綁定」的 holder 解開，gateway 建好後填入。
    gateway_ref: dict[str, Gateway | None] = {"gw": None}

    async def send_push(chat_id: int, thread_id: int | None, text: str) -> bool:
        gw = gateway_ref["gw"]
        return await gw.send_html(chat_id, thread_id, text) if gw is not None else False

    notifier: MilestoneNotifier | None = None
    milestone_deps: MilestoneDeps | None = None
    if settings.milestone_notify_enabled:
        notifier = MilestoneNotifier(
            schedule=milestones,
            subscriptions=SubscriptionStore(db),
            state=NotifyStateStore(db),
            sender=send_push,
            cards=partial(collect_due_cards, gitlab, roster),  # NT-11 到期卡片提醒（target 由排程端帶入）
            tz=settings.tz,
            hour=settings.milestone_notify_hour,
            minute=settings.milestone_notify_minute,
            catchup_minutes=settings.milestone_notify_catchup_minutes,
            always_teams=settings.milestone_always_team_list,
            send_when_empty=settings.milestone_notify_when_empty,
        )
        milestone_deps = MilestoneDeps(
            subscriptions=SubscriptionStore(db), schedule=milestones, notifier=notifier
        )

    commands = CommandHandlers(settings, groups, reload_cb=reload_cb, milestones=milestone_deps, memories=memories)

    async def business_handler(req: BusinessRequest) -> BusinessResult:
        result = await agent.handle(
            AgentRequest(
                chat_id=req.chat_id,
                thread_id=req.thread_id,
                user_id=req.user_id,
                username=req.username,
                text=req.text,
                resume=req.resume,
                history=req.history,
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
            media=result.media,
            reaction=result.reaction,
            history=result.history,
        )

    gateway = Gateway(settings, groups, audit, commands, business_handler, pending_store=pending_store)
    gateway_ref["gw"] = gateway

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    notify_task = (
        asyncio.create_task(notifier.run(stop, ready=gateway.ready), name="milestone-notifier")
        if notifier is not None
        else None
    )
    try:
        await gateway.run(stop)
    finally:
        if notify_task is not None:
            stop.set()  # gateway 若因例外結束，排程也要跟著收
            notify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notify_task
        await hackmd.aclose()
        await db.close()
        log.info("小石結束。")
