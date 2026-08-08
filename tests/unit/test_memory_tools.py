"""群組記憶工具：remember／list／forget 的上限防呆、跨群隔離與人話回覆。"""

from __future__ import annotations

from sitcon_bot.agent.tools.base import ToolContext
from sitcon_bot.agent.tools.memory_tools import (
    MemoryForgetArgs,
    MemoryForgetTool,
    MemoryListArgs,
    MemoryListTool,
    MemoryRememberArgs,
    MemoryRememberTool,
    build_memory_tools,
)
from sitcon_bot.storage.db import Database
from sitcon_bot.storage.memories import MAX_CONTENT_CHARS, MAX_MEMORIES_PER_GROUP, GroupMemoryStore

CHAT = -1001


def _ctx(chat_id: int = CHAT) -> ToolContext:
    return ToolContext(chat_id=chat_id, thread_id=None, user_id=42, username="yuan", text="小石記住")


async def test_remember_then_list(db: Database) -> None:
    store = GroupMemoryStore(db)
    out = await MemoryRememberTool(store).run(MemoryRememberArgs(content="  開會前一天要提醒大家  "), _ctx())
    assert "已記住" in out and "開會前一天要提醒大家" in out
    listed = await MemoryListTool(store).run(MemoryListArgs(), _ctx())
    assert "開會前一天要提醒大家" in listed
    assert "@yuan" in listed  # 顯示設定者
    # 內容有 strip
    assert (await store.list_for(CHAT))[0].content == "開會前一天要提醒大家"


async def test_remember_rejects_blank_and_too_long(db: Database) -> None:
    tool = MemoryRememberTool(GroupMemoryStore(db))
    assert "空的" in await tool.run(MemoryRememberArgs(content="   "), _ctx())
    out = await tool.run(MemoryRememberArgs(content="長" * (MAX_CONTENT_CHARS + 1)), _ctx())
    assert "太長" in out
    assert await GroupMemoryStore(db).count(CHAT) == 0


async def test_remember_enforces_per_group_limit(db: Database) -> None:
    store = GroupMemoryStore(db)
    for i in range(MAX_MEMORIES_PER_GROUP):
        await store.add(CHAT, f"事項 {i}")
    out = await MemoryRememberTool(store).run(MemoryRememberArgs(content="再一件"), _ctx())
    assert "上限" in out
    assert await store.count(CHAT) == MAX_MEMORIES_PER_GROUP
    # 別群不受影響
    out2 = await MemoryRememberTool(store).run(MemoryRememberArgs(content="他群可記"), _ctx(chat_id=-2))
    assert "已記住" in out2


async def test_list_empty(db: Database) -> None:
    out = await MemoryListTool(GroupMemoryStore(db)).run(MemoryListArgs(), _ctx())
    assert "沒有任何記憶" in out


async def test_forget_deletes_and_reports_content(db: Database) -> None:
    store = GroupMemoryStore(db)
    m = await store.add(CHAT, "過時的慣例")
    out = await MemoryForgetTool(store).run(MemoryForgetArgs(memory_id=m.id), _ctx())
    assert "已忘記" in out and "過時的慣例" in out
    assert await store.list_for(CHAT) == []


async def test_forget_missing_or_cross_group(db: Database) -> None:
    store = GroupMemoryStore(db)
    other = await store.add(-2, "別群的記憶")
    out = await MemoryForgetTool(store).run(MemoryForgetArgs(memory_id=other.id), _ctx())
    assert "沒有" in out  # 拿別群編號刪不到
    assert await store.count(-2) == 1


async def test_build_memory_tools_names(db: Database) -> None:
    names = [t.name for t in build_memory_tools(GroupMemoryStore(db))]
    assert names == ["memory_remember", "memory_list", "memory_forget"]
