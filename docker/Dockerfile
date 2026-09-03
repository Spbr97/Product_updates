# Product Tracker image.
#
# Default build is lean (no browser). For sites that need JavaScript rendering, build with
# the Playwright base image, which ships Chromium and its system libraries:
#
#   docker build -f docker/Dockerfile \
#     --build-arg BASE_IMAGE=mcr.microsoft.com/playwright/python:v1.47.0-jammy \
#     --build-arg EXTRAS=browser -t product-tracker:browser .
#
ARG BASE_IMAGE=python:3.12-slim

# --- Stage 1: build the Product Entry SPA -------------------------------------------
# Node is a build-time tool only; it never enters the runtime image. The output is a
# handful of static files under src/product_tracker/web/app/ that the API serves at /ui.
FROM node:20-slim AS frontend

WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# vite writes the bundle to /src/product_tracker/web/app (see outDir in vite.config.ts).
RUN npm run build


FROM ${BASE_IMAGE} AS base

ARG EXTRAS=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libpq is needed by psycopg; curl is used by the compose healthcheck.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency metadata first so `pip install` is cached across source edits.
COPY pyproject.toml README.md ./
COPY src ./src
# The built SPA, from stage 1, so the wheel's package-data has something to ship.
COPY --from=frontend /src/product_tracker/web/app ./src/product_tracker/web/app

RUN if [ -n "${EXTRAS}" ]; then pip install ".[${EXTRAS}]"; else pip install "."; fi

COPY alembic.ini ./
COPY migrations ./migrations
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Run as a non-root user: nothing here needs privileges.
RUN useradd --create-home --uid 10001 tracker && chown -R tracker:tracker /app
USER tracker

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "product_tracker.api.app:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
