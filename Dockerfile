# syntax=docker/dockerfile:1.6

# ---------- frontend build ----------
FROM node:20-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend .
RUN npm run build

# ---------- backend image ----------
FROM python:3.12-slim AS backend
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git curl && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY tool tool
COPY server server
# Bring the built frontend into the package's static dir
COPY --from=frontend /app/server/obsmcp_server/frontend_dist server/obsmcp_server/frontend_dist

RUN pip install --upgrade pip && pip install .

ENV OBSMCP_HOST=0.0.0.0 \
    OBSMCP_PORT=8000

EXPOSE 8000
CMD ["obsmcp-server"]
