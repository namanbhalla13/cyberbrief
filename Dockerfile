# syntax=docker/dockerfile:1
# One container: FastAPI serves the built frontend and the API. One worker (SQLite + in-process runner).

FROM node:24-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund --legacy-peer-deps
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim AS runtime
ARG WITH_FFMPEG=false
ARG BUILD_ID=dev
RUN if [ "$WITH_FFMPEG" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*; \
    fi
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd backend && uv sync --frozen --no-dev --no-install-project
COPY backend/ ./backend/
COPY evals/fixtures/ ./evals/fixtures/
COPY scripts/ ./scripts/
COPY --from=frontend /app/frontend/dist ./frontend/dist
RUN useradd -r -u 10001 cyberbrief && mkdir -p /app/data && chown -R cyberbrief /app/data
ENV PATH="/app/backend/.venv/bin:$PATH" \
    BUILD_ID=$BUILD_ID \
    APP_ENV=production \
    AI_MODE=fixture \
    DATABASE_URL=sqlite:////app/data/cyberbrief.db \
    STORAGE_ROOT=/app/data/private \
    CHECKPOINT_DB=/app/data/checkpoints.db \
    AUDIO_CONVERTER=none \
    COOKIE_SECURE=true
USER cyberbrief
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"
WORKDIR /app/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
