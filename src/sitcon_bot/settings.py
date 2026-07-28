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

    # --- 其他 ---
    team_charter_path: str = "role.md"  # 職掌文件（RO-8）；供 LLM 判斷組別；/reload 重載
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
            f"  LLM           : provider={self.llm_provider}  model={self.llm_model}  thinking={self.llm_thinking}",
            f"                  api_key={mask(self.llm_api_key)}  base_url={self.llm_base_url or '<預設>'}",
            f"  GitLab        : {self.gitlab_url}  project={self.gitlab_project}  token={mask(self.gitlab_token)}",
            f"  Drive         : drive_id={self.drive_shared_drive_id}  scope={self.drive_scope_folder_names}",
            f"  名冊 Sheet     : id={self.roster_sheet_id}  gid={self.roster_sheet_gid}",
            f"  HackMD        : team={self.hackmd_team_path}  token={mask(self.hackmd_token)}",
            f"                  year_folder={self.hackmd_year_folder}  "
            f"meeting_folder={self.hackmd_meeting_folder}  subfolder={self.hackmd_team_meeting_subfolder}",
            f"  時區／DB      : tz={self.tz}  db={self.db_path}",
            f"  快取 TTL(s)   : labels={self.cache_ttl_labels} hackmd={self.cache_ttl_hackmd} "
            f"drive={self.cache_ttl_drive_tree} roster={self.cache_ttl_roster} photos={self.cache_ttl_photos}",
            f"  照片索引       : sheet={self.photo_index_sheet_id[:12]}… tab={self.photo_index_tab}",
            f"  反問續接       : ttl={self.context_ttl_seconds}s（純 reply-chain：回覆訊息才帶脈絡）",
            f"  日誌等級       : {self.log_level}",
            "─────────────────────────────",
        ]
        return "\n".join(lines)


def load_settings() -> Settings:
    """建立 Settings；缺必要變數時 pydantic 會拋 ValidationError（由進入點捕捉）。"""
    return Settings()  # type: ignore[call-arg]
