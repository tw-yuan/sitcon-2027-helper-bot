"""T3：授權清單流程（AUTH-2/3/6/9）。"""

from __future__ import annotations

from sitcon_bot.auth.groups import GroupStore
from sitcon_bot.storage.db import Database

ADMIN = 42


async def test_authorize_list_revoke_flow(db: Database) -> None:
    store = GroupStore(db)
    await store.load()

    assert store.is_authorized(-100) is False

    # 授權
    newly = await store.authorize(-100, "SITCON 開發群", ADMIN)
    assert newly is True
    assert store.is_authorized(-100) is True

    groups = await store.list_groups()
    assert len(groups) == 1
    assert groups[0].chat_id == -100
    assert groups[0].title == "SITCON 開發群"
    assert groups[0].authorized_by == ADMIN

    # 撤銷
    existed = await store.revoke(-100)
    assert existed is True
    assert store.is_authorized(-100) is False
    assert await store.list_groups() == []


async def test_reauthorize_returns_false_and_updates_title(db: Database) -> None:
    store = GroupStore(db)
    await store.authorize(-1, "舊名", ADMIN)
    newly = await store.authorize(-1, "新名", ADMIN)
    assert newly is False
    groups = await store.list_groups()
    assert len(groups) == 1
    assert groups[0].title == "新名"  # 標題更新


async def test_revoke_nonexistent_returns_false(db: Database) -> None:
    store = GroupStore(db)
    assert await store.revoke(-999) is False


async def test_persistence_across_reload(db: Database) -> None:
    """授權寫入 DB 後，新的 GroupStore.load() 應能還原（AUTH-9）。"""
    store = GroupStore(db)
    await store.authorize(-100, "群一", ADMIN)
    await store.authorize(-200, "群二", ADMIN)

    fresh = GroupStore(db)
    n = await fresh.load()
    assert n == 2
    assert fresh.is_authorized(-100) is True
    assert fresh.is_authorized(-200) is True
    assert fresh.is_authorized(-300) is False
