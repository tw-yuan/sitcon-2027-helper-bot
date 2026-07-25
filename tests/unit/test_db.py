"""T2：SQLite 建表、寫入查詢、重啟持久化。"""

from __future__ import annotations

from pathlib import Path

from sitcon_bot.storage.db import Database


async def test_schema_and_group_roundtrip(tmp_path: Path) -> None:
    db = await Database.connect(str(tmp_path / "t.sqlite3"))
    await db.execute(
        "INSERT INTO authorized_groups (chat_id, title, authorized_by, authorized_at) VALUES (?, ?, ?, ?)",
        (-1001, "SITCON 2027 開發組", 42, "2026-07-22T00:00:00+00:00"),
    )
    row = await db.fetchone("SELECT * FROM authorized_groups WHERE chat_id = ?", (-1001,))
    assert row is not None
    assert row["title"] == "SITCON 2027 開發組"
    assert row["authorized_by"] == 42
    await db.close()


async def test_persistence_across_reopen(tmp_path: Path) -> None:
    """重啟（重開連線）後資料保留（NFR-2）。"""
    path = str(tmp_path / "persist.sqlite3")
    db = await Database.connect(path)
    await db.execute(
        "INSERT INTO authorized_groups (chat_id, title, authorized_by, authorized_at) VALUES (?, ?, ?, ?)",
        (-500, "群組", 1, "2026-01-01T00:00:00+00:00"),
    )
    await db.close()

    db2 = await Database.connect(path)
    row = await db2.fetchone("SELECT title FROM authorized_groups WHERE chat_id = ?", (-500,))
    assert row is not None and row["title"] == "群組"
    await db2.close()


async def test_connect_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c.sqlite3"
    db = await Database.connect(str(nested))
    assert nested.exists()
    await db.close()


async def test_insert_returns_lastrowid(tmp_path: Path) -> None:
    db = await Database.connect(str(tmp_path / "i.sqlite3"))
    rid = await db.insert("INSERT INTO audit_log (ts, action) VALUES (?, ?)", ("2026-07-22T00:00:00+00:00", "x"))
    assert rid >= 1
    await db.close()
