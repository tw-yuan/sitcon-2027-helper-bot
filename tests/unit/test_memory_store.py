"""群組記憶 store：CRUD、跨群隔離、重啟持久化。"""

from __future__ import annotations

from pathlib import Path

from sitcon_bot.storage.db import Database
from sitcon_bot.storage.memories import GroupMemoryStore

CHAT = -1001
OTHER = -1002


async def test_add_and_list(db: Database) -> None:
    store = GroupMemoryStore(db)
    m = await store.add(CHAT, "這群的卡預設指派給 Yuan", by=42, by_name="yuan")
    assert m.id >= 1
    memories = await store.list_for(CHAT)
    assert [x.content for x in memories] == ["這群的卡預設指派給 Yuan"]
    assert memories[0].created_by == 42
    assert memories[0].created_by_name == "yuan"
    assert memories[0].created_at  # ISO8601 時間戳存在


async def test_list_is_scoped_per_chat(db: Database) -> None:
    store = GroupMemoryStore(db)
    await store.add(CHAT, "A 群的事")
    await store.add(OTHER, "B 群的事")
    assert [m.content for m in await store.list_for(CHAT)] == ["A 群的事"]
    assert [m.content for m in await store.list_for(OTHER)] == ["B 群的事"]


async def test_delete_scoped_to_chat(db: Database) -> None:
    """以 (chat_id, id) 雙鍵刪除：拿別群的編號刪不到自己群以外的記憶。"""
    store = GroupMemoryStore(db)
    mine = await store.add(CHAT, "自己的")
    other = await store.add(OTHER, "別群的")
    assert await store.delete(CHAT, other.id) is None  # 跨群刪除無效
    assert [m.content for m in await store.list_for(OTHER)] == ["別群的"]
    deleted = await store.delete(CHAT, mine.id)
    assert deleted is not None and deleted.content == "自己的"
    assert await store.list_for(CHAT) == []


async def test_delete_missing_returns_none(db: Database) -> None:
    store = GroupMemoryStore(db)
    assert await store.delete(CHAT, 999) is None


async def test_count_and_clear(db: Database) -> None:
    store = GroupMemoryStore(db)
    await store.add(CHAT, "一")
    await store.add(CHAT, "二")
    await store.add(OTHER, "他群")
    assert await store.count(CHAT) == 2
    assert await store.clear(CHAT) == 2
    assert await store.count(CHAT) == 0
    assert await store.count(OTHER) == 1  # 清空不波及別群


async def test_revoke_clears_group_memories(db: Database) -> None:
    """撤銷授權時記憶一併清空（未授權群組不該留任何狀態）。"""
    from sitcon_bot.auth.groups import GroupStore
    from sitcon_bot.settings import Settings
    from sitcon_bot.telegram.commands import CommandHandlers

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token="123:abc",
        telegram_admin_id=42,
        llm_api_key="k",
        gitlab_token="g",
        hackmd_token="h",
        hackmd_team_path="sitcon",
    )
    groups = GroupStore(db)
    await groups.authorize(CHAT, "群", 42)
    store = GroupMemoryStore(db)
    await store.add(CHAT, "慣例一")
    await store.add(CHAT, "慣例二")

    out = await CommandHandlers(settings, groups, memories=store).revoke(CHAT)
    assert "群組記憶 2 筆" in out
    assert await store.count(CHAT) == 0


async def test_persistence_across_reopen(tmp_path: Path) -> None:
    """重啟（重開連線）後記憶保留（NFR-2 精神）。"""
    path = str(tmp_path / "mem.sqlite3")
    db = await Database.connect(path)
    await GroupMemoryStore(db).add(CHAT, "會後要發會議記錄到群組")
    await db.close()

    db2 = await Database.connect(path)
    memories = await GroupMemoryStore(db2).list_for(CHAT)
    assert [m.content for m in memories] == ["會後要發會議記錄到群組"]
    await db2.close()
