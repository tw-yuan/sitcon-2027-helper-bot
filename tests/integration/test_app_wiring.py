"""整合：app.run 的完整組裝路徑（DB、各服務工廠、全部工具、agent、gateway）不出錯。

以 stub 取代 Gateway.run（不連 Telegram），驗證從設定到 gateway 建構的整條接線。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sitcon_bot import app as app_module
from sitcon_bot.settings import Settings


async def test_app_run_wiring_constructs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_gateway_run(self: object, stop: object) -> None:
        return None

    monkeypatch.setattr("sitcon_bot.telegram.gateway.Gateway.run", _fake_gateway_run)

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token="123:abc",
        telegram_admin_id=1,
        llm_api_key="test-key",
        gitlab_token="glpat-x",
        hackmd_token="hmd-x",
        hackmd_team_path="sitcon",
        db_path=str(tmp_path / "wiring.sqlite3"),
    )

    # 不應拋出任何例外；完整建置後 stub gateway 立即返回，finally 收尾。
    await app_module.run(settings)

    assert (tmp_path / "wiring.sqlite3").exists()  # DB 有建立


async def test_app_run_wiring_with_calendar_dwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GOOGLE_DWD_SUBJECT 設定時 calendar 工具接線也要能完整建構（gateway 建構前不觸網）。"""
    captured: dict[str, object] = {}

    async def _fake_gateway_run(self: object, stop: object) -> None:
        return None

    original_registry = app_module.ToolRegistry

    def _capture_registry(tools: list) -> object:  # type: ignore[type-arg]
        reg = original_registry(tools)
        captured["names"] = reg.names()
        return reg

    monkeypatch.setattr("sitcon_bot.telegram.gateway.Gateway.run", _fake_gateway_run)
    monkeypatch.setattr(app_module, "ToolRegistry", _capture_registry)

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token="123:abc",
        telegram_admin_id=1,
        llm_api_key="test-key",
        gitlab_token="glpat-x",
        hackmd_token="hmd-x",
        hackmd_team_path="sitcon",
        google_dwd_subject="me@yuan-tw.net",
        db_path=str(tmp_path / "wiring-cal.sqlite3"),
    )
    await app_module.run(settings)

    names = captured["names"]
    assert "calendar_create_event" in names
    assert "calendar_list_events" in names
    assert "calendar_update_event" in names
    assert "calendar_delete_event" in names
    assert "gitlab_create_label" in names
    assert "gitlab_update_label" in names
    assert "gitlab_delete_label" in names
