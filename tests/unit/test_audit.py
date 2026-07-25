"""T2：稽核紀錄寫入與讀取（LOG-1～LOG-4）。"""

from __future__ import annotations

from datetime import datetime

from sitcon_bot.storage.audit import STATUS_ERROR, AuditLog
from sitcon_bot.storage.db import Database


async def test_record_and_read_back(db: Database) -> None:
    audit = AuditLog(db)
    rid = await audit.record(
        chat_id=-1001,
        user_id=7,
        trigger_text="小石 幫我開一張卡：官網倒數計時器壞了",
        action="gitlab.create_issue",
        chat_title="開發群",
        username="yuan",
        target="issue#42",
        detail={"labels": ["Status::Inbox", "Team::開發組"], "assignees": ["yuan"]},
        status="ok",
    )
    entry = await audit.get(rid)
    assert entry is not None
    assert entry.action == "gitlab.create_issue"
    assert entry.target == "issue#42"
    assert entry.chat_id == -1001
    # detail 為 JSON，含中文可正確 roundtrip
    assert entry.detail == {"labels": ["Status::Inbox", "Team::開發組"], "assignees": ["yuan"]}


async def test_ts_is_utc(db: Database) -> None:
    audit = AuditLog(db)
    rid = await audit.record(chat_id=1, user_id=1, trigger_text="x", action="drive.search")
    entry = await audit.get(rid)
    assert entry is not None
    dt = datetime.fromisoformat(entry.ts)
    assert dt.utcoffset() is not None
    assert dt.utcoffset().total_seconds() == 0  # UTC（AGENTS 6.7）


async def test_recent_orders_newest_first(db: Database) -> None:
    audit = AuditLog(db)
    ids = []
    for i in range(3):
        ids.append(await audit.record(chat_id=1, user_id=1, trigger_text=f"t{i}", action="x"))
    recent = await audit.recent(limit=2)
    assert [e.id for e in recent] == [ids[2], ids[1]]


async def test_error_status_and_null_detail(db: Database) -> None:
    audit = AuditLog(db)
    rid = await audit.record(
        chat_id=None,
        user_id=None,
        trigger_text=None,
        action="error",
        status=STATUS_ERROR,
        error="GitLab 502",
    )
    entry = await audit.get(rid)
    assert entry is not None
    assert entry.status == "error"
    assert entry.error == "GitLab 502"
    assert entry.detail is None
