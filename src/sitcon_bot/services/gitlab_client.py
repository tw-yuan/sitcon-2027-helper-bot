"""GitLab client（GL-1～GL-23）。

作用範圍限定 `sitcon-tw/2027` 單一專案。硬性防線在此程式層強制：
  GL-10  建卡／編輯卡片只用既有 label，寫入前逐一比對白名單。
         【2026-08-02 追加需求例外】label 本身的管理（create_label／update_label／delete_label）
         開放為獨立操作；每次異動後強制刷新白名單，卡片操作仍受白名單約束。
  GL-13  scoped label 互斥由 client 端保證（組最終 label 集合時先移除同 scope 舊值）。
  GL-16  不主動變更 issue state：payload 永不含 state_event。
         【2026-08-17 修訂】卡片狀態改用 GitLab native status（work item status）後，
         Done/Canceled 類別的 status 由 GitLab 自動連動 close——那是 GitLab 端的行為，
         bot 仍不自己發 close/reopen。

【2026-08-17 修訂】卡片狀態自 Status:: scoped label 遷移至 native status：
  - status 只有 GraphQL API（REST 不支援），故 backend 混用兩者——issue／label CRUD 走 REST，
    status 的清單（namespace.statuses）、讀取（widget STATUS）、設定（workItemUpdate.statusWidget）
    走 GraphQL。
  - status 白名單比照 label：namespace 既有 status 才能用（GL-10 精神），未知即拒絕並附近似候選。
  - 「開著的卡」（GL-22）改為 state=opened 且 status ≠ Review。
  - namespace 尚未設定 status（清單為空）時整體降級：不帶預設狀態、不做 Review 排除，
    讓 GitLab 端設定完成前 bot 照常運作。

外部 I/O 抽象為 GitLabBackend（可注入假物件測試）；client 疊上白名單、scoped 互斥、
attribution（GL-8）、多 assignee 落差偵測（GL-6）、重試（EC-9）與憑證錯誤（EC-10）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ..domain.matching import nearest_labels, normalize_label

if TYPE_CHECKING:
    from ..settings import Settings

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 例外
# --------------------------------------------------------------------------- #
class GitLabError(RuntimeError):
    """GitLab 操作錯誤基底。"""


class LabelNotFoundError(GitLabError):
    """label 不在專案既有白名單（GL-10/GL-12）。附近似候選。"""

    def __init__(self, requested: str, candidates: list[str]) -> None:
        self.requested = requested
        self.candidates = candidates
        super().__init__(f"label 不存在：{requested}")


class StatusNotFoundError(GitLabError):
    """status 不在 namespace 既有清單（GL-10 精神，2026-08-17 native status 修訂）。附近似候選。"""

    def __init__(self, requested: str, candidates: list[str]) -> None:
        self.requested = requested
        self.candidates = candidates
        super().__init__(f"狀態不存在：{requested}")


class CredentialError(GitLabError):
    """憑證失效（401/403，EC-10）。"""


class GitLabAPIError(GitLabError):
    """其他 API 錯誤（4xx/5xx，EC-9）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class GitLabBackendError(RuntimeError):
    """backend 層拋出的原始 HTTP 錯誤，帶狀態碼，供 client 分類。

    retryable：連線／逾時等網路層錯誤沒有 HTTP 狀態碼，但屬暫時性，標記為可重試（EC-9）。
    """

    def __init__(self, status: int | None, message: str, retryable: bool = False) -> None:
        self.status = status
        self.retryable = retryable
        super().__init__(message)


# --------------------------------------------------------------------------- #
# 資料模型
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Assignee:
    id: int
    username: str | None = None
    name: str | None = None


@dataclass(slots=True)
class Issue:
    iid: int
    web_url: str
    title: str
    description: str | None
    labels: list[str]
    assignees: list[Assignee]
    due_date: str | None
    state: str
    # native status 名稱（REST 不回傳，由 client 以 GraphQL 批次補上；未設定／未啟用時為 None）
    status: str | None = None

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> Issue:
        assignees = [
            Assignee(id=a["id"], username=a.get("username"), name=a.get("name"))
            for a in (d.get("assignees") or [])
        ]
        return cls(
            iid=d["iid"],
            web_url=d.get("web_url", ""),
            title=d.get("title", ""),
            description=d.get("description"),
            labels=list(d.get("labels") or []),
            assignees=assignees,
            due_date=d.get("due_date"),
            state=d.get("state", ""),
        )


