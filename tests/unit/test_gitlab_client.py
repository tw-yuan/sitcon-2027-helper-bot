"""T5：GitLab client — 白名單、scoped 互斥、native status、assignee 落差、開著查詢、重試、attribution。"""

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
    StatusNotFoundError,
    label_scope,
    merge_labels,
)

LABELS = [
    "Team::開發組",
    "Team::行政組",
    "Team::總召組",
    "0913 一籌",
    "0110 站立會議",
]

# native status（2026-08-17 修訂：狀態自 Status:: label 遷移）
STATUSES = ["Inbox", "Waiting", "Doing", "Review", "To Do"]


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
    def __init__(self, labels: list[str], statuses: list[str] | None = None) -> None:
        self.labels = list(labels)
        self.statuses = list(STATUSES if statuses is None else statuses)
        self.issue_statuses: dict[int, str] = {}  # iid → native status
        self.issues: dict[int, dict[str, Any]] = {}
        self.notes: dict[int, list[dict[str, Any]]] = {}
        self.calls: collections.Counter[str] = collections.Counter()
        self.errors: dict[str, list[Exception]] = {}
        self.applied_assignees: list[int] | None = None
        self.last_create_payload: dict[str, Any] | None = None
        self.last_update_payload: dict[str, Any] | None = None
        self.last_filters: dict[str, Any] | None = None
        self._next_iid = 100
        self.links: dict[int, list[dict[str, Any]]] = {}
        self._next_link_id = 500

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

    def list_statuses(self) -> list[str]:
        self.calls["list_statuses"] += 1
        self._maybe_error("list_statuses")
        return list(self.statuses)

    def get_issue_statuses(self, iids: list[int]) -> dict[int, str | None]:
        self.calls["get_issue_statuses"] += 1
        self._maybe_error("get_issue_statuses")
        return {iid: self.issue_statuses.get(iid) for iid in iids}

    def set_issue_status(self, iid: int, status: str) -> str:
        self.calls["set_issue_status"] += 1
        self._maybe_error("set_issue_status")
        self.issue_statuses[iid] = status
        return status

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

    def list_issue_links(self, iid: int) -> list[dict[str, Any]]:
        self.calls["list_issue_links"] += 1
        self._maybe_error("list_issue_links")
        return [dict(link) for link in self.links.get(iid, [])]

    def create_issue_link(self, iid: int, target_iid: int, link_type: str) -> None:
        self.calls["create_issue_link"] += 1
        self._maybe_error("create_issue_link")
        if target_iid not in self.issues:
            raise GitLabBackendError(404, "404 Issue Not Found")
        if any(link["iid"] == target_iid for link in self.links.get(iid, [])):
            raise GitLabBackendError(409, "409 issues already assigned")
        link_id = self._next_link_id
        self._next_link_id += 1
        inverse = {"blocks": "is_blocked_by", "is_blocked_by": "blocks"}.get(link_type, "relates_to")
        self.links.setdefault(iid, []).append(
            {**self.issues[target_iid], "issue_link_id": link_id, "link_type": link_type}
        )
        if iid in self.issues:  # GitLab 連結為雙向，鏡像另一側
            self.links.setdefault(target_iid, []).append(
                {**self.issues[iid], "issue_link_id": link_id, "link_type": inverse}
            )

    def delete_issue_link(self, iid: int, issue_link_id: int) -> None:
        self.calls["delete_issue_link"] += 1
        self._maybe_error("delete_issue_link")
        for lst in self.links.values():
            lst[:] = [link for link in lst if link["issue_link_id"] != issue_link_id]


def _client(backend: FakeBackend, **kw: Any) -> GitLabClient:
    return GitLabClient(backend, sleep=_noop_sleep, **kw)


# ------------------------------------------------------------------ #
# 純函式
# ------------------------------------------------------------------ #
def test_label_scope() -> None:
    assert label_scope("Team::開發組") == "Team"
    assert label_scope("A::B::C") == "A::B"
    assert label_scope("0913 一籌") is None


