FROM python:3.11-alpine

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency manifests first (for Docker layer caching)
COPY pyproject.toml uv.lock ./

# Install project dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY interactive-bot/ ./interactive-bot/
COPY db/ ./db/
COPY assets/ ./assets/

CMD ["python", "-m", "interactive-bot"]
