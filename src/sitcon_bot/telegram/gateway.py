"""Telegram gateway：長輪詢、TRIG-1 過濾、指令與業務分派、分段送出、typing。

privacy off 後 bot 收到授權群組的所有訊息（TRIG-2），過濾僅在本機記憶體進行（TRIG-1）。
非觸發訊息一律當場丟棄，不入儲存、不送 LLM（LOG-3）。

併發模型（三層）：
  1. PTB `concurrent_updates(n)`：update 之間並行，不再是處理完一則才拿下一則。
     非觸發訊息與指令因此可以立刻回，不必排在別群的 agent 回合後面。
  2. per-chat 序列化：同一個對話（chat + forum topic）的 agent 回合一次只跑一個，
     維持回覆順序與 ask_user 續接語意；不同群／不同 topic 完全並行。
     鎖只包住 agent 呼叫——👀 reaction 與 typing 在排隊期間就先送出，使用者看得到已收到。
     （PTB 的 `BaseUpdateProcessor.process_update` 是 @final，全域 semaphore 一定在最外層，
     所以 per-chat 鎖放在這裡而不是自訂 update processor，順便讓指令不受業務回合阻塞。）
  3. 全域 agent 回合上限：擋突發流量打爆 LLM／GitLab 速率限制。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    MessageEntity,
    ReactionTypeEmoji,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..agent.context import HistoryStore, PendingStore
from ..auth.groups import GroupStore
from ..concurrency import KeyedLock
from ..settings import Settings
from ..storage.audit import AuditLog
from .commands import CommandHandlers
from .formatting import escape_html, split_message
from .routing import Action, classify_trigger, command_args, route

log = logging.getLogger(__name__)

# 重啟後 getUpdates 可能帶回積壓訊息；超過此時限的觸發視為過期並丟棄（AGENTS 6.5）。
STALE_AFTER = timedelta(minutes=5)
TYPING_INTERVAL = 4.0  # 秒；< 5 秒以維持 typing 指示（NFR-1 / AGENTS 4.6）

# 進度回饋：收到即 👀 reaction + typing，完成後改 ✅。不送佔位訊息（會讓使用者漏收最終回覆通知）。
REACT_RECEIVED = "👀"
REACT_DONE = "✅"
GENERIC_ERROR = "小石遇到未預期的錯誤，請稍後再試或通知管理員。"

# 串流草稿（Bot API 9.5+ sendMessageDraft）：LLM 生成期間即時預覽部分回覆，同 draft_id 有打字動畫。
DRAFT_INTERVAL = 1.5  # 秒；節流草稿更新頻率，避免撞 Telegram 速率限制
DRAFT_MAX_CHARS = 4096  # sendMessageDraft 的 text 上限

# LLM 服務出問題時的錯誤回覆掛「重試」按鈕：按下即以原始請求重跑，並就地更新該則錯誤訊息。
RETRY_PREFIX = "retry:"
RETRY_BUTTON_TEXT = "🔄 重試"
RETRY_IN_PROGRESS = "🔄 重試中…"
RETRY_GONE = "這個重試按鈕已過期或正在重試中，請重新傳送原本的訊息。"


class RetryStore:
    """token → 原始 BusinessRequest；take 即移除（防連點重複觸發），TTL 過期或重啟即失效。"""

    def __init__(self, ttl_seconds: int, max_entries: int = 200, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._store: dict[str, tuple[float, BusinessRequest]] = {}

    def put(self, req: BusinessRequest) -> str:
        now = self._clock()
        for k in [k for k, (at, _) in self._store.items() if (now - at) > self._ttl]:
            self._store.pop(k, None)
        if len(self._store) >= self._max:
            for k, _ in sorted(self._store.items(), key=lambda kv: kv[1][0])[: len(self._store) - self._max + 1]:
                self._store.pop(k, None)
        token = secrets.token_hex(8)
        self._store[token] = (now, req)
        return token

    def take(self, token: str) -> BusinessRequest | None:
        item = self._store.pop(token, None)
        if item is None:
            return None
        at, req = item
        return req if (self._clock() - at) <= self._ttl else None


@dataclass(slots=True)
class BusinessRequest:
    """一則業務觸發訊息（交給 agent 處理）。"""

    chat_id: int
    chat_title: str | None
    thread_id: int | None
    user_id: int
    username: str | None
    text: str
    trigger_message_id: int
    # 純 reply-chain 脈絡（優先序由上而下）：resume=回覆 ask_user 問句時的待答狀態；
    # history=回覆小石一般回覆時該回合的完整 transcript（含工具紀錄，2026-08-11 修訂）；
    # reply_context=被回覆訊息內容（他人訊息或 transcript 失效時的後援）
    resume: Any = None
    history: Any = None
    reply_context: str | None = None
    # 串流回呼（gateway 於回合開跑前填入 _DraftStreamer）；agent 以累積全文呼叫
    on_partial: Any = None


class _DraftStreamer:
    """節流地以 sendMessageDraft 串流部分回覆（同 draft_id 更新會有打字動畫）。

    草稿是暫時預覽（約 30 秒），最終回覆仍由正式訊息送出並取代之。
    任一失敗即停用本回合串流（環境不支援等），完全不影響正式回覆。
    """

    def __init__(self, bot: Any, chat_id: int, thread_id: int | None, draft_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._draft_id = draft_id
        self._last = 0.0
        self._disabled = False

    async def __call__(self, text: str) -> None:
        if self._disabled or not text:
            return
        now = asyncio.get_running_loop().time()
        if now - self._last < DRAFT_INTERVAL:
            return
        self._last = now
        try:
            # 草稿以純文字送出（不帶 parse_mode——串流中的半截標記會壞）；逾長取尾端維持動態
            await self._bot.send_message_draft(
                chat_id=self._chat_id,
                draft_id=self._draft_id,
                text=text[-DRAFT_MAX_CHARS:],
                message_thread_id=self._thread_id,
            )
        except Exception as exc:
            self._disabled = True
            log.info("串流草稿不可用，本回合停用 chat_id=%s：%s", self._chat_id, exc)


@dataclass(slots=True)
class BusinessResult:
    """agent 處理結果（供回覆與稽核 LOG-1）。"""

    reply: str
    action: str = "agent"
    target: str | None = None
    status: str = "ok"  # ok / clarify / error
    error: str | None = None
    detail: dict[str, Any] | None = None
    pending: Any = None  # 若以 ask_user 收尾，帶回待答狀態供以問句 message_id 保存
    media: list[Any] = field(default_factory=list)  # 隨回覆送出的圖片（MediaItem：url/caption）
    reaction: str | None = None  # agent 要求對觸發訊息按的 emoji（react_heart ❤），取代完成時的 ✅
    history: Any = None  # 本回合完整 transcript；以小石回覆的 message_id 保存供「回覆此則」續接


# 業務處理器：吃 BusinessRequest 回傳 BusinessResult。
BusinessHandler = Callable[[BusinessRequest], Awaitable[BusinessResult]]


async def _echo_handler(req: BusinessRequest) -> BusinessResult:
    return BusinessResult(reply=f"（小石施工中）我收到你的訊息了：{req.text}", action="echo")


class Gateway:
    def __init__(
        self,
        settings: Settings,
        groups: GroupStore,
        audit: AuditLog,
        commands: CommandHandlers,
        business_handler: BusinessHandler | None = None,
        pending_store: PendingStore | None = None,
        history_store: HistoryStore | None = None,
    ) -> None:
        self._settings = settings
        self._groups = groups
        self._audit = audit
        self._commands = commands
        self._business_handler = business_handler or _echo_handler
        self._pending = pending_store or PendingStore(settings.context_ttl_seconds)
        self._history = history_store or HistoryStore(settings.context_ttl_seconds)
        self._app: Application | None = None
        self.bot_id: int = 0
        self.bot_username: str | None = None
        # 長輪詢就緒後設定；主動推播（里程碑預告）在此之前不得送出。
        self.ready = asyncio.Event()
        # 同一對話的 agent 回合序列化；鍵為 (chat_id, thread_id)，forum topic 之間可並行。
        # 關掉即為 SPEC EC-16 的「同群組併發觸發彼此不阻塞」。
        self._chat_lock = KeyedLock() if settings.serialize_per_chat else None
        self._agent_slots = asyncio.Semaphore(max(1, settings.max_concurrent_agent_turns))
        # LLM 服務錯誤的「重試」按鈕：token → 原始請求；與 reply-chain 同一套 TTL
        self._retries = RetryStore(settings.context_ttl_seconds)

    # ------------------------------------------------------------------ #
    # 生命週期
    # ------------------------------------------------------------------ #
    async def run(self, stop: asyncio.Event) -> None:
        """建立 Application、getMe 檢查、開始長輪詢，直到 stop 被設定。"""
        token = self._settings.telegram_bot_token.get_secret_value()
        # concurrent_updates > 1 才會讓 PTB 對每則 update 開 task；預設 1 是嚴格逐則處理。
        # rate_limiter：並行後同群送訊變密集，撞到 429 時自動退避重試——否則 _reply 會把
        # 例外吞掉（見該方法），使用者等不到回覆卻沒有任何跡象。
        app = (
            ApplicationBuilder()
            .token(token)
            .concurrent_updates(max(2, self._settings.max_concurrent_updates))
            .rate_limiter(AIORateLimiter(max_retries=3))
            .build()
        )
        app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, self._on_message))
        app.add_handler(CallbackQueryHandler(self._on_callback, pattern=f"^{RETRY_PREFIX}"))
        self._app = app

        await app.initialize()
        me = await app.bot.get_me()
        self.bot_id = me.id
        self.bot_username = me.username
        log.info("Telegram 連線成功：@%s (id=%s)", me.username, me.id)

        await app.start()
        await app.updater.start_polling(drop_pending_updates=False, allowed_updates=["message", "callback_query"])
        log.info("開始長輪詢；等待訊息…")
        self.ready.set()
        try:
            await stop.wait()
        finally:
            self.ready.clear()
            log.info("關閉 Telegram gateway…")
            for closer in (app.updater.stop, app.stop, app.shutdown):
                with contextlib.suppress(Exception):
                    await closer()

    # ------------------------------------------------------------------ #
    # 訊息處理
    # ------------------------------------------------------------------ #
    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None or message.from_user is None or message.chat is None:
            return
        if message.from_user.is_bot:
            return
        # 過期觸發丟棄（避免重啟後重播積壓訊息）
        if message.date and datetime.now(UTC) - message.date > STALE_AFTER:
            return

        text = message.text or message.caption or ""
        mentions = self._mentions_bot(message)
        reply_to_bot = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == self.bot_id
        )
        kind, command = classify_trigger(
            text,
            mentions_bot=mentions,
            reply_to_bot=reply_to_bot,
            trigger_name=self._settings.bot_trigger_name,
            bot_username=self.bot_username,
        )
        action = route(
            chat_type=str(message.chat.type),
            is_admin=message.from_user.id == self._settings.telegram_admin_id,
            is_authorized=self._groups.is_authorized(message.chat.id),
            kind=kind,
            command=command,
        )
        if action is Action.IGNORE:
            return  # 沉默丟棄（LOG-3：不記錄非觸發訊息）

        try:
            await self._dispatch(action, message, text)
        except Exception:  # gateway 邊界：不可讓單則訊息的例外中斷長輪詢
            log.exception("處理訊息時發生未預期錯誤 chat_id=%s", message.chat.id)
            with contextlib.suppress(Exception):
                await self._reply(message, "小石遇到未預期的錯誤，請稍後再試或通知管理員。")

    async def _dispatch(self, action: Action, message: Message, text: str) -> None:
        chat = message.chat
        if action is Action.PRIVATE_NOTICE:
            await self._reply(message, self._commands.private_notice())
        elif action is Action.PRIVATE_AUTHORIZE_REDIRECT:
            await self._reply(message, self._commands.private_authorize_redirect())
        elif action is Action.CMD_AUTHORIZE:
            await self._reply(message, await self._commands.authorize(chat.id, chat.title))
        elif action is Action.CMD_REVOKE:
            await self._reply(message, await self._commands.revoke(chat.id))
        elif action is Action.CMD_LIST_GROUPS:
            await self._reply(message, await self._commands.list_groups())
        elif action is Action.CMD_RELOAD:
            await self._reply(message, await self._commands.reload())
        elif action is Action.CMD_HELP:
            await self._reply(message, self._commands.help_text())
        elif action is Action.CMD_START:
            await self._reply(message, self._commands.start_text())
        elif action is Action.CMD_NOTIFY_ON:
            user = message.from_user
            assert user is not None
            await self._reply(
                message,
                await self._commands.notify_on(
                    chat.id, chat.title, user.id, command_args(text), message.message_thread_id
                ),
            )
        elif action is Action.CMD_NOTIFY_OFF:
            await self._reply(message, await self._commands.notify_off(chat.id))
        elif action is Action.CMD_NOTIFY_LIST:
            await self._reply(message, await self._commands.notify_list())
        elif action is Action.CMD_NOTIFY_TEST:
            await self._reply(message, await self._commands.notify_test(chat.id))
        elif action is Action.BUSINESS:
            await self._handle_business(message, text)

    async def _handle_business(self, message: Message, text: str) -> None:
        user = message.from_user
        assert user is not None
        # 純 reply-chain 脈絡：回覆 ask_user 問句 → 待答狀態續接；回覆小石一般回覆 → 完整
        # transcript 續接（2026-08-11 修訂）；其餘（他人訊息／已失效）→ 帶被回覆文字當脈絡
        resume = None
        history = None
        reply_context = None
        reply_to = message.reply_to_message
        if reply_to is not None:
            resume = self._pending.take(message.chat.id, reply_to.message_id)
            if resume is None:
                history = self._history.get(message.chat.id, reply_to.message_id)
            if resume is None and history is None:
                reply_context = reply_to.text or reply_to.caption
        req = BusinessRequest(
            chat_id=message.chat.id,
            chat_title=message.chat.title,
            thread_id=message.message_thread_id,
            user_id=user.id,
            username=user.username,
            text=text,
            trigger_message_id=message.message_id,
            resume=resume,
            history=history,
            reply_context=reply_context,
        )
        # 收到即回饋：👀 reaction + typing（不送佔位訊息，避免使用者漏收最終回覆通知）
        await self._react(req.chat_id, req.trigger_message_id, REACT_RECEIVED)
        try:
            result = await self._run_business(req)
        except Exception:  # 業務層未預期錯誤：回一則錯誤說明，不中斷長輪詢
            log.exception("業務處理未預期錯誤 chat_id=%s", req.chat_id)
            await self._reply(message, escape_html(GENERIC_ERROR))
            await self._audit.record(
                chat_id=req.chat_id,
                chat_title=req.chat_title,
                user_id=req.user_id,
                username=req.username,
                trigger_text=text,
                action="error",
                target=None,
                detail=None,
                status="error",
                error="unhandled",
            )
            return
        # 業務回覆為 LLM 產生的動態內容 → escape 後才以 HTML 送出（避免破壞 HTML / 注入）
        reply_mid = await self._reply(message, escape_html(result.reply), reply_markup=self._retry_markup(req, result))
        await self._finalize(req, result, reply_mid)

    async def _run_business(self, req: BusinessRequest) -> BusinessResult:
        # 串流草稿：每回合（含重試）配一個新 streamer；停用或不支援時為 None（agent 即不串流）
        req.on_partial = (
            _DraftStreamer(self._app.bot, req.chat_id, req.thread_id, req.trigger_message_id)
            if self._settings.stream_draft_replies and self._app is not None
            else None
        )
        # typing 包在鎖外：排隊等前一則跑完的期間也持續顯示「輸入中」。
        async with self._typing(req.chat_id, req.thread_id):
            # 同一對話序列化 → 同群回覆保持順序；再取全域額度擋突發流量。
            async with self._serialized(req.chat_id, req.thread_id), self._agent_slots:
                return await self._business_handler(req)

    def _retry_markup(self, req: BusinessRequest, result: BusinessResult) -> InlineKeyboardMarkup | None:
        """LLM 服務問題（llm_unavailable）的錯誤回覆掛「重試」按鈕，按下以原始請求重跑。"""
        if result.error != "llm_unavailable":
            return None
        token = self._retries.put(req)
        return InlineKeyboardMarkup([[InlineKeyboardButton(RETRY_BUTTON_TEXT, callback_data=f"{RETRY_PREFIX}{token}")]])

    async def _finalize(self, req: BusinessRequest, result: BusinessResult, reply_mid: int | None) -> None:
        """回覆送出後的共同收尾：reaction／縮圖／反問與 transcript 保存／稽核（LOG-1）。"""
        if result.status == "ok":
            if result.media:  # 代表縮圖（photo_search）→ 以圖片送出
                await self._send_media(req, result.media)
            # 完成 → ✅；agent 有按愛心（react_heart）則以 ❤ 取代（bot 一則訊息只能掛一個 reaction）
            await self._react(req.chat_id, req.trigger_message_id, result.reaction or REACT_DONE)
        elif result.reaction:  # clarify 也保留愛心（維持 👀 只在未按愛心時）
            await self._react(req.chat_id, req.trigger_message_id, result.reaction)
        # 反問待答狀態以「問句 message_id」為鍵保存；使用者回覆該問句時才續接
        if result.pending is not None and reply_mid is not None:
            self._pending.put(req.chat_id, reply_mid, result.pending)
        # 完成回合的完整 transcript 以「回覆 message_id」保存；使用者回覆該則時以完整脈絡續接
        if result.history is not None and reply_mid is not None:
            self._history.put(req.chat_id, reply_mid, result.history)
        # LOG-1：記錄觸發互動（動作、目標、結果狀態、錯誤摘要）
        await self._audit.record(
            chat_id=req.chat_id,
            chat_title=req.chat_title,
            user_id=req.user_id,
            username=req.username,
            trigger_text=req.text,
            action=result.action,
            target=result.target,
            detail=result.detail,
            status=result.status,
            error=result.error,
        )

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """「重試」按鈕：取回原始請求重跑 agent 回合，結果就地更新該則錯誤訊息。"""
        q = update.callback_query
        if q is None or not (q.data or "").startswith(RETRY_PREFIX):
            return
        assert self._app is not None
        msg = q.message  # 可能為 InaccessibleMessage（仍有 message_id）；None 代表訊息已不可考
        req = self._retries.take((q.data or "")[len(RETRY_PREFIX) :])
        if req is None or msg is None:
            with contextlib.suppress(Exception):
                await q.answer(RETRY_GONE)
            if msg is not None:  # 移除失效按鈕，避免重複點擊
                with contextlib.suppress(Exception):
                    await self._app.bot.edit_message_reply_markup(
                        chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=None
                    )
            return
        # 按鈕任何群組成員都可按，但群組須仍在授權名單（授權可能在按下前被撤銷）
        if not (self._groups.is_authorized(req.chat_id) or q.from_user.id == self._settings.telegram_admin_id):
            with contextlib.suppress(Exception):
                await q.answer("此對話已不在授權名單，無法重試。")
            return
        with contextlib.suppress(Exception):
            await q.answer("重試中…")
        mid = msg.message_id
        await self._edit(req.chat_id, mid, RETRY_IN_PROGRESS)
        try:
            result = await self._run_business(req)
        except Exception:  # 與 _handle_business 同界線：不可讓例外中斷長輪詢
            log.exception("重試回合未預期錯誤 chat_id=%s", req.chat_id)
            await self._edit(req.chat_id, mid, escape_html(GENERIC_ERROR))
            await self._audit.record(
                chat_id=req.chat_id,
                chat_title=req.chat_title,
                user_id=req.user_id,
                username=req.username,
                trigger_text=req.text,
                action="error",
                target=None,
                detail={"retry": True},
                status="error",
                error="unhandled",
            )
            return
        result.detail = {**(result.detail or {}), "retry": True}
        # 結果就地更新該則錯誤訊息；仍失敗則重掛按鈕可再重試。逾長時第一段就地更新、其餘分段回覆
        chunks = split_message(escape_html(result.reply))
        edited = await self._edit(req.chat_id, mid, chunks[0], reply_markup=self._retry_markup(req, result))
        for chunk in chunks[1:]:
            with contextlib.suppress(Exception):
                await self._app.bot.send_message(
                    chat_id=req.chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    reply_parameters=ReplyParameters(
                        message_id=req.trigger_message_id, allow_sending_without_reply=True
                    ),
                    message_thread_id=req.thread_id,
                )
        await self._finalize(req, result, mid if edited else None)

    # ------------------------------------------------------------------ #
    # 輔助
    # ------------------------------------------------------------------ #
    def _mentions_bot(self, message: Message) -> bool:
        if not self.bot_username:
            return False
        text = message.text or message.caption or ""
        entities = message.entities or message.caption_entities or []
        target = f"@{self.bot_username}".lower()
        for e in entities:
            if e.type == MessageEntity.MENTION:
                frag = text[e.offset : e.offset + e.length]
                if frag.lower() == target:
                    return True
            elif e.type == MessageEntity.TEXT_MENTION and e.user and e.user.id == self.bot_id:
                return True
        return False

    @contextlib.asynccontextmanager
    async def _serialized(self, chat_id: int, thread_id: int | None) -> AsyncIterator[None]:
        """同一對話的 agent 回合互斥；serialize_per_chat=False 時直接放行（EC-16）。"""
        if self._chat_lock is None:
            yield
            return
        async with self._chat_lock((chat_id, thread_id)):
            yield

    @contextlib.asynccontextmanager
    async def _typing(self, chat_id: int, thread_id: int | None) -> AsyncIterator[None]:
        """處理期間持續送出 typing 指示（TRIG-9 / NFR-1）。"""
        task = asyncio.create_task(self._typing_loop(chat_id, thread_id))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _typing_loop(self, chat_id: int, thread_id: int | None) -> None:
        assert self._app is not None
        while True:
            with contextlib.suppress(Exception):
                await self._app.bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.TYPING, message_thread_id=thread_id
                )
            await asyncio.sleep(TYPING_INTERVAL)

    async def _send_media(self, req: BusinessRequest, media: list[Any]) -> None:
        """送出代表縮圖（單張用 send_photo、多張用媒體群組）；失敗靜默略過（連結已在文字回覆內）。

        Telegram 會自行抓取圖片 URL；圖組上限 10 張。caption 為純文字（含 Flickr 連結，會自動變可點）。
        """
        assert self._app is not None
        items = [m for m in media if getattr(m, "url", None)][:10]
        if not items:
            return
        reply_params = ReplyParameters(message_id=req.trigger_message_id, allow_sending_without_reply=True)
        with contextlib.suppress(Exception):
            if len(items) == 1:
                await self._app.bot.send_photo(
                    chat_id=req.chat_id,
                    photo=items[0].url,
                    caption=items[0].caption or None,
                    reply_parameters=reply_params,
                    message_thread_id=req.thread_id,
                )
            else:
                group = [InputMediaPhoto(media=m.url, caption=m.caption or None) for m in items]
                await self._app.bot.send_media_group(
                    chat_id=req.chat_id,
                    media=group,
                    reply_parameters=reply_params,
                    message_thread_id=req.thread_id,
                )

    async def send_html(self, chat_id: int, thread_id: int | None, text: str) -> bool:
        """主動送出一則訊息（非回覆），供里程碑預告排程使用。

        text 須為合法 HTML（呼叫端已 escape 動態內容）。逾長分段（TRIG-8）。
        回傳是否至少送出一段；失敗只記錄不拋出——單一群組送不出去不該影響其他群組。
        """
        if self._app is None:
            log.warning("Telegram 尚未就緒，略過主動推播 chat_id=%s", chat_id)
            return False
        ok = False
        for chunk in split_message(text):
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=thread_id,
                )
                ok = True
            except Exception:
                log.warning("主動推播送出失敗 chat_id=%s thread_id=%s", chat_id, thread_id, exc_info=True)
                break
        return ok

    async def _react(self, chat_id: int, message_id: int, emoji: str) -> None:
        """對觸發訊息設定 emoji reaction（進度回饋）；失敗（如群組未開放該表情）靜默略過。"""
        assert self._app is not None
        with contextlib.suppress(Exception):
            await self._app.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )

    async def _reply(self, message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> int | None:
        """以 HTML parse mode 分段回覆；每段 reply 至觸發訊息並帶 thread_id（TRIG-5/8）。

        回傳第一段送出的 message_id（供 reply-chain 保存反問狀態）；全數失敗則回 None。
        reply_markup（如「重試」按鈕）只掛在第一段。
        text 須為合法 HTML：靜態模板可含 <b> 等標籤；動態/外部內容由呼叫端先 escape_html
        （指令回覆在 commands.py escape、業務回覆在 _handle_business escape）。
        """
        assert self._app is not None
        reply_params = ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True)
        first_id: int | None = None
        for chunk in split_message(text):
            with contextlib.suppress(Exception):
                sent = await self._app.bot.send_message(
                    chat_id=message.chat.id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    reply_parameters=reply_params,
                    message_thread_id=message.message_thread_id,
                    reply_markup=reply_markup if first_id is None else None,
                )
                if first_id is None:
                    first_id = sent.message_id
        return first_id

    async def _edit(
        self, chat_id: int, message_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ) -> bool:
        """就地更新小石自己的訊息（HTML）；失敗（訊息過舊、內容相同等）記錄並回 False。"""
        assert self._app is not None
        try:
            await self._app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return True
        except Exception:
            log.warning("就地更新訊息失敗 chat_id=%s message_id=%s", chat_id, message_id, exc_info=True)
            return False
