# --- Stage 1: build the React frontend ---------------------------------------
FROM node:20-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Empty = same origin: the API serves the UI, so no host/port is baked in.
ENV VITE_API_URL=""
RUN npm run build

# --- Stage 2: runtime with every tool SonarFix shells out to -----------------
FROM python:3.11-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# git for repo work; JDK/Maven/CMake/g++ for build verification;
# Node + Claude Code CLI only matter for SONARFIX_ENGINE=claude-code.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git openssh-client ca-certificates curl \
        openjdk-17-jdk-headless maven \
        cmake build-essential \
        nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra all

COPY --from=frontend /frontend/dist ./frontend/dist

EXPOSE 8000
VOLUME ["/app/data"]
CMD ["sonarfix", "api", "--host", "0.0.0.0", "--port", "8000"]
