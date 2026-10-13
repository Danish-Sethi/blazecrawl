"""Result caching for BlazeCrawl Core.

Backends:
* ``memory`` (default, zero-infrastructure): process-local TTL dict.
* ``redis``: shared cache across processes when ``REDIS_URL`` is configured.

The cache is a performance optimisation only; it is safe to run with the
memory backend. Keys are derived from the normalized URL + requested formats.
"""

from __future__ import annotations

import hashlib
import json
import time

from blazecrawl_core.config import settings
from blazecrawl_core.logging import get_logger
from blazecrawl_core.network.headers import validate_headers

logger = get_logger(__name__)


class _MemoryCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, str]] = {}

    async def get(self, key: str) -> str | None:
        item = self._store.get(key)
        if not item:
            return None
        expires, value = item
        if time.time() > expires:
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._store[key] = (time.time() + ttl, value)


class _RedisCache:
    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self._client.set(key, value, ex=ttl)


_cache = None


def _get_cache():
    global _cache
    if _cache is not None:
        return _cache
    if settings.REDIS_URL:
        try:
            _cache = _RedisCache(settings.REDIS_URL)
            logger.info("Using Redis cache backend")
            return _cache
        except Exception as e:
            logger.warning("Redis unavailable; falling back to memory cache", error=str(e))
    _cache = _MemoryCache()
    return _cache


def make_key(
    url: str,
    formats: list[str] | None,
    only_main_content: bool,
    render: str = "auto",
    user_agent: str = "BlazeCrawl/0.1.0",
    full_page: bool = False,
    headers: dict[str, str] | None = None,
) -> str:
    normalized = validate_headers(headers)
    base = f"{url}|{sorted(formats or ['markdown'])}|{int(only_main_content)}|{render}|{user_agent}"
    # full_page only changes screenshot output, so it only varies the key for screenshot requests.
    if "screenshot" in (formats or []):
        base += f"|full_page={full_page}"
    if normalized:
        base += f"|{sorted(normalized.items())}"
    return "bc:scrape:" + hashlib.sha256(base.encode()).hexdigest()


async def get_cached(
    url: str,
    formats: list[str] | None,
    only_main_content: bool,
    render: str = "auto",
    user_agent: str = "BlazeCrawl/0.1.0",
    full_page: bool = False,
    headers: dict[str, str] | None = None,
) -> dict | None:
    try:
        raw = await _get_cache().get(
            make_key(url, formats, only_main_content, render, user_agent, full_page, headers)
        )
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.warning("Cache get failed", error=str(e))
        return None


async def set_cached(
    url: str,
    formats: list[str] | None,
    only_main_content: bool,
    payload: dict,
    ttl: int | None = None,
    render: str = "auto",
    user_agent: str = "BlazeCrawl/0.1.0",
    full_page: bool = False,
    headers: dict[str, str] | None = None,
) -> None:
    try:
        await _get_cache().set(
            make_key(url, formats, only_main_content, render, user_agent, full_page, headers),
            json.dumps(payload),
            ttl or settings.CACHE_DEFAULT_TTL_SECONDS,
        )
    except Exception as e:
        logger.warning("Cache set failed", error=str(e))
