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
