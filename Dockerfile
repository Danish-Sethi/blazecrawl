# BlazeCrawl Core — self-hosted image.
#
# Multi-stage: install Python deps, install the Chromium runtime for
# Playwright, then run the API as a non-root user.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps for Playwright Chromium + lxml.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the package with optional Redis cache + structured-data extras.
COPY pyproject.toml README.md LICENSE ./
COPY blazecrawl_core ./blazecrawl_core
RUN pip install ".[redis,structured]" \
    && rm -rf blazecrawl_core

# Install Chromium + its OS dependencies for Playwright into a shared location
# readable by the non-root runtime user.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

# Non-root runtime user + writable persistent state dir (API key bootstrap).
RUN useradd --create-home --uid 10001 blazecrawl \
    && mkdir -p /data/blazecrawl \
    && chown -R blazecrawl:blazecrawl /data/blazecrawl
USER blazecrawl

ENV BLAZECRAWL_HOST=0.0.0.0 \
    BLAZECRAWL_PORT=8000 \
    BLAZECRAWL_STATE_DIR=/data/blazecrawl

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["blazecrawl-server"]