def test_merge_labels_scoped_exclusion() -> None:
    out = merge_labels(["Team::開發組", "0913 一籌"], ["Team::行政組"], [])
    assert set(out) == {"Team::行政組", "0913 一籌"}


def test_merge_labels_remove() -> None:
    out = merge_labels(["Team::開發組", "0913 一籌"], [], ["0913 一籌"])
    assert out == ["Team::開發組"]


# ------------------------------------------------------------------ #
# GL-10 / GL-12：白名單與近似候選
# ------------------------------------------------------------------ #
async def test_create_rejects_unknown_label_with_candidates() -> None:
    c = _client(FakeBackend(LABELS))
    with pytest.raises(LabelNotFoundError) as ei:
        await c.create_issue(
            title="t", description="d", label_names=["Team::開發"],
            assignee_ids=[], due_date=None, requester="@yuan",
        )
    assert "Team::開發組" in ei.value.candidates


async def test_create_resolves_label_normalization() -> None:
    b = FakeBackend(LABELS)
    res = await _client(b).create_issue(
        title="t", description="d", label_names=["team::開發組"],  # 大小寫不同
        assignee_ids=[], due_date=None, requester="@yuan",
    )
    assert "Team::開發組" in res.issue.labels


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
    name = await c.create_label(name="Prio::High", color="#ff0000")
    assert name == "Prio::High"
    assert b.calls["create_label"] == 1
    # 白名單立即可用：後續建卡不需等 TTL
    idx = await c.get_label_index()
    assert idx.resolve("prio::high") == "Prio::High"


async def test_create_label_rejects_duplicate() -> None:
    from sitcon_bot.services.gitlab_client import GitLabError

    b = FakeBackend(LABELS)
    with pytest.raises(GitLabError, match="已存在"):
        await _client(b).create_label(name="team::開發組", color="#fff")  # 正規化後同名
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
        await _client(b).update_label("Team::開發組", new_name="Team::行政組")
    assert b.calls["update_label"] == 0


async def test_update_label_unknown_gives_candidates() -> None:
    b = FakeBackend(LABELS)
    with pytest.raises(LabelNotFoundError) as ei:
        await _client(b).update_label("Team::開發", color="#000")
    assert "Team::開發組" in ei.value.candidates


async def test_delete_label_resolves_and_refreshes() -> None:
    b = FakeBackend(LABELS)
    c = _client(b)
    deleted = await c.delete_label("team::行政組")  # 正規化解析到正式名稱
    assert deleted == "Team::行政組"
    assert "Team::行政組" not in b.labels
    idx = await c.get_label_index()
    assert idx.resolve("Team::行政組") is None


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
        title="t", description="場地保證金", label_names=["Team::行政組"],
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
        title="t", description="d", label_names=["Team::開發組"],
        assignee_ids=[1, 2, 3], due_date=None, requester="@yuan",
    )
    assert res.missing_assignees == [3]


# ------------------------------------------------------------------ #
# native status（2026-08-17 修訂）：白名單、建卡設定、編輯流轉、落差
# ------------------------------------------------------------------ #
async def test_create_with_status_normalized_and_applied() -> None:
    b = FakeBackend(LABELS)
    res = await _client(b).create_issue(
        title="t", description="d", label_names=[], assignee_ids=[],
        due_date=None, requester="@yuan", status="doing",  # 大小寫不同 → 正規化解析
    )
    assert res.issue.status == "Doing"
    assert b.issue_statuses[res.issue.iid] == "Doing"
    assert res.missing_status is None


async def test_create_rejects_unknown_status_with_candidates() -> None:
    b = FakeBackend(LABELS)
    with pytest.raises(StatusNotFoundError) as ei:
        await _client(b).create_issue(
            title="t", description="d", label_names=[], assignee_ids=[],
            due_date=None, requester="@yuan", status="Inbx",
        )
    assert "Inbox" in ei.value.candidates
    assert b.calls["create_issue"] == 0  # 先驗證再建卡，不留半成品


