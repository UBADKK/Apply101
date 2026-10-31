# Private test deployment (Railway): one service serves the web client and
# the API (backend/app/deploy_app.py). Data lives on the volume mounted at
# APPLY101_DATA_DIR; .dockerignore keeps .env files, SQLite files, uploads/
# and backups/ at any depth out of the build context. See docs/RAILWAY_TEST_RUNBOOK.md.

FROM node:22.22.0-bookworm-slim AS web
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Same origin: the API is served under /api by deploy_app.
ENV VITE_API_BASE_URL=/api
RUN npm run build

FROM python:3.13.17-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APPLY101_LOAD_DOTENV=0 \
    APPLY101_ANALYSIS_PROVIDER=fake \
    APPLY101_WEB_DIR=/app/web
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY --from=web /build/frontend/dist /app/web
# One worker: SQLite on one volume, and one daily-sync thread per process.
# No proxy headers are trusted: X-Forwarded-For would let a client choose
# its own address for the rate limits.
CMD ["sh", "-c", "exec uvicorn --factory backend.app.deploy_app:create_app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-proxy-headers"]
