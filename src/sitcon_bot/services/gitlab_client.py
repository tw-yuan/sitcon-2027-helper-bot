"""GitLab client（GL-1～GL-23）。

作用範圍限定 `sitcon-tw/2027` 單一專案。硬性防線在此程式層強制：
  GL-10  只用既有 label，永不呼叫建立 label API；寫入前逐一比對白名單。
  GL-13  scoped label 互斥由 client 端保證（組最終 label 集合時先移除同 scope 舊值）。
  GL-16  不實作任何 state 變更（close/reopen）與刪除；payload 永不含 state_event。

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
    # 要求但未實際套用（權限不足／assignee 非專案成員時 GitLab 靜默忽略）
    missing_labels: list[str] = field(default_factory=list)
    missing_assignees: list[int] = field(default_factory=list)

    def any_change(self) -> bool:
        return bool(
            self.labels_added
            or self.labels_removed
            or self.assignees_added
            or self.assignees_removed
            or self.title_changed
            or self.description_changed
            or self.due_date_changed
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


class LabelIndex:
    """專案既有 label 白名單索引（GL-10/GL-11/GL-12）。"""

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
def attribution_line(username: str | None, user_id: int) -> str:
    handle = f"@{username}" if username else "@?"
    return f"> _via 小石 · requested by {handle} ({user_id})_"


def with_attribution(text: str | None, username: str | None, user_id: int) -> str:
    line = attribution_line(username, user_id)
    body = (text or "").rstrip()
    return f"{body}\n\n{line}" if body else line


# --------------------------------------------------------------------------- #
# backend
# --------------------------------------------------------------------------- #
class GitLabBackend(Protocol):
    """同步 GitLab 存取抽象；回傳/接收 plain dict。實作可為 python-gitlab 或測試假物件。"""

    def list_labels(self) -> list[str]: ...
    def create_issue(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def get_issue(self, iid: int) -> dict[str, Any]: ...
    def update_issue(self, iid: int, payload: dict[str, Any]) -> dict[str, Any]: ...
    def list_issue_notes(self, iid: int) -> list[dict[str, Any]]: ...
    def create_issue_note(self, iid: int, body: str) -> dict[str, Any]: ...
    def list_issues(self, filters: dict[str, Any]) -> list[dict[str, Any]]: ...


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

    # -------------------------- label 白名單 -------------------------- #
    async def get_label_index(self, *, force: bool = False) -> LabelIndex:
        fresh = (
            self._label_index is not None
            and self._labels_at is not None
            and (self._clock() - self._labels_at) < self._label_ttl
        )
        if force or not fresh:
            names = await self._call(self._b.list_labels)
            self._label_index = LabelIndex(names)
            self._labels_at = self._clock()
        assert self._label_index is not None
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

    # -------------------------- 建卡 -------------------------- #
    async def create_issue(
        self,
        *,
        title: str,
        description: str | None,
        label_names: list[str],
        assignee_ids: list[int],
        due_date: str | None,
        requester_username: str | None,
        requester_user_id: int,
    ) -> CreateResult:
        index = await self.get_label_index()
        canonical = self._validate(index, label_names)
        final_labels = merge_labels([], canonical, [])
        desc = with_attribution(description, requester_username, requester_user_id)  # GL-8

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
        if missing or missing_labels:
            log.warning(
                "建卡 #%s 套用落差：labels=%s assignees=%s（多為權限不足或 assignee 非專案成員）",
                issue.iid, missing_labels, missing,
            )
        return CreateResult(issue=issue, missing_assignees=missing, missing_labels=missing_labels)

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
        requester_username: str | None = None,
        requester_user_id: int | None = None,
    ) -> UpdateResult:
        """編輯卡片（GL-14）。labels 以最終集合覆寫（GL-13）；不觸碰 state（GL-16）。

        編輯描述時比照建卡/留言附上來源標註（GL-8），保留可追溯性、避免整段覆寫洗掉標註。
        """
        before = await self.get_issue(iid)
        payload: dict[str, Any] = {}

        if title is not None and title != before.title:
            payload["title"] = title
        if description is not None:
            if requester_user_id is not None:
                payload["description"] = with_attribution(description, requester_username, requester_user_id)
            else:
                payload["description"] = description

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

        if not payload:
            return UpdateResult(issue=before)

        raw = await self._call(self._b.update_issue, iid, payload)
        after = Issue.from_raw(raw)
        result = self._diff(before, after)
        # 落差偵測：要求新增但未套用的 label／指派（多為權限不足或 assignee 非專案成員）
        after_labels = set(after.labels)
        result.missing_labels = [name for name in add_canon if name not in after_labels]
        after_a = {a.id for a in after.assignees}
        result.missing_assignees = [aid for aid in requested_assignees if aid and aid not in after_a]
        if result.missing_labels or result.missing_assignees:
            log.warning(
                "編輯 #%s 套用落差：labels=%s assignees=%s",
                iid, result.missing_labels, result.missing_assignees,
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
    async def comment_issue(
        self, iid: int, body: str, *, requester_username: str | None, requester_user_id: int
    ) -> Note:
        text = with_attribution(body, requester_username, requester_user_id)  # GL-18 → GL-8 來源列
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
        return Issue.from_raw(raw)

    async def search_issues(
        self,
        *,
        label_filters: list[str] | None = None,
        assignee_id: int | None = None,
        title_query: str | None = None,
        open_only: bool = False,
    ) -> list[Issue]:
        """條件查詢（GL-21）。open_only 採 GL-22 定義：state=opened 且無 Status::Review。"""
        filters: dict[str, Any] = {}
        if label_filters:
            index = await self.get_label_index()
            canonical = self._validate(index, label_filters)
            filters["labels"] = ",".join(canonical)
        filters["state"] = "opened" if open_only else "all"
        if assignee_id is not None:
            filters["assignee_id"] = assignee_id
        if title_query:
            filters["search"] = title_query

        raw = await self._call(self._b.list_issues, filters)
        issues = [Issue.from_raw(r) for r in raw]
        if open_only:
            issues = [i for i in issues if "Status::Review" not in i.labels]  # GL-22
        return issues

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
    """以 python-gitlab 實作 GitLabBackend；限定單一專案（GL：sitcon-tw/2027）。

    python-gitlab 為同步；GitLabClient 會在 to_thread 中呼叫本 backend。
    """

    def __init__(self, url: str, token: str, project_path: str, timeout: int = 30) -> None:
        self._url = url
        self._token = token
        self._project_path = project_path
        self._timeout = timeout
        self._project: Any = None

    def _get_project(self) -> Any:
        if self._project is None:
            import gitlab

            gl = gitlab.Gitlab(self._url, private_token=self._token, timeout=self._timeout)
            with _map_errors():
                self._project = gl.projects.get(self._project_path)
        return self._project

    def list_labels(self) -> list[str]:
        with _map_errors():
            labels = self._get_project().labels.list(get_all=True)  # 全量分頁（GL-11）
        return [label.name for label in labels]

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


def build_gitlab_client(settings: Settings) -> GitLabClient:
    backend = PyGitlabBackend(
        url=settings.gitlab_url,
        token=settings.gitlab_token.get_secret_value(),
        project_path=settings.gitlab_project,
    )
    return GitLabClient(backend, label_cache_ttl=settings.cache_ttl_labels)
