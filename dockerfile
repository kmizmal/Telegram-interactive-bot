# syntax=docker/dockerfile:1

FROM debian:bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Runtime dependencies: Debian bookworm ships Python 3.11, matching .python-version.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Install uv from its official image.
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

WORKDIR /app

# Install dependencies in a separate layer for better build caching.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev

# Copy application source after dependency installation.
COPY interactive-bot/ ./interactive-bot/
COPY db/ ./db/
COPY scripts/ ./scripts/
COPY assets/ ./assets/

# Run as an unprivileged user; /app must stay writable for SQLite, pickle and log.txt.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin bot \
    && chown -R bot:bot /app

USER bot

ENTRYPOINT ["/app/.venv/bin/python", "-m", "interactive-bot"]
