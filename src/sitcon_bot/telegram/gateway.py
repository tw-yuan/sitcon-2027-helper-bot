"""Telegram gateway：長輪詢、TRIG-1 過濾、指令與業務分派、分段送出、typing。

privacy off 後 bot 收到授權群組的所有訊息（TRIG-2），過濾僅在本機記憶體進行（TRIG-1）。
非觸發訊息一律當場丟棄，不入儲存、不送 LLM（LOG-3）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram import InputMediaPhoto, Message, MessageEntity, ReactionTypeEmoji, ReplyParameters, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from ..agent.context import PendingStore
from ..auth.groups import GroupStore
from ..settings import Settings
from ..storage.audit import AuditLog
from .commands import CommandHandlers
from .formatting import escape_html, split_message
from .routing import Action, classify_trigger, route

log = logging.getLogger(__name__)

# 重啟後 getUpdates 可能帶回積壓訊息；超過此時限的觸發視為過期並丟棄（AGENTS 6.5）。
STALE_AFTER = timedelta(minutes=5)
TYPING_INTERVAL = 4.0  # 秒；< 5 秒以維持 typing 指示（NFR-1 / AGENTS 4.6）

# 進度回饋：收到即 👀 reaction + typing，完成後改 ✅。不送佔位訊息（會讓使用者漏收最終回覆通知）。
REACT_RECEIVED = "👀"
REACT_DONE = "✅"
GENERIC_ERROR = "小石遇到未預期的錯誤，請稍後再試或通知管理員。"


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
    # 純 reply-chain 脈絡：resume=回覆 ask_user 問句時的待答狀態；reply_context=被回覆訊息內容
    resume: Any = None
    reply_context: str | None = None


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
    ) -> None:
        self._settings = settings
        self._groups = groups
        self._audit = audit
        self._commands = commands
        self._business_handler = business_handler or _echo_handler
        self._pending = pending_store or PendingStore(settings.context_ttl_seconds)
        self._app: Application | None = None
        self.bot_id: int = 0
        self.bot_username: str | None = None

    # ------------------------------------------------------------------ #
    # 生命週期
    # ------------------------------------------------------------------ #
    async def run(self, stop: asyncio.Event) -> None:
        """建立 Application、getMe 檢查、開始長輪詢，直到 stop 被設定。"""
        token = self._settings.telegram_bot_token.get_secret_value()
        app = ApplicationBuilder().token(token).build()
        app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, self._on_message))
        self._app = app

        await app.initialize()
        me = await app.bot.get_me()
        self.bot_id = me.id
        self.bot_username = me.username
        log.info("Telegram 連線成功：@%s (id=%s)", me.username, me.id)

        await app.start()
        await app.updater.start_polling(drop_pending_updates=False, allowed_updates=["message"])
        log.info("開始長輪詢；等待訊息…")
        try:
            await stop.wait()
        finally:
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
        elif action is Action.BUSINESS:
            await self._handle_business(message, text)

    async def _handle_business(self, message: Message, text: str) -> None:
        user = message.from_user
        assert user is not None
        # 純 reply-chain 脈絡：回覆 ask_user 問句 → 取待答狀態續接；回覆其他訊息 → 帶其內容當脈絡
        resume = None
        reply_context = None
        reply_to = message.reply_to_message
        if reply_to is not None:
            resume = self._pending.take(message.chat.id, reply_to.message_id)
            if resume is None:
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
            reply_context=reply_context,
        )
        # 收到即回饋：👀 reaction + typing（不送佔位訊息，避免使用者漏收最終回覆通知）
        await self._react(message, REACT_RECEIVED)
        try:
            async with self._typing(req.chat_id, req.thread_id):
                result = await self._business_handler(req)
        except Exception:  # 業務層未預期錯誤：回一則錯誤說明，不中斷長輪詢
            log.exception("業務處理未預期錯誤 chat_id=%s", req.chat_id)
            await self._reply(message, escape_html(GENERIC_ERROR))
            await self._audit.record(
                chat_id=req.chat_id, chat_title=req.chat_title, user_id=req.user_id,
                username=req.username, trigger_text=text, action="error", target=None,
                detail=None, status="error", error="unhandled",
            )
            return
        # 業務回覆為 LLM 產生的動態內容 → escape 後才以 HTML 送出（避免破壞 HTML / 注入）
        reply_mid = await self._reply(message, escape_html(result.reply))
        if result.status == "ok":
            if result.media:  # 代表縮圖（photo_search）→ 以圖片送出
                await self._send_media(message, result.media)
            await self._react(message, REACT_DONE)  # 完成 → ✅（clarify/error 維持 👀）
        # 反問待答狀態以「問句 message_id」為鍵保存；使用者回覆該問句時才續接
        if result.pending is not None and reply_mid is not None:
            self._pending.put(message.chat.id, reply_mid, result.pending)
        # LOG-1：記錄觸發互動（動作、目標、結果狀態、錯誤摘要）
        await self._audit.record(
            chat_id=req.chat_id,
            chat_title=req.chat_title,
            user_id=req.user_id,
            username=req.username,
            trigger_text=text,
            action=result.action,
            target=result.target,
            detail=result.detail,
            status=result.status,
            error=result.error,
        )

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

    async def _send_media(self, message: Message, media: list[Any]) -> None:
        """送出代表縮圖（單張用 send_photo、多張用媒體群組）；失敗靜默略過（連結已在文字回覆內）。

        Telegram 會自行抓取圖片 URL；圖組上限 10 張。caption 為純文字（含 Flickr 連結，會自動變可點）。
        """
        assert self._app is not None
        items = [m for m in media if getattr(m, "url", None)][:10]
        if not items:
            return
        reply_params = ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True)
        with contextlib.suppress(Exception):
            if len(items) == 1:
                await self._app.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=items[0].url,
                    caption=items[0].caption or None,
                    reply_parameters=reply_params,
                    message_thread_id=message.message_thread_id,
                )
            else:
                group = [InputMediaPhoto(media=m.url, caption=m.caption or None) for m in items]
                await self._app.bot.send_media_group(
                    chat_id=message.chat.id,
                    media=group,
                    reply_parameters=reply_params,
                    message_thread_id=message.message_thread_id,
                )

    async def _react(self, message: Message, emoji: str) -> None:
        """對觸發訊息設定 emoji reaction（進度回饋）；失敗（如群組未開放該表情）靜默略過。"""
        assert self._app is not None
        with contextlib.suppress(Exception):
            await self._app.bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )

    async def _reply(self, message: Message, text: str) -> int | None:
        """以 HTML parse mode 分段回覆；每段 reply 至觸發訊息並帶 thread_id（TRIG-5/8）。

        回傳第一段送出的 message_id（供 reply-chain 保存反問狀態）；全數失敗則回 None。
        text 須為合法 HTML：靜態模板可含 <b> 等標籤；動態/外部內容由呼叫端先 escape_html
        （指令回覆在 commands.py escape、業務回覆在 _handle_business escape）。
        """
        assert self._app is not None
        reply_params = ReplyParameters(
            message_id=message.message_id, allow_sending_without_reply=True
        )
        first_id: int | None = None
        for chunk in split_message(text):
            with contextlib.suppress(Exception):
                sent = await self._app.bot.send_message(
                    chat_id=message.chat.id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    reply_parameters=reply_params,
                    message_thread_id=message.message_thread_id,
                )
                if first_id is None:
                    first_id = sent.message_id
        return first_id
