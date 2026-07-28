"""管理與說明指令的處理（AUTH-2/3/6/7/8）。

各方法回傳可直接送出的 HTML（gateway 以 parse_mode=HTML 分段送出，不再整段 escape）。
靜態模板可用 <b> 等標籤；動態內容（群組名等）在此以 escape_html 處理，避免破壞 HTML 或注入。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ..auth.groups import GroupStore
from ..settings import Settings
from .formatting import escape_html

log = logging.getLogger(__name__)

# /reload 的重載回呼：回傳一段人話摘要（各項筆數）。T3 尚未接快取，預設 None。
ReloadCallback = Callable[[], Awaitable[str]]

PRIVATE_NOTICE = "小石僅在授權群組內提供服務。"
PRIVATE_AUTHORIZE_REDIRECT = "請在目標群組內執行 /authorize（管理員限定）。"

HELP_TEXT = """<b>小石</b> — SITCON 2027 工作人員助理

在授權群組裡，用一句話就能操作 GitLab 卡片、找雲端硬碟檔案、開/找/改 HackMD 筆記。
觸發方式：@提及我、reply 我的訊息、或用「小石」開頭。

<b>GitLab 卡片</b>
• 小石 幫我開一張卡：官網倒數計時器壞了
• 小石 開一張卡給行政組，標題場地保證金匯款，due 8/15
• 小石 把 #42 改成 Doing，加上 0913 一籌
• 小石 在 #42 留言：場地已確認
• 小石 列出行政組還開著的卡

<b>雲端硬碟（唯讀搜尋）</b>
• 小石 幫我找去年的場地租借合約

<b>照片搜尋（歷年 Flickr 活動照）</b>
• 小石 找幾張講者演講的橫式照片
• 小石 幫我找 Camp 2026 工作坊、有人物的照片

<b>HackMD 筆記</b>
• 小石 開一份 0913 一籌的會議記錄
• 小石 幫行政組開今天的會議記錄
• 小石 找上次討論贊助方案的那份文件

管理指令（管理員）：/authorize /revoke /list_groups /reload"""

START_TEXT = "我是小石，SITCON 2027 的工作人員助理。輸入 /help 看我能做什麼。"


class CommandHandlers:
    """處理管理與說明指令。"""

    def __init__(
        self,
        settings: Settings,
        groups: GroupStore,
        reload_cb: ReloadCallback | None = None,
    ) -> None:
        self._settings = settings
        self._groups = groups
        self._reload_cb = reload_cb

    async def authorize(self, chat_id: int, title: str | None) -> str:
        newly = await self._groups.authorize(chat_id, title, self._settings.telegram_admin_id)
        if newly:
            name = escape_html(title) if title else "(未命名群組)"
            return f"✅ 已授權此群組「{name}」（chat_id={chat_id}）。群組成員現在可以使用小石。"
        return "此群組已授權。"

    async def revoke(self, chat_id: int) -> str:
        existed = await self._groups.revoke(chat_id)
        if existed:
            return "已撤銷此群組的授權，小石在此群組將停止服務。"
        return "此群組原本就未授權。"

    async def list_groups(self) -> str:
        groups = await self._groups.list_groups()
        if not groups:
            return "目前沒有授權任何群組。"
        lines = [f"目前授權群組（{len(groups)}）："]
        lines += [f"• {escape_html(g.title) if g.title else '(未命名)'}（{g.chat_id}）" for g in groups]
        return "\n".join(lines)

    async def reload(self) -> str:
        if self._reload_cb is None:
            return "已重載（目前尚無可重載的快取）。"
        summary = await self._reload_cb()
        return f"已重載：{summary}"

    def help_text(self) -> str:
        return HELP_TEXT

    def start_text(self) -> str:
        return START_TEXT

    def private_notice(self) -> str:
        return PRIVATE_NOTICE

    def private_authorize_redirect(self) -> str:
        return PRIVATE_AUTHORIZE_REDIRECT
