"""進入點：`python -m sitcon_bot`。

行為：
  1. 載入設定；缺必要變數立即失敗並指出變數名（fail-fast，T1 DoD）。
  2. 設定日誌、輸出設定摘要（遮蔽 secret）。
  3. 啟動應用（`--check` 僅驗證與輸出摘要後結束，供 CI／部署前檢查）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pydantic import ValidationError

from .app import run
from .logging_conf import configure_logging
from .settings import load_settings


def _format_validation_error(exc: ValidationError) -> str:
    """把 pydantic 錯誤轉成點名缺漏／格式錯誤變數的人話。"""
    env_name = {
        "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
        "telegram_admin_id": "TELEGRAM_ADMIN_ID",
        "llm_api_key": "LLM_API_KEY",
        "gitlab_token": "GITLAB_TOKEN",
        "hackmd_token": "HACKMD_TOKEN",
        "hackmd_team_path": "HACKMD_TEAM_PATH",
    }
    lines = ["設定載入失敗，請檢查 .env（或環境變數）："]
    for err in exc.errors():
        field = str(err["loc"][0]) if err["loc"] else "?"
        name = env_name.get(field, field.upper())
        if err["type"] in ("missing", "value_error.missing"):
            lines.append(f"  ✗ 缺少必要變數：{name}")
        else:
            lines.append(f"  ✗ {name}：{err['msg']}")
    lines.append("\n可參考 .env.example 補齊後重試。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sitcon-bot", description="小石 — SITCON 2027 助理 bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="僅驗證設定並輸出摘要後結束（不連線）",
    )
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ValidationError as exc:
        print(_format_validation_error(exc), file=sys.stderr)
        raise SystemExit(1) from None

    configure_logging(settings.log_level)
    print(settings.summary(), file=sys.stderr)

    if args.check:
        print("設定檢查通過。", file=sys.stderr)
        return

    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
