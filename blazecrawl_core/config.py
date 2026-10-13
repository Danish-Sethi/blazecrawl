"""OSS runtime configuration for BlazeCrawl Core.

Single-node profile by design: no GCP, no Stripe, no Vertex, no SSO/SCIM, no
multi-region residency, no hosted identity. All settings are optional with
safe defaults so a fresh ``docker compose up`` reaches a healthy API without
any manual configuration.

Security posture is NOT relaxed in this profile: SSRF validation, DNS-rebinding
pin-at-connect, private-address blocking and request validation remain active.
"""

from __future__ import annotations

import os
from functools import lru_cache


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

        # Optional Redis: caching, and crawl job persistence (falls back to in-process if unreachable).
        self.REDIS_URL: str | None = os.environ.get("REDIS_URL") or None
        self.CACHE_DEFAULT_TTL_SECONDS: int = _env_int("CACHE_DEFAULT_TTL_SECONDS", 3600)

        # Crawl queue backend: "memory" (default, zero-infra) or "redis".
        self.CRAWL_QUEUE_BACKEND: str = (
            os.environ.get("CRAWL_QUEUE_BACKEND", "memory").strip().lower()
        )

        # Crawl limits.
        self.CRAWL_MAX_PAGES_DEFAULT: int = _env_int("CRAWL_MAX_PAGES_DEFAULT", 50)
        self.CRAWL_MAX_DEPTH_DEFAULT: int = _env_int("CRAWL_MAX_DEPTH_DEFAULT", 2)
        self.CRAWL_MAX_CONCURRENCY: int = _env_int("CRAWL_MAX_CONCURRENCY", 4)

        # Outbound fetch guardrails.
        self.MAX_RESPONSE_BYTES: int = _env_int("MAX_RESPONSE_BYTES", 20 * 1024 * 1024)
        self.MAX_REDIRECTS: int = _env_int("MAX_REDIRECTS", 5)

        # robots.txt enforcement is ON by default and cannot be disabled in the
        # OSS core via configuration (the commercial bypass workflow is not part
        # of this package).
        self.RESPECT_ROBOTS_TXT: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
