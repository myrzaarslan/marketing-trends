# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — build the React/Vite frontend into static files
# ---------------------------------------------------------------------------
FROM node:20-bookworm-slim AS web
WORKDIR /web

# Install deps against the lockfile first (better layer caching).
COPY web/package.json web/package-lock.json ./
RUN npm ci

# Build the SPA → /web/dist
COPY web/ ./
RUN npm run build


# ---------------------------------------------------------------------------
# Stage 2 — Python runtime with Playwright browsers + ffmpeg
# ---------------------------------------------------------------------------
# The Playwright base image ships Chromium/WebKit/Firefox + all their system
# libraries preinstalled at /ms-playwright. The tag MUST match the pinned
# playwright version in requirements.txt (1.60.0) so the browser binaries and
# the Python client agree.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# yt-dlp needs ffmpeg to merge separate video/audio streams (e.g. TikTok H.264).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (playwright==1.60.0 is already satisfied by the base image, so
# pip skips re-downloading the browsers).
COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

# Application code only — mutable state (data/, profiles/, secrets/) is mounted
# as volumes at runtime, never baked into the image.
COPY adapters/ ./adapters/
COPY api/ ./api/
COPY core/ ./core/
COPY enrichment/ ./enrichment/

# Built frontend from stage 1 — FastAPI serves it from "/" (see api/main.py).
COPY --from=web /web/dist ./web/dist

EXPOSE 8001

# 0.0.0.0 so the host's published port can reach it from outside the container.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8001"]
