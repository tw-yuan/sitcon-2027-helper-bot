"""應用設定（pydantic-settings）。

啟動即驗證：缺任一必要變數立即失敗並指出變數名（AGENTS T1 DoD）。
secret 一律以 SecretStr 保存，避免進入 repr／日誌／LLM context（NFR-3）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["anthropic", "openai_compat"]
ThinkingLevel = Literal["off", "low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    """自 `.env`／環境變數載入的全域設定。欄位名（小寫）對映 .env 變數名（大寫）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram ---
    telegram_bot_token: SecretStr
    telegram_admin_id: int
    bot_trigger_name: str = "小石"

    # --- LLM ---
    llm_provider: LLMProvider = "anthropic"
    llm_model: str = "claude-sonnet-4-6"
    llm_api_key: SecretStr
    llm_base_url: str = ""
    llm_thinking: ThinkingLevel = "high"
    llm_max_tool_iterations: int = 8
    # openai_compat 專用；留空＝不帶。Codex 的 fast mode 即 service_tier="fast"，
    # 官方 API 另有 priority／flex。不支援時 provider 會回 400，改回留空即可。
    llm_service_tier: str = ""

    # --- GitLab ---
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: SecretStr
    gitlab_project: str = "sitcon-tw/2027"

    # --- Google ---
    google_sa_json_path: str = "/run/secrets/google-sa.json"
    drive_shared_drive_id: str = "0AIx9UW7aBiDgUk9PVA"  # 保留參考；查詢改用 corpora=allDrives + 範圍資料夾 ID
    # DR-1 搜尋範圍：以「名稱=資料夾ID」對應（service account 不需為共用硬碟成員，只要被分享這兩個資料夾）
    drive_scope_folders: str = (
        "SITCON 2027=1pigQFmO-v5xWjvhTWJrXQWcWGqTfPX_F,"
        "SITCON 2026=1oXcggPh2qxv0Do-g-ws6Nze1USoptA4T,"
        "SITCON 2025=1xL8dYXCH-N2z0c8gF0WmpbnIUmvY5ihQ"
    )
    roster_sheet_id: str = "1BiK-zMplJqSakQNfdQeDkwe2BMk21ngbm0xGEv8L5e4"
    roster_sheet_gid: int = 1822407485
    # 照片索引（Flickr Photo Finder 試算表；須分享給 service account）
    photo_index_sheet_id: str = "1JM2QzJo5kpeILZPyTSE6gUK3z-FyRcaGhPJlYE-FMbs"
    photo_index_tab: str = "photos"
    # 籌備時程表（里程碑預告來源；須分享給 service account，唯讀即可）
    milestone_sheet_id: str = "1esryzLBpnE51NIUU4-prwG0RrnG3W4eYQJ6FZSigFPQ"
    milestone_sheet_gid: int = 0  # 「工作表1」；以 gid 解析分頁名再讀值

    # --- Google Calendar（DWD，2026-08-02 追加需求）---
    # 冒用對象 email；留空＝停用 calendar 功能（工具不註冊）。需在 Workspace 後台對
    # service account 的 client ID 授權 https://www.googleapis.com/auth/calendar scope。
    google_dwd_subject: str = ""
    calendar_id: str = "primary"  # 操作冒用對象的哪本行事曆
    # 活動異動要不要寄通知信給邀請對象：all＝寄／externalOnly＝只寄網域外／none＝不寄
    # （none 時對方日曆上仍看得到活動，只是沒有 email）
    calendar_send_updates: Literal["all", "externalOnly", "none"] = "all"

    # --- 里程碑預告（NT-*）---
    milestone_notify_enabled: bool = True
    milestone_notify_hour: int = 23  # Asia/Taipei，預告「隔天」的事項＋開著卡片提醒
    milestone_notify_minute: int = 0
    # 錯過到點（如剛好在重啟）時的補送視窗；超過就跳過該日，不半夜打擾
    milestone_notify_catchup_minutes: int = 60
    # 不論訂閱哪幾組都會收到的組別（這些本來就是全員共同事項）
    milestone_always_teams: str = "全體,重要日期"
    # 隔天沒有任何事項時是否仍送出「今天沒事」；預設不送，避免每日噪音
    milestone_notify_when_empty: bool = False

    # --- HackMD ---
    hackmd_token: SecretStr
    hackmd_team_path: str
    hackmd_year_folder: str = "SITCON 2027"  # 年度根資料夾；建立筆記限縮於其子樹
    hackmd_search_year_folders: str = "SITCON 2027,SITCON 2026"  # 搜尋範圍：限縮於這些年度資料夾
    hackmd_meeting_folder: str = "會議文件"  # 年度根下的大籌/站立會議記錄資料夾
    hackmd_team_meeting_subfolder: str = "會議文件"  # 組別資料夾下的組會子資料夾
    hackmd_default_read_perm: str = "signed_in"
    hackmd_default_write_perm: str = "signed_in"

    # --- 快取 TTL（秒）---
    cache_ttl_labels: int = 600
    cache_ttl_hackmd: int = 600
    cache_ttl_drive_tree: int = 1800
    cache_ttl_roster: int = 3600
    cache_ttl_photos: int = 21600  # 照片索引極少變動 → 預設 6 小時
    cache_ttl_milestones: int = 3600  # 籌備時程表

    # --- 併發 ---
    # 同時處理的 update 上限（PTB concurrent_updates）。非觸發訊息幾乎不佔用時間，
    # 設寬鬆即可；真正吃資源的 agent 回合另由 max_concurrent_agent_turns 控管。
    max_concurrent_updates: int = 32
    # 同時進行的 agent 回合上限（跨群共用）：擋住突發流量打爆 LLM／GitLab 速率限制。
    max_concurrent_agent_turns: int = 8
    # 同一對話（chat + forum topic）的 agent 回合是否序列化。
    # True（預設）＝同群訊息依序回、順序不亂；False＝完全並行（SPEC EC-16 的原始要求）。
    # 純 reply-chain 脈絡下同群並行沒有正確性問題（一訊息一 session），差別只在回覆順序。
    serialize_per_chat: bool = True

    # --- 其他 ---
    team_charter_path: str = "role.md"  # 職掌文件（RO-8）；供 LLM 判斷組別；/reload 重載
    knowledge_path: str = "config/knowledge.md"  # 背景知識（會議室代碼等內部常識）；/reload 重載
    tz: str = "Asia/Taipei"
    context_ttl_seconds: int = 1800
    context_max_turns: int = 10
    log_level: str = "INFO"
    db_path: str = "/data/sitcon_bot.sqlite3"

    # ------------------------------------------------------------------ #
    # 衍生值
    # ------------------------------------------------------------------ #
    @property
    def drive_scope_map(self) -> dict[str, str]:
        """DR-1 範圍資料夾「名稱→資料夾ID」對應（逗號分隔的 name=id）。"""
        out: dict[str, str] = {}
        for entry in self.drive_scope_folders.split(","):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            name, _, fid = entry.partition("=")
            name, fid = name.strip(), fid.strip()
            if name and fid:
                out[name] = fid
        return out

    @property
    def drive_scope_folder_names(self) -> list[str]:
        """DR-1 範圍資料夾名單（供顯示與 scope 縮小）。"""
        return list(self.drive_scope_map)

    @property
    def milestone_always_team_list(self) -> list[str]:
        """不論訂閱哪幾組都會收到的組別（逗號分隔）。"""
        return [s.strip() for s in self.milestone_always_teams.split(",") if s.strip()]

    @property
    def hackmd_search_folder_list(self) -> list[str]:
        """HackMD 搜尋範圍的年度資料夾名單（逗號分隔）。"""
        return [s.strip() for s in self.hackmd_search_year_folders.split(",") if s.strip()]

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()

    # ------------------------------------------------------------------ #
    # 啟動摘要（遮蔽 secret）
    # ------------------------------------------------------------------ #
    def summary(self) -> str:
        """回傳啟動設定摘要；所有 secret 一律遮蔽（T1 DoD）。"""

        def mask(secret: SecretStr) -> str:
            raw = secret.get_secret_value()
            if not raw:
                return "<空>"
            if len(raw) <= 6:
                return "***"
            return f"{raw[:3]}…{raw[-2:]}（已設定）"

        lines = [
            "小石 設定摘要",
            "─────────────────────────────",
            f"  Telegram      : token={mask(self.telegram_bot_token)}  admin_id={self.telegram_admin_id}",
            f"  觸發詞         : {self.bot_trigger_name}",
            f"  LLM           : provider={self.llm_provider}  model={self.llm_model}  thinking={self.llm_thinking}"
            f"  tier={self.llm_service_tier or '<預設>'}",
            f"                  api_key={mask(self.llm_api_key)}  base_url={self.llm_base_url or '<預設>'}",
            f"  GitLab        : {self.gitlab_url}  project={self.gitlab_project}  token={mask(self.gitlab_token)}",
            f"  Drive         : drive_id={self.drive_shared_drive_id}  scope={self.drive_scope_folder_names}",
            "  Calendar(DWD) : "
            + (
                f"subject={self.google_dwd_subject}  calendar={self.calendar_id}  "
                f"邀請信={self.calendar_send_updates}"
                if self.google_dwd_subject
                else "停用（GOOGLE_DWD_SUBJECT 未設定）"
            ),
            f"  名冊 Sheet     : id={self.roster_sheet_id}  gid={self.roster_sheet_gid}",
            f"  HackMD        : team={self.hackmd_team_path}  token={mask(self.hackmd_token)}",
            f"                  year_folder={self.hackmd_year_folder}  "
            f"meeting_folder={self.hackmd_meeting_folder}  subfolder={self.hackmd_team_meeting_subfolder}",
            f"  時區／DB      : tz={self.tz}  db={self.db_path}",
            f"  快取 TTL(s)   : labels={self.cache_ttl_labels} hackmd={self.cache_ttl_hackmd} "
            f"drive={self.cache_ttl_drive_tree} roster={self.cache_ttl_roster} photos={self.cache_ttl_photos}",
            f"  照片索引       : sheet={self.photo_index_sheet_id[:12]}… tab={self.photo_index_tab}",
            f"  里程碑預告     : {'啟用' if self.milestone_notify_enabled else '停用'}  "
            f"每天 {self.milestone_notify_hour:02d}:{self.milestone_notify_minute:02d}（隔天里程碑＋開著卡片）  "
            f"sheet={self.milestone_sheet_id[:12]}… gid={self.milestone_sheet_gid}  "
            f"必收組別={self.milestone_always_team_list}",
            f"  反問續接       : ttl={self.context_ttl_seconds}s（純 reply-chain：回覆訊息才帶脈絡）",
            f"  併發          : updates={self.max_concurrent_updates} "
            f"agent回合={self.max_concurrent_agent_turns} "
            f"同一對話={'序列化' if self.serialize_per_chat else '並行（EC-16）'}",
            f"  日誌等級       : {self.log_level}",
            "─────────────────────────────",
        ]
        return "\n".join(lines)


def load_settings() -> Settings:
    """建立 Settings；缺必要變數時 pydantic 會拋 ValidationError（由進入點捕捉）。"""
    return Settings()  # type: ignore[call-arg]