async def test_create_status_failure_reported_not_rolled_back() -> None:
    b = FakeBackend(LABELS)
    b.errors["set_issue_status"] = [GitLabBackendError(422, "x")]
    res = await _client(b).create_issue(
        title="t", description="d", label_names=[], assignee_ids=[],
        due_date=None, requester="@yuan", status="Doing",
    )
    assert res.issue.iid == 100  # 卡已建立
    assert res.missing_status == "Doing"
    assert res.issue.status is None


async def test_update_status_transition_and_diff() -> None:
    b = FakeBackend(LABELS)
    b.issues[42] = {
        "iid": 42, "web_url": "u", "title": "t", "description": "d",
        "labels": [], "assignees": [], "due_date": None, "state": "opened",
    }
    b.issue_statuses[42] = "Inbox"
    res = await _client(b).update_issue(42, status="doing")
    assert (res.status_before, res.status_after) == ("Inbox", "Doing")
    assert res.any_change() is True
    assert b.issue_statuses[42] == "Doing"
    assert b.calls["update_issue"] == 0  # 只改狀態時不打 REST update


async def test_update_same_status_is_noop() -> None:
    b = FakeBackend(LABELS)
    b.issues[42] = {
        "iid": 42, "web_url": "u", "title": "t", "description": "d",
        "labels": [], "assignees": [], "due_date": None, "state": "opened",
    }
    b.issue_statuses[42] = "Doing"
    res = await _client(b).update_issue(42, status="Doing")
    assert res.any_change() is False
    assert b.calls["set_issue_status"] == 0


async def test_update_status_failure_reported() -> None:
    b = FakeBackend(LABELS)
    b.issues[42] = {
        "iid": 42, "web_url": "u", "title": "舊", "description": "d",
        "labels": [], "assignees": [], "due_date": None, "state": "opened",
    }
    b.errors["set_issue_status"] = [GitLabBackendError(422, "x")]
    res = await _client(b).update_issue(42, title="新", status="Doing")
    assert res.title_changed is True  # 其他欄位照常更新
    assert res.status_after is None
    assert res.missing_status == "Doing"


async def test_get_issue_enriched_with_status() -> None:
    b = FakeBackend(LABELS)
    b.issues[42] = {
        "iid": 42, "web_url": "u", "title": "t", "description": None,
        "labels": [], "assignees": [], "due_date": None, "state": "opened",
    }
    b.issue_statuses[42] = "Waiting"
    issue = await _client(b).get_issue(42)
    assert issue.status == "Waiting"


# ------------------------------------------------------------------ #
# GL-13：scoped 互斥（編輯）+ GL-16：無 state_event
# ------------------------------------------------------------------ #
async def test_update_scoped_exclusion_and_diff() -> None:
    b = FakeBackend(LABELS)
    b.issues[42] = {
        "iid": 42, "web_url": "u", "title": "舊標題",
        "description": "d", "labels": ["Team::開發組", "0913 一籌"],
        "assignees": [], "due_date": None, "state": "opened",
    }
    res = await _client(b).update_issue(42, add_labels=["Team::行政組"], title="新標題")
    assert set(res.issue.labels) == {"Team::行政組", "0913 一籌"}
    assert res.labels_added == ["Team::行政組"]
    assert res.labels_removed == ["Team::開發組"]
    assert res.title_changed is True
    # GL-16：payload 永不含 state_event
    assert "state_event" not in b.last_update_payload


async def test_update_no_change_returns_no_diff() -> None:
    b = FakeBackend(LABELS)
    b.issues[42] = {
        "iid": 42, "web_url": "u", "title": "標題", "description": "d",
        "labels": ["Team::開發組"], "assignees": [], "due_date": None, "state": "opened",
    }
    res = await _client(b).update_issue(42, title="標題")  # 同名 → 無變更
    assert res.any_change() is False
    assert b.calls["update_issue"] == 0  # 無 payload 時不呼叫 API


