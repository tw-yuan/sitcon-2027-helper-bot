"""T5：GitLab client — 白名單、scoped 互斥、assignee 落差、開著查詢、重試、attribution。"""

from __future__ import annotations

import collections
from typing import Any

import pytest

from sitcon_bot.services.gitlab_client import (
    CredentialError,
    GitLabAPIError,
    GitLabBackendError,
    GitLabClient,
    LabelNotFoundError,
    label_scope,
    merge_labels,
)

LABELS = [
    "Status::Inbox",
    "Status::Doing",
    "Status::Review",
    "Status::To Do",
    "Team::開發組",
    "Team::行政組",
    "Team::總召組",
    "0913 一籌",
    "0110 站立會議",
]


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


async def _noop_sleep(_: float) -> None:
    return None


class FakeBackend:
    def __init__(self, labels: list[str]) -> None:
        self.labels = list(labels)
        self.issues: dict[int, dict[str, Any]] = {}
        self.notes: dict[int, list[dict[str, Any]]] = {}
        self.calls: collections.Counter[str] = collections.Counter()
        self.errors: dict[str, list[Exception]] = {}
        self.applied_assignees: list[int] | None = None
        self.last_create_payload: dict[str, Any] | None = None
        self.last_update_payload: dict[str, Any] | None = None
        self.last_filters: dict[str, Any] | None = None
        self._next_iid = 100

    def _maybe_error(self, method: str) -> None:
        q = self.errors.get(method)
        if q:
            raise q.pop(0)

    @staticmethod
    def _users(ids: list[int]) -> list[dict[str, Any]]:
        return [{"id": i, "username": f"u{i}", "name": f"n{i}"} for i in ids]

    def list_labels(self) -> list[str]:
        self.calls["list_labels"] += 1
        self._maybe_error("list_labels")
        return list(self.labels)

    def create_label(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls["create_label"] += 1
        self._maybe_error("create_label")
        self.labels.append(payload["name"])
        return dict(payload)

    def update_label(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls["update_label"] += 1
        self._maybe_error("update_label")
        final = payload.get("new_name", name)
        self.labels = [final if label == name else label for label in self.labels]
        return {"name": final, **{k: v for k, v in payload.items() if k != "new_name"}}

    def delete_label(self, name: str) -> None:
        self.calls["delete_label"] += 1
        self._maybe_error("delete_label")
        self.labels.remove(name)

    def create_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls["create_issue"] += 1
        self._maybe_error("create_issue")
        self.last_create_payload = payload
        iid = self._next_iid
        self._next_iid += 1
        labels = payload["labels"].split(",") if payload.get("labels") else []
        req = payload.get("assignee_ids", [])
        applied = self.applied_assignees if self.applied_assignees is not None else req
        issue = {
            "iid": iid,
            "web_url": f"https://gitlab.com/sitcon-tw/2027/-/issues/{iid}",
            "title": payload["title"],
            "description": payload.get("description"),
            "labels": labels,
            "assignees": self._users(applied),
            "due_date": payload.get("due_date"),
            "state": "opened",
        }
        self.issues[iid] = issue
        return dict(issue)

    def get_issue(self, iid: int) -> dict[str, Any]:
        self.calls["get_issue"] += 1
        self._maybe_error("get_issue")
        return dict(self.issues[iid])

    def update_issue(self, iid: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls["update_issue"] += 1
        self._maybe_error("update_issue")
        self.last_update_payload = payload
        issue = self.issues[iid]
        if "title" in payload:
            issue["title"] = payload["title"]
        if "description" in payload:
            issue["description"] = payload["description"]
        if "labels" in payload:
            issue["labels"] = payload["labels"].split(",") if payload["labels"] else []
        if "assignee_ids" in payload:
            ids = [i for i in payload["assignee_ids"] if i != 0]
            issue["assignees"] = self._users(ids)
        if "due_date" in payload:
            issue["due_date"] = payload["due_date"] or None
        return dict(issue)

    def list_issue_notes(self, iid: int) -> list[dict[str, Any]]:
        self.calls["list_issue_notes"] += 1
        return [dict(n) for n in self.notes.get(iid, [])]

    def create_issue_note(self, iid: int, body: str) -> dict[str, Any]:
        self.calls["create_issue_note"] += 1
        note = {"id": len(self.notes.get(iid, [])) + 1, "body": body, "system": False, "author": {"username": "bot"}}
        self.notes.setdefault(iid, []).append(note)
        return dict(note)

    def list_issues(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls["list_issues"] += 1
        self._maybe_error("list_issues")
        self.last_filters = filters
        result = []
        for iss in self.issues.values():
            if filters.get("state") == "opened" and iss["state"] != "opened":
                continue
            if filters.get("due_date") == "any" and not iss.get("due_date"):
                continue
            if filters.get("labels"):
                want = set(filters["labels"].split(","))
                if not want.issubset(set(iss["labels"])):
                    continue
            result.append(dict(iss))
        return result


def _client(backend: FakeBackend, **kw: Any) -> GitLabClient:
    return GitLabClient(backend, sleep=_noop_sleep, **kw)


# ------------------------------------------------------------------ #
# 純函式
# ------------------------------------------------------------------ #
def test_label_scope() -> None:
    assert label_scope("Status::Review") == "Status"
    assert label_scope("Team::開發組") == "Team"
    assert label_scope("0913 一籌") is None


def test_merge_labels_scoped_exclusion() -> None:
    out = merge_labels(["Status::Inbox", "Team::開發組"], ["Status::Doing"], [])
    assert set(out) == {"Status::Doing", "Team::開發組"}


def test_merge_labels_remove() -> None:
    out = merge_labels(["Status::Doing", "0913 一籌"], [], ["0913 一籌"])
    assert out == ["Status::Doing"]


# ------------------------------------------------------------------ #
# GL-10 / GL-12：白名單與近似候選
# ------------------------------------------------------------------ #
async def test_create_rejects_unknown_label_with_candidates() -> None:
    c = _client(FakeBackend(LABELS))
    with pytest.raises(LabelNotFoundError) as ei:
        await c.create_issue(
            title="t", description="d", label_names=["Status::Inboxx"],
            assignee_ids=[], due_date=None, requester="@yuan",
        )
    assert "Status::Inbox" in ei.value.candidates


async def test_create_resolves_label_normalization() -> None:
    b = FakeBackend(LABELS)
    res = await _client(b).create_issue(
        title="t", description="d", label_names=["status::inbox"],  # 大小寫不同
        assignee_ids=[], due_date=None, requester="@yuan",
    )
    assert "Status::Inbox" in res.issue.labels


async def test_card_ops_never_create_labels_implicitly() -> None:
    # GL-10（卡片操作）：未知 label 一律拋錯，不會偷偷呼叫 create_label 補建
    b = FakeBackend(LABELS)
    with pytest.raises(LabelNotFoundError):
        await _client(b).create_issue(
            title="t", description="d", label_names=["不存在的label"],
            assignee_ids=[], due_date=None, requester="@yuan",
        )
    assert b.calls["create_label"] == 0


# ------------------------------------------------------------------ #
# label 管理（2026-08-02 追加需求）
# ------------------------------------------------------------------ #
async def test_create_label_refreshes_whitelist() -> None:
    b = FakeBackend(LABELS)
    c = _client(b)
    name = await c.create_label(name="Status::Blocked", color="#ff0000")
    assert name == "Status::Blocked"
    assert b.calls["create_label"] == 1
    # 白名單立即可用：後續建卡不需等 TTL
    idx = await c.get_label_index()
    assert idx.resolve("status::blocked") == "Status::Blocked"


async def test_create_label_rejects_duplicate() -> None:
    from sitcon_bot.services.gitlab_client import GitLabError

    b = FakeBackend(LABELS)
    with pytest.raises(GitLabError, match="已存在"):
        await _client(b).create_label(name="status::inbox", color="#fff")  # 正規化後同名
    assert b.calls["create_label"] == 0


async def test_update_label_rename_and_refresh() -> None:
    b = FakeBackend(LABELS)
    c = _client(b)
    final = await c.update_label("0913 一籌", new_name="0920 一籌")
    assert final == "0920 一籌"
    idx = await c.get_label_index()
    assert idx.resolve("0920 一籌") is not None
    assert idx.resolve("0913 一籌") is None


async def test_update_label_rename_clash_rejected() -> None:
    from sitcon_bot.services.gitlab_client import GitLabError

    b = FakeBackend(LABELS)
    with pytest.raises(GitLabError, match="已存在"):
        await _client(b).update_label("Status::Inbox", new_name="Status::Doing")
    assert b.calls["update_label"] == 0


async def test_update_label_unknown_gives_candidates() -> None:
    b = FakeBackend(LABELS)
    with pytest.raises(LabelNotFoundError) as ei:
        await _client(b).update_label("Status::Inboxx", color="#000")
    assert "Status::Inbox" in ei.value.candidates


async def test_delete_label_resolves_and_refreshes() -> None:
    b = FakeBackend(LABELS)
    c = _client(b)
    deleted = await c.delete_label("status::review")  # 正規化解析到正式名稱
    assert deleted == "Status::Review"
    assert "Status::Review" not in b.labels
    idx = await c.get_label_index()
    assert idx.resolve("Status::Review") is None


async def test_delete_label_unknown_raises() -> None:
    b = FakeBackend(LABELS)
    with pytest.raises(LabelNotFoundError):
        await _client(b).delete_label("不存在")
    assert b.calls["delete_label"] == 0


# ------------------------------------------------------------------ #
# GL-8：attribution
# ------------------------------------------------------------------ #
async def test_create_appends_attribution() -> None:
    b = FakeBackend(LABELS)
    await _client(b).create_issue(
        title="t", description="場地保證金", label_names=["Status::Inbox"],
        assignee_ids=[], due_date=None, requester="@yuan",
    )
    assert "requested by @yuan" in b.last_create_payload["description"]


async def test_comment_appends_attribution_and_filters_system() -> None:
    b = FakeBackend(LABELS)
    c = _client(b)
    note = await c.comment_issue(50, "場地已確認", requester="@leaf")
    assert "requested by @leaf" in note.body

    b.notes[50] = [
        {"id": 1, "body": "changed status to Doing", "system": True, "author": {"username": "gitlab"}},
        {"id": 2, "body": "真人留言", "system": False, "author": {"username": "yuan"}},
    ]
    human = await c.get_issue_notes(50)  # GL-19：過濾系統訊息
    assert [n.body for n in human] == ["真人留言"]


# ------------------------------------------------------------------ #
# GL-6：多 assignee 落差
# ------------------------------------------------------------------ #
async def test_multi_assignee_discrepancy_detected() -> None:
    b = FakeBackend(LABELS)
    b.applied_assignees = [1, 2]  # API 只套用 1、2
    res = await _client(b).create_issue(
        title="t", description="d", label_names=["Status::Inbox"],
        assignee_ids=[1, 2, 3], due_date=None, requester="@yuan",
    )
    assert res.missing_assignees == [3]


# ------------------------------------------------------------------ #
# GL-13：scoped 互斥（編輯）+ GL-16：無 state_event
# ------------------------------------------------------------------ #
async def test_update_scoped_exclusion_and_diff() -> None:
    b = FakeBackend(LABELS)
    b.issues[42] = {
        "iid": 42, "web_url": "u", "title": "舊標題",
        "description": "d", "labels": ["Status::Inbox", "Team::開發組"],
        "assignees": [], "due_date": None, "state": "opened",
    }
    res = await _client(b).update_issue(42, add_labels=["Status::Doing"], title="新標題")
    assert set(res.issue.labels) == {"Status::Doing", "Team::開發組"}
    assert res.labels_added == ["Status::Doing"]
    assert res.labels_removed == ["Status::Inbox"]
    assert res.title_changed is True
    # GL-16：payload 永不含 state_event
    assert "state_event" not in b.last_update_payload


async def test_update_no_change_returns_no_diff() -> None:
    b = FakeBackend(LABELS)
    b.issues[42] = {
        "iid": 42, "web_url": "u", "title": "標題", "description": "d",
        "labels": ["Status::Inbox"], "assignees": [], "due_date": None, "state": "opened",
    }
    res = await _client(b).update_issue(42, title="標題")  # 同名 → 無變更
    assert res.any_change() is False
    assert b.calls["update_issue"] == 0  # 無 payload 時不呼叫 API


# ------------------------------------------------------------------ #
# GL-21/GL-22：條件查詢與「開著」
# ------------------------------------------------------------------ #
async def test_open_only_excludes_review_and_closed() -> None:
    b = FakeBackend(LABELS)
    b.issues = {
        1: {"iid": 1, "web_url": "u1", "title": "a", "description": None,
            "labels": ["Team::行政組", "Status::Doing"], "assignees": [], "due_date": None, "state": "opened"},
        2: {"iid": 2, "web_url": "u2", "title": "b", "description": None,
            "labels": ["Team::行政組", "Status::Review"], "assignees": [], "due_date": None, "state": "opened"},
        3: {"iid": 3, "web_url": "u3", "title": "c", "description": None,
            "labels": ["Team::行政組"], "assignees": [], "due_date": None, "state": "closed"},
    }
    issues = await _client(b).search_issues(label_filters=["Team::行政組"], open_only=True)
    assert [i.iid for i in issues] == [1]  # Review 排除、closed 排除
    assert b.last_filters["state"] == "opened"


# ------------------------------------------------------------------ #
# NT-11：開著卡片查詢（2026-08-06 修訂：不限已過期）
# ------------------------------------------------------------------ #
def _bare_issue(iid: int, due: str | None, state: str = "opened", labels: list[str] | None = None) -> dict[str, Any]:
    return {"iid": iid, "web_url": f"u{iid}", "title": f"t{iid}", "description": None,
            "labels": labels or [], "assignees": [], "due_date": due, "state": state}


async def test_open_cards_filters_and_sorts() -> None:
    b = FakeBackend(LABELS)
    b.issues = {
        1: _bare_issue(1, "2026-08-01", labels=["Status::To Do"]),   # 未到期也列入
        2: _bare_issue(2, "2026-07-31", labels=["Status::Doing"]),
        3: _bare_issue(3, "2026-07-25", labels=["Status::Inbox"]),   # 過期最久 → 排最前
        4: _bare_issue(4, "2026-07-28", "closed"),                   # 已關閉 → 排除
        5: _bare_issue(5, None, labels=["Status::Waiting"]),         # 無到期日也列入，殿後
        6: _bare_issue(6, "2026-07-25"),                             # 同日到期 → 以 iid 穩定排序
        7: _bare_issue(7, "2026-07-20", labels=["Status::Review"]),  # Review 不算開著（GL-22）→ 排除
    }
    issues = await _client(b).open_cards()
    assert [i.iid for i in issues] == [3, 6, 2, 1, 5]
    assert b.last_filters == {"state": "opened"}


# ------------------------------------------------------------------ #
# GL-11：label 快取 TTL
# ------------------------------------------------------------------ #
async def test_label_cache_ttl() -> None:
    clock = FakeClock()
    b = FakeBackend(LABELS)
    c = _client(b, label_cache_ttl=600, clock=clock)
    await c.get_label_index()
    await c.get_label_index()
    assert b.calls["list_labels"] == 1
    clock.advance(601)
    await c.get_label_index()
    assert b.calls["list_labels"] == 2


# ------------------------------------------------------------------ #
# EC-9 / EC-10：重試與憑證錯誤
# ------------------------------------------------------------------ #
async def test_retry_on_5xx_then_success() -> None:
    b = FakeBackend(LABELS)
    b.errors["list_labels"] = [GitLabBackendError(503, "x"), GitLabBackendError(503, "x")]
    idx = await _client(b).get_label_index()
    assert idx.names
    assert b.calls["list_labels"] == 3  # 初次 + 2 retries


async def test_retry_exhausted_raises_api_error() -> None:
    b = FakeBackend(LABELS)
    b.errors["list_labels"] = [GitLabBackendError(500, "x")] * 5
    with pytest.raises(GitLabAPIError):
        await _client(b).get_label_index()
    assert b.calls["list_labels"] == 3


async def test_401_raises_credential_error() -> None:
    b = FakeBackend(LABELS)
    b.errors["list_labels"] = [GitLabBackendError(401, "unauth")]
    with pytest.raises(CredentialError):
        await _client(b).get_label_index()
