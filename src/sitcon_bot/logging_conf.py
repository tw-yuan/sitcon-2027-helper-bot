"""結構化應用日誌設定（NFR-8）。等級由設定控制；secret 絕不進日誌（NFR-3）。"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """設定 root logger。可重複呼叫（僅第一次生效）。"""
    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger().setLevel(level.upper())
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 壓低第三方 library 的雜訊
    for noisy in ("httpx", "httpcore", "telegram", "urllib3", "googleapiclient"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