# ------------------------------------------------------------------ #
# GL-21/GL-22：條件查詢與「開著」（2026-08-17 修訂：Review 為 native status）
# ------------------------------------------------------------------ #
async def test_open_only_excludes_review_and_closed() -> None:
    b = FakeBackend(LABELS)
    b.issues = {
        1: {"iid": 1, "web_url": "u1", "title": "a", "description": None,
            "labels": ["Team::行政組"], "assignees": [], "due_date": None, "state": "opened"},
        2: {"iid": 2, "web_url": "u2", "title": "b", "description": None,
            "labels": ["Team::行政組"], "assignees": [], "due_date": None, "state": "opened"},
        3: {"iid": 3, "web_url": "u3", "title": "c", "description": None,
            "labels": ["Team::行政組"], "assignees": [], "due_date": None, "state": "closed"},
    }
    b.issue_statuses = {1: "Doing", 2: "Review"}
    issues = await _client(b).search_issues(label_filters=["Team::行政組"], open_only=True)
    assert [i.iid for i in issues] == [1]  # Review 排除、closed 排除
    assert b.last_filters["state"] == "opened"


async def test_search_by_status_filter_matched_locally() -> None:
    b = FakeBackend(LABELS)
    b.issues = {
        1: {"iid": 1, "web_url": "u1", "title": "a", "description": None,
            "labels": [], "assignees": [], "due_date": None, "state": "opened"},
        2: {"iid": 2, "web_url": "u2", "title": "b", "description": None,
            "labels": [], "assignees": [], "due_date": None, "state": "opened"},
    }
    b.issue_statuses = {1: "Doing", 2: "Waiting"}
    issues = await _client(b).search_issues(status_filter="doing")  # 正規化解析
    assert [i.iid for i in issues] == [1]
    assert "labels" not in (b.last_filters or {})  # status 不是 label，不透傳 labels=

    with pytest.raises(StatusNotFoundError):
        await _client(b).search_issues(status_filter="不存在的狀態")


# ------------------------------------------------------------------ #
# GL-21：title_query 在本地做子字串比對（gitlab.com 的 search= 對中文只認整詞，
# 「預算」查不到「填預算」——2026-08-14 實測；故不得透傳）
# ------------------------------------------------------------------ #
async def test_title_query_substring_matched_locally_not_via_api() -> None:
    b = FakeBackend(LABELS)
    b.issues = {
        1: {"iid": 1, "web_url": "u1", "title": "[場務組] 填預算", "description": None,
            "labels": [], "assignees": [], "due_date": None, "state": "opened"},
        2: {"iid": 2, "web_url": "u2", "title": "場勘", "description": "回報前先確認預算上限",
            "labels": [], "assignees": [], "due_date": None, "state": "opened"},
        3: {"iid": 3, "web_url": "u3", "title": "[議程組] 籌備時程表", "description": None,
            "labels": [], "assignees": [], "due_date": None, "state": "closed"},
        4: {"iid": 4, "web_url": "u4", "title": "Fix OpenVidu relay", "description": None,
            "labels": [], "assignees": [], "due_date": None, "state": "opened"},
    }
    c = _client(b)

    hits = await c.search_issues(title_query="預算")
    assert [i.iid for i in hits] == [1, 2]  # 子字串命中標題與描述
    assert "search" not in b.last_filters   # 回歸防線：不得透傳 GitLab search=

    hits = await c.search_issues(title_query="場務 預算")
    assert [i.iid for i in hits] == [1]     # 多詞（空白分隔）＝ AND

    hits = await c.search_issues(title_query="時程表")
    assert [i.iid for i in hits] == [3]     # 預設 state=all，已關卡也可搜到

    hits = await c.search_issues(title_query="openvidu")
    assert [i.iid for i in hits] == [4]     # 英文不分大小寫


# ------------------------------------------------------------------ #
# NT-11：開著卡片查詢（2026-08-06 修訂：不限已過期）
# ------------------------------------------------------------------ #
def _bare_issue(iid: int, due: str | None, state: str = "opened", labels: list[str] | None = None) -> dict[str, Any]:
    return {"iid": iid, "web_url": f"u{iid}", "title": f"t{iid}", "description": None,
            "labels": labels or [], "assignees": [], "due_date": due, "state": state}


