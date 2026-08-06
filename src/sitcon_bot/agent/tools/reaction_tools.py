"""愛心 reaction 工具：讓 LLM 自行判斷何時對觸發訊息按 ❤。

只設定 ctx.reaction 旗標，實際送出在 gateway 回覆完成時（取代預設的 ✅）——
Telegram bot 對一則訊息只能掛一個 reaction，處理中已佔用 👀，故不能即時另送。
"""

from __future__ import annotations

from pydantic import BaseModel

from .base import Tool, ToolContext

# Telegram reaction 只接受官方清單內的 emoji；愛心是 U+2764「❤」不帶變體選擇子（VS16），
# 寫成「❤️」會被 API 拒絕。
HEART = "❤"


class ReactHeartArgs(BaseModel):
    pass


class ReactHeartTool(Tool):
    name = "react_heart"
    description = (
        "對使用者這則訊息按 ❤ 愛心 reaction。何時按由你自行判斷：好消息、道謝、值得鼓勵、"
        "溫暖或有趣的訊息都適合；不必每則都按。按了不影響你照常回覆，愛心會在回覆送出時"
        "取代預設的 ✅。無參數。"
    )
    args_model = ReactHeartArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        ctx.reaction = HEART
        return "已對這則訊息按下 ❤（回覆送出時生效）。"


def build_reaction_tools() -> list[Tool]:
    return [ReactHeartTool()]