@dataclass(slots=True)
class LinkedIssue:
    """Linked items 中的一筆連結（links API 回傳＝對象卡欄位＋連結資訊）。"""

    link_id: int  # issue_link_id，解除連結時使用
    link_type: str  # relates_to / blocks / is_blocked_by（以本卡視角）
    issue: Issue

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> LinkedIssue:
        return cls(
            link_id=d["issue_link_id"],
            link_type=d.get("link_type", "relates_to"),
            issue=Issue.from_raw(d),
        )


@dataclass(slots=True)
class Note:
    id: int
    body: str
    system: bool
    author_username: str | None

    @classmethod
    def from_raw(cls, d: dict[str, Any]) -> Note:
        author = d.get("author") or {}
        return cls(
            id=d["id"],
            body=d.get("body", ""),
            system=bool(d.get("system", False)),
            author_username=author.get("username"),
        )


@dataclass(slots=True)
class CreateResult:
    issue: Issue
    # GL-6：要求但未實際套用的 assignee id（多重指派落差／權限不足／非專案成員）
    missing_assignees: list[int] = field(default_factory=list)
    # 要求但未實際套用的 label（多為權限不足被 GitLab 靜默忽略）
    missing_labels: list[str] = field(default_factory=list)
    # 卡已建立但 status 設定失敗時的目標狀態（卡片操作不因 status 失敗整筆回滾）
    missing_status: str | None = None


@dataclass(slots=True)
class UpdateResult:
    issue: Issue
    labels_added: list[str] = field(default_factory=list)
    labels_removed: list[str] = field(default_factory=list)
    assignees_added: list[int] = field(default_factory=list)
    assignees_removed: list[int] = field(default_factory=list)
    title_changed: bool = False
    description_changed: bool = False
    due_date_changed: bool = False
    # native status 變更（status_after 非 None＝有實際改）
    status_before: str | None = None
    status_after: str | None = None
    # 要求但未實際套用（權限不足／assignee 非專案成員時 GitLab 靜默忽略）
    missing_labels: list[str] = field(default_factory=list)
    missing_assignees: list[int] = field(default_factory=list)
    # 其他欄位已更新但 status 設定失敗時的目標狀態
    missing_status: str | None = None

    def any_change(self) -> bool:
        return bool(
            self.labels_added
            or self.labels_removed
            or self.assignees_added
            or self.assignees_removed
            or self.title_changed
            or self.description_changed
            or self.due_date_changed
            or self.status_after is not None
        )


# --------------------------------------------------------------------------- #
# label 邏輯（純函式，易測）
# --------------------------------------------------------------------------- #
def label_scope(name: str) -> str | None:
    """scoped label 的 scope key（最後一組 :: 之前）；非 scoped 回傳 None。"""
    if "::" in name:
        return name.rsplit("::", 1)[0]
    return None


def merge_labels(existing: list[str], add: list[str], remove: list[str]) -> list[str]:
    """組出最終 label 集合（GL-13）。

    先移除 remove；再套用 add：對 scoped 的新值，先自集合移除同 scope 的其他值，再加入。
    保留既有順序，新值附加於末。
    """
    remove_set = set(remove)
    final = [label for label in existing if label not in remove_set]
    for a in add:
        scope = label_scope(a)
        if scope is not None:
            final = [label for label in final if label_scope(label) != scope]
        if a not in final:
            final.append(a)
    return final


# 「已完成待總召 review」的 native status 名稱；GL-22 的「開著」排除條件
REVIEW_STATUS = "Review"


class LabelIndex:
    """既有名稱白名單索引（GL-10/GL-11/GL-12）；label 與 native status 共用同一套解析邏輯。"""

    def __init__(self, names: list[str]) -> None:
        self.names = list(names)
        self._norm_to_canon: dict[str, str] = {}
        for n in self.names:
            self._norm_to_canon.setdefault(normalize_label(n), n)

    def resolve(self, query: str) -> str | None:
        """正規化後精確對應到既有 label 的正式名稱；無對應回 None。"""
        return self._norm_to_canon.get(normalize_label(query))

    def nearest(self, query: str, n: int = 5) -> list[str]:
        return nearest_labels(query, self.names, n)


# --------------------------------------------------------------------------- #
# attribution（GL-8）
# --------------------------------------------------------------------------- #
def attribution_line(requester: str) -> str:
    """GL-8 來源列。requester 由呼叫端（工具層）解析成 GitLab 身分字串（如 @gitlab_username）。"""
    return f"> _via 小石 · requested by {requester}_"


def with_attribution(text: str | None, requester: str) -> str:
    line = attribution_line(requester)
    body = (text or "").rstrip()
    return f"{body}\n\n{line}" if body else line


