"""T1：設定載入與 fail-fast 行為。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sitcon_bot.settings import Settings

REQUIRED_FIELDS = [
    "telegram_bot_token",
    "telegram_admin_id",
    "llm_api_key",
    "gitlab_token",
    "hackmd_token",
    "hackmd_team_path",
]


def _clear_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    """清掉所有可能影響測試的環境變數，確保 hermetic。"""
    for key in list(env) + [f.upper() for f in REQUIRED_FIELDS]:
        monkeypatch.delenv(key, raising=False)


def test_missing_required_vars_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺必要變數時應拋 ValidationError，且點名所有缺漏欄位。"""
    _clear_env(monkeypatch, {})
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]

    missing = {str(e["loc"][0]) for e in excinfo.value.errors() if e["type"] == "missing"}
    assert set(REQUIRED_FIELDS) <= missing


def test_valid_settings_load(required_env: dict[str, str]) -> None:
    """提供完整必要值時應成功載入，型別正確。"""
    s = Settings(_env_file=None, **{k.lower(): v for k, v in required_env.items()})  # type: ignore[call-arg]
    assert s.telegram_admin_id == 42
    assert isinstance(s.telegram_admin_id, int)
    assert s.llm_provider == "anthropic"
    assert s.gitlab_project == "sitcon-tw/2027"


def test_summary_masks_all_secrets(required_env: dict[str, str]) -> None:
    """啟動摘要不得洩漏任何 secret 明文（NFR-3）。"""
    s = Settings(_env_file=None, **{k.lower(): v for k, v in required_env.items()})  # type: ignore[call-arg]
    summary = s.summary()
    for secret in (
        required_env["TELEGRAM_BOT_TOKEN"],
        required_env["LLM_API_KEY"],
        required_env["GITLAB_TOKEN"],
        required_env["HACKMD_TOKEN"],
    ):
        assert secret not in summary, f"secret 洩漏於摘要：{secret}"
    # 非 secret 資訊應可見
    assert "sitcon-tw/2027" in summary
    assert "42" in summary


def test_drive_scope_folder_names_default(required_env: dict[str, str]) -> None:
    """DR-1 範圍資料夾解析（逗號分隔）。"""
    s = Settings(_env_file=None, **{k.lower(): v for k, v in required_env.items()})  # type: ignore[call-arg]
    assert s.drive_scope_folder_names == ["SITCON 2027", "SITCON 2026", "SITCON 2025"]


def test_drive_scope_map_parses_name_id_pairs(required_env: dict[str, str]) -> None:
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        drive_scope_folders=" A=id1 , B=id2, =noname, C=id3 , D ",  # 缺 id / 缺 name 的項忽略
        **{k.lower(): v for k, v in required_env.items()},
    )
    assert s.drive_scope_map == {"A": "id1", "B": "id2", "C": "id3"}
    assert s.drive_scope_folder_names == ["A", "B", "C"]


def test_log_level_uppercased(required_env: dict[str, str]) -> None:
    s = Settings(_env_file=None, log_level="debug", **{k.lower(): v for k, v in required_env.items()})  # type: ignore[call-arg]
    assert s.log_level == "DEBUG"
