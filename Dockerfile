# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# 不寫 .pyc、輸出不緩衝，方便容器日誌
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 從官方 image 取 uv（快速、可重現的依賴安裝）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 先只複製依賴宣告，善用 layer 快取
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src

# 以 lockfile 安裝（存在時），並安裝本專案
RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

# 複製其餘檔案（config、role.md 於 compose 以 volume 掛載覆蓋，可熱改）
COPY config ./config
COPY role.md ./role.md

# data 目錄由 volume 掛載
RUN mkdir -p /data

# 非 root 執行
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /data
USER appuser

ENTRYPOINT ["uv", "run", "--no-dev", "python", "-m", "sitcon_bot"]
