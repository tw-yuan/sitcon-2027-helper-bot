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