# --------------------------------------------------------------------------- #
# backend
# --------------------------------------------------------------------------- #
class GitLabBackend(Protocol):
    """同步 GitLab 存取抽象；回傳/接收 plain dict。實作可為 python-gitlab＋GraphQL 或測試假物件。

    status 三方法走 GraphQL（native status 無 REST API，2026-08-17 修訂）：
      list_statuses      namespace 既有 status 名稱（未設定／未啟用時回空清單）
      get_issue_statuses 批次讀多張卡的 status 名稱（iid → 名稱；無 status 的卡對到 None）
      set_issue_status   設定單張卡 status，回傳實際套用的名稱
    """

    def list_labels(self) -> list[str]: ...
    def list_statuses(self) -> list[str]: ...
    def get_issue_statuses(self, iids: list[int]) -> dict[int, str | None]: ...
    def set_issue_status(self, iid: int, status: str) -> str: ...
    def create_label(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def update_label(self, name: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def delete_label(self, name: str) -> None: ...
    def create_issue(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def get_issue(self, iid: int) -> dict[str, Any]: ...
    def update_issue(self, iid: int, payload: dict[str, Any]) -> dict[str, Any]: ...
    def list_issue_notes(self, iid: int) -> list[dict[str, Any]]: ...
    def create_issue_note(self, iid: int, body: str) -> dict[str, Any]: ...
    def list_issues(self, filters: dict[str, Any]) -> list[dict[str, Any]]: ...
    def list_issue_links(self, iid: int) -> list[dict[str, Any]]: ...
    def create_issue_link(self, iid: int, target_iid: int, link_type: str) -> None: ...
    def delete_issue_link(self, iid: int, issue_link_id: int) -> None: ...


_Clock = Callable[[], float]
_Sleep = Callable[[float], Awaitable[None]]


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class GitLabClient:
    def __init__(
        self,
        backend: GitLabBackend,
        label_cache_ttl: int = 600,
        clock: _Clock = time.monotonic,
        sleep: _Sleep = asyncio.sleep,
        max_retries: int = 2,
    ) -> None:
        self._b = backend
        self._label_ttl = label_cache_ttl
        self._clock = clock
        self._sleep = sleep
        self._max_retries = max_retries
        self._label_index: LabelIndex | None = None
        self._labels_at: float | None = None
        self._labels_lock = asyncio.Lock()
        # native status 白名單（與 label 同一套 TTL＋single-flight，各自獨立快取）
        self._status_index: LabelIndex | None = None
        self._statuses_at: float | None = None
        self._statuses_lock = asyncio.Lock()

    # -------------------------- label 白名單 -------------------------- #
    def _labels_fresh(self) -> bool:
        return (
            self._label_index is not None
            and self._labels_at is not None
            and (self._clock() - self._labels_at) < self._label_ttl
        )

    async def get_label_index(self, *, force: bool = False) -> LabelIndex:
        """single-flight：快取冷掉時同時進來的多個回合只打一次 list_labels。"""
        if not force and self._labels_fresh():
            assert self._label_index is not None
            return self._label_index
        async with self._labels_lock:
            # 等鎖期間可能已有人載好；force（/reload）例外，一定重抓。
            if not force and self._labels_fresh():
                assert self._label_index is not None
                return self._label_index
            names = await self._call(self._b.list_labels)
            self._label_index = LabelIndex(names)
            self._labels_at = self._clock()
            return self._label_index

    async def reload_labels(self) -> int:
        idx = await self.get_label_index(force=True)
        return len(idx.names)

    def _validate(self, index: LabelIndex, names: list[str]) -> list[str]:
        """把使用者提及的 label 對應到既有正式名稱；任一不存在即拋 LabelNotFoundError（GL-10/12）。"""
        out: list[str] = []
        for name in names:
            canon = index.resolve(name)
            if canon is None:
                raise LabelNotFoundError(name, index.nearest(name))
            out.append(canon)
        return out

    # -------------------------- status 白名單（2026-08-17 native status）-------------------------- #
    def _statuses_fresh(self) -> bool:
        return (
            self._status_index is not None
            and self._statuses_at is not None
            and (self._clock() - self._statuses_at) < self._label_ttl
        )

    async def get_status_index(self, *, force: bool = False) -> LabelIndex:
        """namespace 既有 status 名稱索引；空清單＝尚未設定 native status（降級運作）。"""
        if not force and self._statuses_fresh():
            assert self._status_index is not None
            return self._status_index
        async with self._statuses_lock:
            if not force and self._statuses_fresh():
                assert self._status_index is not None
                return self._status_index
            names = await self._call(self._b.list_statuses)
            self._status_index = LabelIndex(names)
            self._statuses_at = self._clock()
            return self._status_index

    async def reload_statuses(self) -> int:
        idx = await self.get_status_index(force=True)
        return len(idx.names)

    def _validate_status(self, index: LabelIndex, name: str) -> str:
        """把使用者提及的狀態對應到既有正式名稱；不存在即拋 StatusNotFoundError（附近似候選）。"""
        canon = index.resolve(name)
        if canon is None:
            raise StatusNotFoundError(name, index.nearest(name))
        return canon

    async def _enrich_statuses(self, issues: list[Issue]) -> None:
        """以 GraphQL 批次補上 native status（REST 的 issue 回應沒有這個欄位）。"""
        if not issues:
            return
        mapping = await self._call(self._b.get_issue_statuses, [i.iid for i in issues])
        for issue in issues:
            issue.status = mapping.get(issue.iid)

    # -------------------- label 管理（2026-08-02 追加需求）-------------------- #
    # 卡片操作的白名單約束（GL-10）不變；這裡是 label 本身的 CRUD，每次異動後強制刷新白名單，
    # 讓後續建卡／編輯立即看得到新集合。
    async def create_label(self, *, name: str, color: str, description: str | None = None) -> str:
        """建立新 label；已存在（正規化後同名）則拒絕。回傳建立後的正式名稱。"""
        index = await self.get_label_index()
        existing = index.resolve(name)
        if existing is not None:
            raise GitLabError(f"label「{existing}」已存在，未重複建立。")
        payload: dict[str, Any] = {"name": name, "color": color}
        if description:
            payload["description"] = description
        raw = await self._call(self._b.create_label, payload)
        await self.get_label_index(force=True)
        return str(raw.get("name", name))

    async def update_label(
        self,
        name: str,
        *,
        new_name: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> str:
        """編輯既有 label（改名／換色／改描述）。回傳編輯後的正式名稱。

        description 傳空字串＝清除描述；None＝不變。改名目標與其他既有 label 撞名時拒絕。
        """
        index = await self.get_label_index()
        canon = index.resolve(name)
        if canon is None:
            raise LabelNotFoundError(name, index.nearest(name))
        payload: dict[str, Any] = {}
        if new_name is not None and new_name != canon:
            clash = index.resolve(new_name)
            if clash is not None and clash != canon:
                raise GitLabError(f"改名失敗：label「{clash}」已存在。")
            payload["new_name"] = new_name
        if color is not None:
            payload["color"] = color
        if description is not None:
            payload["description"] = description
        if not payload:
            return canon
        raw = await self._call(self._b.update_label, canon, payload)
        await self.get_label_index(force=True)
        return str(raw.get("name", payload.get("new_name", canon)))

    async def delete_label(self, name: str) -> str:
        """刪除既有 label（會同時自所有卡片移除）。回傳被刪除的正式名稱。"""
        index = await self.get_label_index()
        canon = index.resolve(name)
        if canon is None:
            raise LabelNotFoundError(name, index.nearest(name))
        await self._call(self._b.delete_label, canon)
        await self.get_label_index(force=True)
        return canon

    # -------------------------- 建卡 -------------------------- #
    async def create_issue(
        self,
        *,
        title: str,
        description: str | None,
        label_names: list[str],
        assignee_ids: list[int],
        due_date: str | None,
        requester: str,
        status: str | None = None,
    ) -> CreateResult:
        index = await self.get_label_index()
        canonical = self._validate(index, label_names)
        # status 先驗證再建卡：未知狀態整次不執行，不留半成品卡
        status_canon: str | None = None
        if status is not None:
            status_canon = self._validate_status(await self.get_status_index(), status)
        final_labels = merge_labels([], canonical, [])
        desc = with_attribution(description, requester)  # GL-8

        payload: dict[str, Any] = {
            "title": title,
            "description": desc,
            "labels": ",".join(final_labels),
            "assignee_ids": list(assignee_ids),
        }
        if due_date:
            payload["due_date"] = due_date

        raw = await self._call(self._b.create_issue, payload)
        issue = Issue.from_raw(raw)
        applied = {a.id for a in issue.assignees}
        missing = [aid for aid in assignee_ids if aid not in applied]  # GL-6
        applied_labels = set(issue.labels)
        missing_labels = [name for name in final_labels if name not in applied_labels]
        missing_status: str | None = None
        if status_canon is not None:
            # status 走 GraphQL、與 REST 建卡非同一交易；設定失敗時卡已存在，據實回報不回滾
            try:
                issue.status = await self._call(self._b.set_issue_status, issue.iid, status_canon)
            except GitLabError:
                log.warning("建卡 #%s 後設定狀態「%s」失敗", issue.iid, status_canon, exc_info=True)
                missing_status = status_canon
        if missing or missing_labels or missing_status:
            log.warning(
                "建卡 #%s 套用落差：labels=%s assignees=%s status=%s（多為權限不足或 assignee 非專案成員）",
                issue.iid, missing_labels, missing, missing_status,
            )
        return CreateResult(
            issue=issue,
            missing_assignees=missing,
            missing_labels=missing_labels,
            missing_status=missing_status,
        )

    # -------------------------- 編輯 -------------------------- #
    async def update_issue(
        self,
        iid: int,
        *,
        title: str | None = None,
        description: str | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
        set_assignee_ids: list[int] | None = None,
        due_date: str | None = None,
        clear_due_date: bool = False,
        requester: str | None = None,
        status: str | None = None,
    ) -> UpdateResult:
        """編輯卡片（GL-14）。labels 以最終集合覆寫（GL-13）；不自己發 state_event（GL-16）。

        status 為 native status（GraphQL 另行設定；與現值相同時不打 API）。
        編輯描述時比照建卡/留言附上來源標註（GL-8），保留可追溯性、避免整段覆寫洗掉標註。
        """
        before = await self.get_issue(iid)
        payload: dict[str, Any] = {}

        status_canon: str | None = None
        if status is not None:
            status_canon = self._validate_status(await self.get_status_index(), status)
        want_status = status_canon is not None and status_canon != before.status

        if title is not None and title != before.title:
            payload["title"] = title
        if description is not None:
            payload["description"] = with_attribution(description, requester) if requester else description

        add_canon: list[str] = []
        if add_labels or remove_labels:
            index = await self.get_label_index()
            add_canon = self._validate(index, add_labels or [])
            remove_canon = self._validate(index, remove_labels or [])
            final = merge_labels(before.labels, add_canon, remove_canon)
            payload["labels"] = ",".join(final)

        requested_assignees: list[int] = []
        if set_assignee_ids is not None:
            requested_assignees = list(set_assignee_ids)
            # 空清單 → 取消所有指派（API：0 或空值），以 [0] 表達
            payload["assignee_ids"] = requested_assignees if requested_assignees else [0]

        if clear_due_date:
            payload["due_date"] = ""
        elif due_date is not None:
            payload["due_date"] = due_date

        # 硬性：payload 永不含 state_event（GL-16）
        assert "state_event" not in payload

        if not payload and not want_status:
            return UpdateResult(issue=before)

        after = before
        if payload:
            raw = await self._call(self._b.update_issue, iid, payload)
            after = Issue.from_raw(raw)
            after.status = before.status  # REST 回應沒有 status 欄位，沿用讀到的現值
        result = self._diff(before, after)
        if want_status:
            assert status_canon is not None
            prev_status = before.status  # 先取現值：payload 為空時 after 與 before 是同一物件
            try:
                applied = await self._call(self._b.set_issue_status, iid, status_canon)
                after.status = applied
                result.issue = after
                result.status_before = prev_status
                result.status_after = applied
            except GitLabError:
                log.warning("編輯 #%s 設定狀態「%s」失敗", iid, status_canon, exc_info=True)
                result.missing_status = status_canon
        # 落差偵測：要求新增但未套用的 label／指派（多為權限不足或 assignee 非專案成員）
        after_labels = set(after.labels)
        result.missing_labels = [name for name in add_canon if name not in after_labels]
        after_a = {a.id for a in after.assignees}
        result.missing_assignees = [aid for aid in requested_assignees if aid and aid not in after_a]
        if result.missing_labels or result.missing_assignees or result.missing_status:
            log.warning(
                "編輯 #%s 套用落差：labels=%s assignees=%s status=%s",
                iid, result.missing_labels, result.missing_assignees, result.missing_status,
            )
        return result

    @staticmethod
    def _diff(before: Issue, after: Issue) -> UpdateResult:
        before_labels, after_labels = set(before.labels), set(after.labels)
        before_a = {a.id for a in before.assignees}
        after_a = {a.id for a in after.assignees}
        return UpdateResult(
            issue=after,
            labels_added=sorted(after_labels - before_labels),
            labels_removed=sorted(before_labels - after_labels),
            assignees_added=sorted(after_a - before_a),
            assignees_removed=sorted(before_a - after_a),
            title_changed=before.title != after.title,
            description_changed=(before.description or "") != (after.description or ""),
            due_date_changed=(before.due_date or "") != (after.due_date or ""),
        )

    # -------------------------- 留言 -------------------------- #
    async def comment_issue(self, iid: int, body: str, *, requester: str) -> Note:
        text = with_attribution(body, requester)  # GL-18 → GL-8 來源列
        raw = await self._call(self._b.create_issue_note, iid, text)
        return Note.from_raw(raw)

    async def get_issue_notes(self, iid: int, *, human_only: bool = True) -> list[Note]:
        raw = await self._call(self._b.list_issue_notes, iid)
        notes = [Note.from_raw(n) for n in raw]
        if human_only:
            notes = [n for n in notes if not n.system]  # GL-19 過濾系統訊息
        return notes

    # -------------------------- 查詢 -------------------------- #
    async def get_issue(self, iid: int) -> Issue:
        raw = await self._call(self._b.get_issue, iid)
        issue = Issue.from_raw(raw)
        await self._enrich_statuses([issue])
        return issue

    async def search_issues(
        self,
        *,
        label_filters: list[str] | None = None,
        assignee_id: int | None = None,
        title_query: str | None = None,
        open_only: bool = False,
        status_filter: str | None = None,
    ) -> list[Issue]:
        """條件查詢（GL-21）。open_only 採 GL-22 定義：state=opened 且 status ≠ Review。

        status_filter 為 native status：REST 無法以 status 過濾，抓回後在本地比對
        （GraphQL 批次補值）。title_query 不透傳 GitLab `search=`：gitlab.com 的 issues
        搜尋走 PostgreSQL 全文檢索，中文無斷詞、只認整詞相等（「預算」查不到「填預算」），
        故抓回後在本地做不分大小寫的子字串比對（標題＋描述；多詞以空白分隔，全部命中才算）。
        """
        status_canon: str | None = None
        if status_filter is not None:
            status_canon = self._validate_status(await self.get_status_index(), status_filter)
        filters: dict[str, Any] = {}
        if label_filters:
            index = await self.get_label_index()
            canonical = self._validate(index, label_filters)
            filters["labels"] = ",".join(canonical)
        filters["state"] = "opened" if open_only else "all"
        if assignee_id is not None:
            filters["assignee_id"] = assignee_id

        raw = await self._call(self._b.list_issues, filters)
        issues = [Issue.from_raw(r) for r in raw]
        if title_query:
            terms = [t.casefold() for t in title_query.split()]
            issues = [
                i for i in issues
                if all(t in f"{i.title}\n{i.description or ''}".casefold() for t in terms)
            ]
        await self._enrich_statuses(issues)
        if status_canon is not None:
            issues = [i for i in issues if i.status == status_canon]
        if open_only:
            issues = [i for i in issues if i.status != REVIEW_STATUS]  # GL-22
        return issues

    async def open_cards(self) -> list[Issue]:
        """所有「開著」的卡片（GL-22：opened 且 status ≠ Review），依到期日排序（未填者殿後）。
        NT-11 卡片提醒的到期視窗篩選在 notify/cards.py（本方法維持通用的「全部開著」語意）。
        """
        raw = await self._call(self._b.list_issues, {"state": "opened"})
        issues = [Issue.from_raw(r) for r in raw]
        await self._enrich_statuses(issues)
        issues = [i for i in issues if i.status != REVIEW_STATUS]  # GL-22
        issues.sort(key=lambda i: (i.due_date is None, i.due_date or "", i.iid))
        return issues

    # -------------------------- Linked items（2026-08-14 追加需求：母卡追蹤） -------------------------- #
    async def get_issue_links(self, iid: int) -> list[LinkedIssue]:
        raw = await self._call(self._b.list_issue_links, iid)
        links = [LinkedIssue.from_raw(r) for r in raw]
        links.sort(key=lambda link: link.issue.iid)
        await self._enrich_statuses([link.issue for link in links])
        return links

    async def link_issues(self, iid: int, target_iid: int, link_type: str = "relates_to") -> None:
        await self._call(self._b.create_issue_link, iid, target_iid, link_type)

    async def unlink_issues(self, iid: int, target_iid: int) -> LinkedIssue | None:
        """解除與對象卡的連結。links API 的刪除吃 issue_link_id 而非對象 iid，先查表對應；
        兩卡間本無連結時回 None（不視為錯誤）。"""
        links = await self.get_issue_links(iid)
        match = next((link for link in links if link.issue.iid == target_iid), None)
        if match is None:
            return None
        await self._call(self._b.delete_issue_link, iid, match.link_id)
        return match

    # -------------------------- 重試與錯誤分類 -------------------------- #
    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """在 thread 執行同步 backend 呼叫；429/5xx 指數退避重試（EC-9）、401/403 憑證錯誤（EC-10）。"""
        attempt = 0
        while True:
            try:
                return await asyncio.to_thread(fn, *args)
            except GitLabBackendError as exc:
                status = exc.status
                if status in (401, 403):
                    raise CredentialError("GitLab 憑證失效，請通知管理員") from exc
                retryable = (
                    exc.retryable  # 網路層錯誤（連線／逾時）
                    or status == 429
                    or (status is not None and 500 <= status < 600)
                )
                if retryable and attempt < self._max_retries:
                    delay = 0.5 * (2**attempt)
                    log.warning("GitLab %s，第 %d 次退避重試（%.1fs）", status, attempt + 1, delay)
                    await self._sleep(delay)
                    attempt += 1
                    continue
                raise GitLabAPIError(f"GitLab 回應錯誤（HTTP {status}）：{exc}", status) from exc


# --------------------------------------------------------------------------- #
# python-gitlab backend（真實 I/O）
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _map_errors() -> Iterator[None]:
    """把 python-gitlab 的 GitlabError 與底層 requests 網路錯誤轉為 GitLabBackendError。"""
    from gitlab.exceptions import GitlabError
    from requests.exceptions import RequestException

    try:
        yield
    except GitlabError as exc:  # 所有 GitlabHttpError 等的基底（帶 HTTP 狀態）
        status = getattr(exc, "response_code", None)
        raise GitLabBackendError(status, str(exc)) from exc
    except RequestException as exc:  # 連線／逾時等網路層錯誤，無 HTTP 狀態 → 可重試（EC-9）
        raise GitLabBackendError(None, f"GitLab 連線錯誤：{exc}", retryable=True) from exc


class PyGitlabBackend:
    """以 python-gitlab（REST）＋原生 GraphQL 實作 GitLabBackend；限定單一專案（GL：sitcon-tw/2027）。

    python-gitlab 為同步；GitLabClient 會在 to_thread 中呼叫本 backend。
    native status 沒有 REST API（GitLab 18.x），status 三方法直接 POST /api/graphql
    （requests 已是 python-gitlab 相依，不另加套件）。
    """

    # 每次 GraphQL 查詢的 work item 批次上限（GraphQL connection 單頁上限 100）
    _GQL_CHUNK = 100

    _Q_STATUSES = """
    query($ns: ID!) {
      namespace(fullPath: $ns) { statuses(first: 100) { nodes { name } } }
    }"""
    _Q_ITEM_STATUSES = """
    query($p: ID!, $iids: [String!]) {
      project(fullPath: $p) {
        workItems(iids: $iids, first: 100) {
          nodes {
            iid
            widgets(onlyTypes: [STATUS]) { ... on WorkItemWidgetStatus { status { name } } }
          }
        }
      }
    }"""
    _Q_ITEM_ID = """
    query($p: ID!, $iid: String!) {
      project(fullPath: $p) { workItems(iids: [$iid], first: 1) { nodes { id } } }
    }"""
    _M_SET_STATUS = """
    mutation($id: WorkItemID!, $name: String!) {
      workItemUpdate(input: {id: $id, statusWidget: {name: $name}}) {
        workItem {
          widgets(onlyTypes: [STATUS]) { ... on WorkItemWidgetStatus { status { name } } }
        }
        errors
      }
    }"""

    def __init__(self, url: str, token: str, project_path: str, timeout: int = 30) -> None:
        self._url = url
        self._token = token
        self._project_path = project_path
        # status 定義在頂層群組；sitcon-tw/2027 的 namespace 即頂層 sitcon-tw
        self._namespace_path = project_path.rsplit("/", 1)[0]
        self._timeout = timeout
        self._project: Any = None

    def _get_project(self) -> Any:
        if self._project is None:
            import gitlab

            gl = gitlab.Gitlab(self._url, private_token=self._token, timeout=self._timeout)
            with _map_errors():
                self._project = gl.projects.get(self._project_path)
        return self._project

    # -------------------------- GraphQL（native status） -------------------------- #
    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        import requests

        try:
            resp = requests.post(
                self._url.rstrip("/") + "/api/graphql",
                json={"query": query, "variables": variables},
                headers={"PRIVATE-TOKEN": self._token},
                timeout=self._timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise GitLabBackendError(None, f"GitLab GraphQL 連線錯誤：{exc}", retryable=True) from exc
        if resp.status_code != 200:
            raise GitLabBackendError(
                resp.status_code, f"GitLab GraphQL HTTP {resp.status_code}：{resp.text[:200]}"
            )
        body = resp.json()
        if body.get("errors"):
            msgs = "；".join(str(e.get("message", e)) for e in body["errors"])
            raise GitLabBackendError(None, f"GitLab GraphQL 錯誤：{msgs}")
        return body.get("data") or {}

    @staticmethod
    def _widget_status_name(container: dict[str, Any] | None) -> str | None:
        for widget in ((container or {}).get("widgets") or []):
            status = widget.get("status")
            if status and status.get("name"):
                return str(status["name"])
        return None

    def list_statuses(self) -> list[str]:
        data = self._graphql(self._Q_STATUSES, {"ns": self._namespace_path})
        ns = data.get("namespace")
        if ns is None:
            # namespace 查不到＝無權限或 status 功能未啟用（如非 Premium）；降級為未設定
            return []
        nodes = (ns.get("statuses") or {}).get("nodes") or []
        return [str(n["name"]) for n in nodes if n.get("name")]

    def get_issue_statuses(self, iids: list[int]) -> dict[int, str | None]:
        out: dict[int, str | None] = {}
        for i in range(0, len(iids), self._GQL_CHUNK):
            chunk = [str(x) for x in iids[i : i + self._GQL_CHUNK]]
            data = self._graphql(self._Q_ITEM_STATUSES, {"p": self._project_path, "iids": chunk})
            nodes = (((data.get("project") or {}).get("workItems") or {}).get("nodes") or [])
            for node in nodes:
                out[int(node["iid"])] = self._widget_status_name(node)
        return out

    def set_issue_status(self, iid: int, status: str) -> str:
        data = self._graphql(self._Q_ITEM_ID, {"p": self._project_path, "iid": str(iid)})
        nodes = (((data.get("project") or {}).get("workItems") or {}).get("nodes") or [])
        if not nodes:
            raise GitLabBackendError(404, f"找不到 work item #{iid}")
        data = self._graphql(self._M_SET_STATUS, {"id": nodes[0]["id"], "name": status})
        payload = data.get("workItemUpdate") or {}
        errs = payload.get("errors") or []
        if errs:
            raise GitLabBackendError(None, "設定狀態失敗：" + "；".join(str(e) for e in errs))
        return self._widget_status_name(payload.get("workItem")) or status

    def list_labels(self) -> list[str]:
        with _map_errors():
            labels = self._get_project().labels.list(get_all=True)  # 全量分頁（GL-11）
        return [label.name for label in labels]

    def create_label(self, payload: dict[str, Any]) -> dict[str, Any]:
        with _map_errors():
            obj = self._get_project().labels.create(payload)
        return dict(obj.attributes)

    def update_label(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        with _map_errors():
            label = self._get_project().labels.get(name)
            for key, value in payload.items():
                setattr(label, key, value)
            label.save()
        return dict(label.attributes)

    def delete_label(self, name: str) -> None:
        with _map_errors():
            self._get_project().labels.delete(name)

    def create_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        with _map_errors():
            obj = self._get_project().issues.create(payload)
        return dict(obj.attributes)

    def get_issue(self, iid: int) -> dict[str, Any]:
        with _map_errors():
            obj = self._get_project().issues.get(iid)
        return dict(obj.attributes)

    def update_issue(self, iid: int, payload: dict[str, Any]) -> dict[str, Any]:
        with _map_errors():
            return dict(self._get_project().issues.update(iid, payload))

    def list_issue_notes(self, iid: int) -> list[dict[str, Any]]:
        with _map_errors():
            issue = self._get_project().issues.get(iid, lazy=True)
            notes = issue.notes.list(get_all=True)
        return [dict(n.attributes) for n in notes]

    def create_issue_note(self, iid: int, body: str) -> dict[str, Any]:
        with _map_errors():
            issue = self._get_project().issues.get(iid, lazy=True)
            note = issue.notes.create({"body": body})
        return dict(note.attributes)

    def list_issues(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        with _map_errors():
            objs = self._get_project().issues.list(get_all=True, **filters)
        return [dict(o.attributes) for o in objs]

    def list_issue_links(self, iid: int) -> list[dict[str, Any]]:
        with _map_errors():
            issue = self._get_project().issues.get(iid, lazy=True)
            links = issue.links.list(get_all=True)
        return [dict(link.attributes) for link in links]

    def create_issue_link(self, iid: int, target_iid: int, link_type: str) -> None:
        with _map_errors():
            issue = self._get_project().issues.get(iid, lazy=True)
            issue.links.create(
                {
                    "target_project_id": self._project_path,
                    "target_issue_iid": target_iid,
                    "link_type": link_type,
                }
            )

    def delete_issue_link(self, iid: int, issue_link_id: int) -> None:
        with _map_errors():
            issue = self._get_project().issues.get(iid, lazy=True)
            issue.links.delete(issue_link_id)


def build_gitlab_client(settings: Settings) -> GitLabClient:
    backend = PyGitlabBackend(
        url=settings.gitlab_url,
        token=settings.gitlab_token.get_secret_value(),
        project_path=settings.gitlab_project,
    )
    return GitLabClient(backend, label_cache_ttl=settings.cache_ttl_labels)
