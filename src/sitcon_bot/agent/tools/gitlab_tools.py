"""GitLab 工具（GL-1～GL-23）。接 GitLabClient + 名冊 + 組別解析，供 agent 呼叫。

硬性防線在 GitLabClient；此層負責：組別自動指派（GL-2/3）、預設 Status::Inbox（GL-5）、
把外部內容以 <external_data> 包起（NFR-6）、把 label 錯誤轉為 GL-12 回覆。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ...domain.team_classifier import resolve_team_assignment
from ...services.gitlab_client import (
    CredentialError,
    GitLabClient,
    GitLabError,
    LabelNotFoundError,
)
from ...services.sheets_roster import Roster, RosterService, RosterUnavailableError
from .base import Tool, ToolContext
from .external_data import wrap_external

log = logging.getLogger(__name__)


def _external(text: str) -> str:
    return wrap_external(text)


async def _safe_roster(service: RosterService | None) -> Roster | None:
    if service is None:
        return None
    try:
        return await service.get()
    except RosterUnavailableError:
        return None
    except Exception:  # 名冊層其他錯誤不應中斷 GitLab 操作
        log.warning("名冊取得失敗，改以無名冊處理", exc_info=True)
        return None


def _requester_handle(roster: Roster | None, ctx: ToolContext, gitlab_url: str) -> str:
    """把發話者解析成 GitLab 身分字串供 attribution（GL-8）。

    用「個人頁連結」而非 @mention——顯示 GitLab 使用者、可點進 profile，但**不會觸發 GitLab 的
    @提及通知**（避免每張卡都寄 email 給當事人）。名冊查無 GitLab 身分時退回標明「（Telegram）」的
    純文字帳號（不加 @，以免誤觸他人 GitLab 提及）。
    """
    if roster is not None:
        member = roster.by_telegram_id(ctx.user_id)
        if member is not None and member.gitlab_username:
            u = member.gitlab_username
            return f"[{u}]({gitlab_url.rstrip('/')}/{u})"
    if ctx.username:
        return f"{ctx.username}（Telegram）"
    return f"Telegram user {ctx.user_id}"


def _label_error(exc: LabelNotFoundError) -> str:
    cands = "、".join(exc.candidates) if exc.candidates else "（無相近候選）"
    return f"找不到 label「{exc.requested}」，此次未執行。最接近的既有 label：{cands}。請改用正確的 label。"


# 建卡/編輯時 label 或 assignee 被 GitLab 靜默忽略的可能原因（供 LLM 據實轉告，勿宣稱成功）
_APPLY_HINT = (
    "（GitLab 未套用上述項目，常見原因：小石的 GitLab 帳號權限不足（需 Reporter 以上），"
    "或指派對象不是本專案成員。請據實告知使用者哪些沒套用成功，不要宣稱成功。）"
)


def _fmt_issue_line(iid: int, title: str, state: str, labels: list[str], assignees: list[str], url: str) -> str:
    """一列卡片摘要。iid／status／url 為受信任的識別資訊留在圍欄外（供 LLM 據以操作）；
    標題與 assignee username 為外部可控自由文字，包進 <external_data>（NFR-6）。"""
    status = next((label for label in labels if label.startswith("Status::")), state)
    who = "、".join(assignees) if assignees else "（無）"
    return f"#{iid}｜{status}｜{url}\n" + wrap_external(f"標題：{title}｜指派：{who}")


class _GitLabToolBase(Tool):
    def __init__(
        self, gitlab: GitLabClient, roster: RosterService | None = None, gitlab_url: str = "https://gitlab.com"
    ) -> None:
        self._gl = gitlab
        self._roster = roster
        self._gitlab_url = gitlab_url


# --------------------------------------------------------------------------- #
# 建卡
# --------------------------------------------------------------------------- #
class CreateIssueArgs(BaseModel):
    title: str = Field(
        description="卡片標題；若這張卡涉及 2 個（含）以上組別，前面加「[主責組、協作組…] 」前綴"
    )
    description: str | None = Field(None, description="描述（整理為簡潔 markdown）")
    team: str | None = Field(
        None, description="任務所屬組名（你依職掌判斷；無法判斷時留空，會自動落總召組）"
    )
    labels: list[str] = Field(default_factory=list, description="其他既有 label（Status::、籌會等；不要放 Team::）")
    assignee_ids: list[int] = Field(
        default_factory=list, description="明確指定的 assignee gitlab_id（先用 resolve_person 取得）"
    )
    due_date: str | None = Field(None, description="到期日 YYYY-MM-DD（Asia/Taipei）")


class GitlabCreateIssueTool(_GitLabToolBase):
    name = "gitlab_create_issue"
    description = "在 sitcon-tw/2027 建立一張卡片。未指定組別時自動判斷並指派組長，未指定狀態預設 Status::Inbox。"
    args_model = CreateIssueArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, CreateIssueArgs)
        roster = await _safe_roster(self._roster)
        try:
            index = await self._gl.get_label_index()
        except CredentialError as exc:
            return str(exc)

        labels = list(args.labels)
        if not any(label.startswith("Status::") for label in labels):
            labels.append("Status::Inbox")  # GL-5

        assignment = resolve_team_assignment(args.team, roster, index)
        if assignment.team_label and not any(label.startswith("Team::") for label in labels):
            labels.append(assignment.team_label)
        assignees = args.assignee_ids or assignment.assignee_ids

        try:
            res = await self._gl.create_issue(
                title=args.title,
                description=args.description,
                label_names=labels,
                assignee_ids=assignees,
                due_date=args.due_date,
                requester=_requester_handle(roster, ctx, self._gitlab_url),
            )
        except LabelNotFoundError as exc:
            return _label_error(exc)
        except GitLabError as exc:
            return f"建卡失敗：{exc}"

        issue = res.issue
        who = "、".join(a.username or str(a.id) for a in issue.assignees) or "（無）"
        parts = [
            f"✅ 已建立 #{issue.iid}",
            f"labels：{'、'.join(issue.labels)}",  # label 受白名單約束，非自由文字
            issue.web_url,
            wrap_external(f"標題：{issue.title}｜指派：{who}"),  # 標題／username 為外部可控
            assignment.note,
        ]
        if res.missing_labels:
            parts.append(f"⚠️ 下列 label 未成功套用：{'、'.join(res.missing_labels)}")
        if res.missing_assignees:
            parts.append(f"⚠️ 下列 assignee 未成功套用：{res.missing_assignees}")
        if res.missing_labels or res.missing_assignees:
            parts.append(_APPLY_HINT)
        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 編輯
# --------------------------------------------------------------------------- #
class UpdateIssueArgs(BaseModel):
    iid: int = Field(description="卡片 IID")
    title: str | None = None
    description: str | None = None
    add_labels: list[str] = Field(default_factory=list, description="要新增的既有 label")
    remove_labels: list[str] = Field(default_factory=list, description="要移除的 label")
    set_assignee_ids: list[int] | None = Field(None, description="整組覆蓋 assignee（空清單=清除）")
    due_date: str | None = Field(None, description="設定到期日 YYYY-MM-DD")
    clear_due_date: bool = Field(False, description="清除到期日")


class GitlabUpdateIssueTool(_GitLabToolBase):
    name = "gitlab_update_issue"
    description = "編輯既有卡片：標題、描述、labels（增減換）、assignees、due date。不會變更卡片開關狀態。"
    args_model = UpdateIssueArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, UpdateIssueArgs)
        roster = await _safe_roster(self._roster)
        try:
            res = await self._gl.update_issue(
                args.iid,
                title=args.title,
                description=args.description,
                add_labels=args.add_labels,
                remove_labels=args.remove_labels,
                set_assignee_ids=args.set_assignee_ids,
                due_date=args.due_date,
                clear_due_date=args.clear_due_date,
                requester=_requester_handle(roster, ctx, self._gitlab_url),
            )
        except LabelNotFoundError as exc:
            return _label_error(exc)
        except GitLabError as exc:
            return f"編輯失敗：{exc}"

        missing = []
        if res.missing_labels:
            missing.append(f"label：{'、'.join(res.missing_labels)}")
        if res.missing_assignees:
            missing.append(f"assignee：{res.missing_assignees}")

        if not res.any_change():
            if missing:
                return f"⚠️ #{args.iid} 的變更未套用（{'；'.join(missing)}）。\n{_APPLY_HINT}"
            return f"#{args.iid} 沒有實際變更。"
        changes = []
        title_line = ""
        if res.title_changed:
            changes.append("標題已更新")
            title_line = "\n新標題：" + wrap_external(res.issue.title)  # 標題為外部可控自由文字
        if res.labels_added:
            changes.append(f"加 label：{'、'.join(res.labels_added)}")
        if res.labels_removed:
            changes.append(f"移除 label：{'、'.join(res.labels_removed)}")
        if res.assignees_added:
            changes.append(f"加 assignee：{res.assignees_added}")
        if res.assignees_removed:
            changes.append(f"移除 assignee：{res.assignees_removed}")
        if res.due_date_changed:
            changes.append(f"到期日→{res.issue.due_date or '（清除）'}")
        if res.description_changed:
            changes.append("更新描述")
        tail = f"\n{_APPLY_HINT}" if missing else ""
        if missing:
            changes.append(f"⚠️ 未套用 {'；'.join(missing)}")
        return f"✅ 已更新 #{args.iid}：" + "；".join(changes) + title_line + f"\n{res.issue.web_url}" + tail


# --------------------------------------------------------------------------- #
# 留言
# --------------------------------------------------------------------------- #
class CommentIssueArgs(BaseModel):
    iid: int
    body: str = Field(description="留言內容")


class GitlabCommentIssueTool(_GitLabToolBase):
    name = "gitlab_comment_issue"
    description = "在指定卡片新增一則留言。"
    args_model = CommentIssueArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, CommentIssueArgs)
        roster = await _safe_roster(self._roster)
        try:
            await self._gl.comment_issue(
                args.iid, args.body, requester=_requester_handle(roster, ctx, self._gitlab_url)
            )
        except GitLabError as exc:
            return f"留言失敗：{exc}"
        return f"✅ 已在 #{args.iid} 留言。"


# --------------------------------------------------------------------------- #
# 讀取（含留言）
# --------------------------------------------------------------------------- #
class GetIssueArgs(BaseModel):
    iid: int
    include_notes: bool = Field(False, description="是否一併取回人工留言（供摘要討論）")


class GitlabGetIssueTool(_GitLabToolBase):
    name = "gitlab_get_issue"
    description = "讀取指定卡片的內容（標題、狀態、assignees、URL）；可一併取回人工留言以供摘要。"
    args_model = GetIssueArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, GetIssueArgs)
        try:
            issue = await self._gl.get_issue(args.iid)
        except GitLabError as exc:
            return f"讀取失敗：{exc}"
        who = [a.username or str(a.id) for a in issue.assignees]
        out = [_fmt_issue_line(issue.iid, issue.title, issue.state, issue.labels, who, issue.web_url)]
        if issue.description:
            out.append("描述：" + _external(issue.description))
        if args.include_notes:
            try:
                notes = await self._gl.get_issue_notes(args.iid, human_only=True)
            except GitLabError as exc:
                return "\n".join(out) + f"\n（留言讀取失敗：{exc}）"
            if notes:
                body = "\n---\n".join(f"{n.author_username or '?'}：{n.body}" for n in notes)
                out.append(f"人工留言（{len(notes)}）：" + _external(body))
            else:
                out.append("（沒有人工留言）")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# 查詢
# --------------------------------------------------------------------------- #
class SearchIssuesArgs(BaseModel):
    label_filters: list[str] = Field(default_factory=list, description="要過濾的既有 label（Team::、Status::、籌會）")
    assignee_id: int | None = Field(None, description="指定 assignee 的 gitlab_id")
    title_query: str | None = Field(None, description="標題/描述關鍵字")
    open_only: bool = Field(False, description="只列開著的卡（opened 且無 Status::Review）")


class GitlabSearchIssuesTool(_GitLabToolBase):
    name = "gitlab_search_issues"
    description = "依條件查詢卡片（組別、狀態、籌會 label、assignee、標題關鍵字、開著/全部），條件可組合。"
    args_model = SearchIssuesArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, SearchIssuesArgs)
        try:
            issues = await self._gl.search_issues(
                label_filters=args.label_filters,
                assignee_id=args.assignee_id,
                title_query=args.title_query,
                open_only=args.open_only,
            )
        except LabelNotFoundError as exc:
            return _label_error(exc)
        except GitLabError as exc:
            return f"查詢失敗：{exc}"

        if not issues:
            return "查無符合條件的卡片。"
        total = len(issues)
        shown = issues[:10]
        lines = [
            _fmt_issue_line(
                i.iid, i.title, i.state, i.labels, [a.username or str(a.id) for a in i.assignees], i.web_url
            )
            for i in shown
        ]
        header = f"共 {total} 張" + ("（顯示前 10 張，可要求下一批）" if total > 10 else "") + "："
        return header + "\n" + "\n".join(lines)


def build_gitlab_tools(
    gitlab: GitLabClient, roster: RosterService | None, gitlab_url: str = "https://gitlab.com"
) -> list[Tool]:
    return [
        GitlabCreateIssueTool(gitlab, roster, gitlab_url),
        GitlabUpdateIssueTool(gitlab, roster, gitlab_url),
        GitlabCommentIssueTool(gitlab, roster, gitlab_url),
        GitlabGetIssueTool(gitlab, roster, gitlab_url),
        GitlabSearchIssuesTool(gitlab, roster, gitlab_url),
    ]
