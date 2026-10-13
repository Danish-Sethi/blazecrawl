"""OSS runtime configuration for BlazeCrawl Core.

Single-node profile by design: no GCP, no Stripe, no Vertex, no SSO/SCIM, no
multi-region residency, no hosted identity. All settings are optional with
safe defaults so a fresh ``docker compose up`` reaches a healthy API without
any manual configuration.

Security posture is NOT relaxed in this profile: SSRF validation, DNS-rebinding
pin-at-connect, private-address blocking and request validation remain active.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import TypeVar

_Number = TypeVar("_Number", int, float)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _positive_number(name: str, default: _Number) -> _Number:
    raw = os.environ.get(name, "").strip()
    try:
        value = type(default)(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive finite {type(default).__name__}") from exc
    if value <= 0 or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError(f"{name} must be a positive finite {type(default).__name__}")
    return value


class Settings:
    """Minimal, self-host-friendly settings. No cloud coupling."""

    def __init__(self) -> None:
        # Runtime mode. "oss" is the only supported mode in this package.
        self.BLAZECRAWL_MODE: str = os.environ.get("BLAZECRAWL_MODE", "oss").strip().lower()

        # ------------------------------------------------------------------
        # Local auth (Stage 5 — Option B/C hybrid).
        #
        # A local API key is required by default so the engine is never an
        # unauthenticated open proxy. If the operator does not supply one, a
        # random key is generated at startup and printed to the logs.
        #
        # Auth may be disabled ONLY when explicitly opted in AND bound to
        # loopback, for trusted local development. See api/auth.py.
        # ------------------------------------------------------------------
        self.BLAZECRAWL_API_KEY: str | None = os.environ.get("BLAZECRAWL_API_KEY") or None
        self.BLAZECRAWL_AUTH_DISABLED: bool = _env_bool("BLAZECRAWL_AUTH_DISABLED", False)
        self.BLAZECRAWL_HOST: str = os.environ.get("BLAZECRAWL_HOST", "127.0.0.1")
        self.BLAZECRAWL_PORT: int = _env_int("BLAZECRAWL_PORT", 8000)

        # Browser pool / rendering.
        self.BROWSER_POOL_SIZE: int = _env_int("BROWSER_POOL_SIZE", 2)
        self.MAX_CONCURRENT_SCRAPES: int = _env_int("MAX_CONCURRENT_SCRAPES", 8)
        self.BROWSER_HEADLESS: bool = _env_bool("BROWSER_HEADLESS", True)
        self.DEFAULT_TIMEOUT_MS: int = _env_int("DEFAULT_TIMEOUT_MS", 30000)
        self.METRICS_ENABLED: bool = _env_bool("METRICS_ENABLED", False)

        # Screenshot capture (formats=["screenshot"]): hard cap on raw PNG bytes.
        self.SCREENSHOT_MAX_BYTES: int = _env_int("SCREENSHOT_MAX_BYTES", 5 * 1024 * 1024)
        if self.SCREENSHOT_MAX_BYTES <= 0:
            raise ValueError("SCREENSHOT_MAX_BYTES must be a positive integer")

        # Optional Redis: caching, and crawl job checkpoints (falls back to in-process if unreachable).
        self.REDIS_URL: str | None = os.environ.get("REDIS_URL") or None
        self.CACHE_DEFAULT_TTL_SECONDS: int = _env_int("CACHE_DEFAULT_TTL_SECONDS", 3600)

        # Crawl queue backend: "memory" (default, zero-infra) or "redis".
        self.CRAWL_QUEUE_BACKEND: str = (
            os.environ.get("CRAWL_QUEUE_BACKEND", "memory").strip().lower()
        )

        # Crawl checkpoints on local disk (opt-in). Used when Redis is not configured or
        # unreachable. Files are written under <state dir>/crawl-checkpoints.
        self.CRAWL_CHECKPOINTS_ENABLED: bool = _env_bool("CRAWL_CHECKPOINTS_ENABLED", False)

        # Crawl limits.
        self.CRAWL_MAX_PAGES_DEFAULT: int = _env_int("CRAWL_MAX_PAGES_DEFAULT", 50)
        self.CRAWL_MAX_DEPTH_DEFAULT: int = _env_int("CRAWL_MAX_DEPTH_DEFAULT", 2)
        self.CRAWL_MAX_CONCURRENCY: int = _positive_number("CRAWL_MAX_CONCURRENCY", 4)
        self.CRAWL_HOST_CONCURRENCY = _positive_number("CRAWL_HOST_CONCURRENCY", 2)
        self.CRAWL_HOST_RATE = _positive_number("CRAWL_HOST_RATE", 1.0)
        if not math.isfinite(1 / self.CRAWL_HOST_RATE):
            raise ValueError("CRAWL_HOST_RATE must have a finite reciprocal")
        self.CRAWL_HOST_BURST = _positive_number("CRAWL_HOST_BURST", 2)
        self.CRAWL_FETCH_BUDGET = _positive_number("CRAWL_FETCH_BUDGET", 10000)
        self.CRAWL_PAGE_ATTEMPTS = _positive_number("CRAWL_PAGE_ATTEMPTS", 3)
        self.CRAWL_PAGE_DEADLINE_SECONDS = _positive_number("CRAWL_PAGE_DEADLINE_SECONDS", 120.0)
        self.CRAWL_BACKOFF_SECONDS = _positive_number("CRAWL_BACKOFF_SECONDS", 1.0)
        self.CRAWL_BACKOFF_MAX_SECONDS = _positive_number("CRAWL_BACKOFF_MAX_SECONDS", 30.0)

        # Outbound fetch guardrails.
        self.MAX_RESPONSE_BYTES: int = _env_int("MAX_RESPONSE_BYTES", 20 * 1024 * 1024)
        self.MAX_REDIRECTS: int = _env_int("MAX_REDIRECTS", 5)
        self.EGRESS_UPSTREAM_PROXY: str | None = os.environ.get("EGRESS_UPSTREAM_PROXY")
        if self.EGRESS_UPSTREAM_PROXY is not None:
            from blazecrawl_core.network.upstream_proxy import UpstreamProxy

            UpstreamProxy.parse(self.EGRESS_UPSTREAM_PROXY)

        # robots.txt enforcement is ON by default and cannot be disabled in the
        # OSS core via configuration (the commercial bypass workflow is not part
        # of this package).
        self.RESPECT_ROBOTS_TXT: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
