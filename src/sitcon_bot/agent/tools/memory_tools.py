"""群組記憶工具：讓使用者用自然語言設定小石在本群要長期記住的事項。

記憶存 SQLite（storage/memories.py），並由 PromptBuilder 注入該群的 system prompt，
所以「記住」後下一則觸發就會生效。刪除（忘記）屬破壞性操作：必須指到明確編號，
模糊時 LLM 應先 memory_list 或 ask_user 確認。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...storage.memories import MAX_CONTENT_CHARS, MAX_MEMORIES_PER_GROUP, GroupMemory, GroupMemoryStore
from .base import Tool, ToolContext


def _format_memory(m: GroupMemory) -> str:
    who = f"@{m.created_by_name}" if m.created_by_name else (str(m.created_by) if m.created_by else "?")
    date = (m.created_at or "")[:10]
    return f"#{m.id} {m.content}（{who} 於 {date} 設定）"


class MemoryRememberArgs(BaseModel):
    content: str = Field(min_length=1, description="要記住的事項（一句話講完的純文字；一次一件事）")


class MemoryRememberTool(Tool):
    name = "memory_remember"
    description = (
        "把一件事記進本群組的長期記憶（重啟不遺失），之後在本群做事時你會自動看到並遵守。"
        "適用於使用者要求「記住／以後都／這群的慣例是…」的偏好、慣例或常用資訊。"
        "一次記一件事；要記多件就多次呼叫（同一回合並行）。"
    )
    args_model = MemoryRememberArgs

    def __init__(self, store: GroupMemoryStore) -> None:
        self._store = store

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, MemoryRememberArgs)
        content = args.content.strip()
        if not content:
            return "記憶內容是空的，請提供要記住的事項。"
        if len(content) > MAX_CONTENT_CHARS:
            return f"這條太長了（{len(content)} 字，上限 {MAX_CONTENT_CHARS} 字），請濃縮成重點再記。"
        if await self._store.count(ctx.chat_id) >= MAX_MEMORIES_PER_GROUP:
            return (
                f"本群記憶已達上限（{MAX_MEMORIES_PER_GROUP} 筆）。請先用 memory_list 檢視、"
                "以 memory_forget 刪掉過時的，再記新的。"
            )
        m = await self._store.add(ctx.chat_id, content, by=ctx.user_id, by_name=ctx.username)
        return f"已記住（#{m.id}）：{content}"


class MemoryListArgs(BaseModel):
    pass


class MemoryListTool(Tool):
    name = "memory_list"
    description = (
        "列出本群組目前的所有記憶事項（含編號）。使用者問「你記得什麼／目前的群組記憶」"
        "或要刪除但沒指明哪一條時使用。無參數。"
    )
    args_model = MemoryListArgs

    def __init__(self, store: GroupMemoryStore) -> None:
        self._store = store

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        memories = await self._store.list_for(ctx.chat_id)
        if not memories:
            return "本群目前沒有任何記憶事項。"
        lines = [f"本群記憶（{len(memories)} 筆，上限 {MAX_MEMORIES_PER_GROUP}）："]
        lines += [f"- {_format_memory(m)}" for m in memories]
        return "\n".join(lines)


class MemoryForgetArgs(BaseModel):
    memory_id: int = Field(description="要刪除的記憶編號（#N 的 N；見 system prompt 的記憶清單或 memory_list）")


class MemoryForgetTool(Tool):
    name = "memory_forget"
    description = (
        "刪除本群組的一筆記憶（破壞性操作）。只有使用者明確指到某一條時才執行；"
        "指涉模糊（如「忘掉那件事」對到多條）時先用 memory_list 對照、必要時 ask_user 確認。"
        "刪錯無法復原，回報時附上被刪的內容。"
    )
    args_model = MemoryForgetArgs

    def __init__(self, store: GroupMemoryStore) -> None:
        self._store = store

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, MemoryForgetArgs)
        deleted = await self._store.delete(ctx.chat_id, args.memory_id)
        if deleted is None:
            return f"本群沒有 #{args.memory_id} 這筆記憶（可能已刪除或編號有誤），可用 memory_list 確認。"
        return f"已忘記 #{deleted.id}：{deleted.content}"


def build_memory_tools(store: GroupMemoryStore) -> list[Tool]:
    return [MemoryRememberTool(store), MemoryListTool(store), MemoryForgetTool(store)]
