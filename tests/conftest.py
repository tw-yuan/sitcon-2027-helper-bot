"""共用 pytest 設定與 fixtures。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from sitcon_bot.storage.db import Database


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-golden",
        action="store_true",
        default=False,
        help="實打 LLM API 執行 golden test（預設略過）",
    )
    parser.addoption(
        "--golden-model",
        action="store",
        default=None,
        help="覆蓋 golden test 使用的模型（Sonnet vs Haiku 對照）",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-golden"):
        return
    skip_golden = pytest.mark.skip(reason="需 --run-golden 才執行（會實打 LLM API）")
    for item in items:
        if item.get_closest_marker("golden") is not None:
            item.add_marker(skip_golden)


# 一組通過驗證的最小必要設定，供各測試建立 Settings 使用。
REQUIRED_ENV: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "123456:test-bot-token-value",
    "TELEGRAM_ADMIN_ID": "42",
    "LLM_API_KEY": "sk-test-key-abcdef",
    "GITLAB_TOKEN": "glpat-test-token",
    "HACKMD_TOKEN": "hackmd-test-token",
    "HACKMD_TEAM_PATH": "sitcon",
}


@pytest.fixture
def required_env() -> dict[str, str]:
    """回傳必要環境變數的副本。"""
    return dict(REQUIRED_ENV)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """每個測試一份乾淨的檔案型 SQLite，teardown 保證關閉連線。"""
    database = await Database.connect(str(tmp_path / "test.sqlite3"))
    try:
        yield database
    finally:
        await database.close()