async def test_open_cards_filters_and_sorts() -> None:
    b = FakeBackend(LABELS)
    b.issues = {
        1: _bare_issue(1, "2026-08-01"),           # 未到期也列入
        2: _bare_issue(2, "2026-07-31"),
        3: _bare_issue(3, "2026-07-25"),           # 過期最久 → 排最前
        4: _bare_issue(4, "2026-07-28", "closed"),  # 已關閉 → 排除
        5: _bare_issue(5, None),                    # 無到期日也列入，殿後
        6: _bare_issue(6, "2026-07-25"),            # 同日到期 → 以 iid 穩定排序
        7: _bare_issue(7, "2026-07-20"),            # Review 不算開著（GL-22）→ 排除
    }
    b.issue_statuses = {1: "To Do", 2: "Doing", 3: "Inbox", 5: "Waiting", 7: "Review"}
    issues = await _client(b).open_cards()
    assert [i.iid for i in issues] == [3, 6, 2, 1, 5]
    assert b.last_filters == {"state": "opened"}


# ------------------------------------------------------------------ #
# GL-27～GL-29：Linked items（2026-08-14 追加需求：母卡追蹤）
# ------------------------------------------------------------------ #
async def test_link_issues_and_list_sorted_bidirectional() -> None:
    b = FakeBackend(LABELS)
    b.issues = {10: _bare_issue(10, None), 11: _bare_issue(11, "2026-09-19"), 12: _bare_issue(12, None, "closed")}
    c = _client(b)
    b.issue_statuses[11] = "Doing"
    await c.link_issues(10, 12)
    await c.link_issues(10, 11)
    links = await c.get_issue_links(10)
    # 依 iid 排序
    assert [(link.issue.iid, link.link_type) for link in links] == [(11, "relates_to"), (12, "relates_to")]
    assert links[0].issue.due_date == "2026-09-19"
    assert links[0].issue.status == "Doing"  # 連結卡片也補上 native status
    assert [link.issue.iid for link in await c.get_issue_links(11)] == [10]  # 連結為雙向


async def test_link_issues_duplicate_and_missing_target_mapped() -> None:
    b = FakeBackend(LABELS)
    b.issues = {10: _bare_issue(10, None), 11: _bare_issue(11, None)}
    c = _client(b)
    await c.link_issues(10, 11)
    with pytest.raises(GitLabAPIError) as dup:
        await c.link_issues(10, 11)
    assert dup.value.status == 409  # 重複連結：不可重試、狀態碼保留供工具轉譯
    with pytest.raises(GitLabAPIError) as missing:
        await c.link_issues(10, 99)
    assert missing.value.status == 404


async def test_unlink_resolves_link_id_and_absent_is_none() -> None:
    b = FakeBackend(LABELS)
    b.issues = {10: _bare_issue(10, None), 11: _bare_issue(11, None)}
    c = _client(b)
    await c.link_issues(10, 11)
    removed = await c.unlink_issues(10, 11)  # 刪除吃 issue_link_id，client 須自行由對象 iid 對應
    assert removed is not None and removed.issue.iid == 11
    assert await c.get_issue_links(10) == []
    assert await c.get_issue_links(11) == []  # 兩側連結一併消失
    assert await c.unlink_issues(10, 11) is None  # 本無連結 → None，非錯誤


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


async def test_status_cache_ttl_and_reload() -> None:
    clock = FakeClock()
    b = FakeBackend(LABELS)
    c = _client(b, label_cache_ttl=600, clock=clock)
    idx = await c.get_status_index()
    assert idx.resolve("to do") == "To Do"
    await c.get_status_index()
    assert b.calls["list_statuses"] == 1
    clock.advance(601)
    await c.get_status_index()
    assert b.calls["list_statuses"] == 2
    assert await c.reload_statuses() == len(STATUSES)  # force 重抓
    assert b.calls["list_statuses"] == 3


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
