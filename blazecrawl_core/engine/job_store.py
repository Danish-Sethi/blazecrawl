"""Crawl job persistence backends for BlazeCrawl Core.

Backends:
* ``MemoryJobStore`` (default): process-local; nothing survives a restart.
* ``RedisJobStore``: used when ``REDIS_URL`` is set and reachable. If Redis
  cannot be reached at startup, the memory store is used instead and a warning
  is logged. Runtime write failures are logged and do not stop the crawl.

Stores hold JSON snapshots produced by ``CrawlJobState.to_dict()``. Persisted
jobs are never resumed; see ``CrawlManager.restore``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from contextlib import suppress
from typing import Any, Protocol

from blazecrawl_core.config import settings
from blazecrawl_core.logging import get_logger

logger = get_logger(__name__)

_REDIS_KEY_PREFIX = "bc:crawl:job:"
_CONNECT_ATTEMPTS = 3
_CONNECT_RETRY_DELAY_SECONDS = 1.0
_PING_TIMEOUT_SECONDS = 2.0


class JobStore(Protocol):
    async def save(self, record: dict[str, Any]) -> None:
        """Store or replace the snapshot whose ``job_id`` matches ``record``."""

    async def load_all(self) -> list[dict[str, Any]]:
        """Return every stored snapshot. Undecodable entries are skipped."""

    async def close(self) -> None: ...


def _decode_records(raw_values: Iterable[str | None]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in raw_values:
        if raw is None:
            continue
        try:
            records.append(json.loads(raw))
        except (TypeError, ValueError) as e:
            logger.warning("Skipping unreadable crawl job record", error=str(e))
    return records


class MemoryJobStore:
    def __init__(self) -> None:
        # Stored as JSON text so later mutation of a live job never leaks into the store.
        self._records: dict[str, str] = {}

    async def save(self, record: dict[str, Any]) -> None:
        self._records[record["job_id"]] = json.dumps(record)

    async def load_all(self) -> list[dict[str, Any]]:
        return _decode_records(self._records.values())

    async def close(self) -> None:
        return None


class RedisJobStore:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def save(self, record: dict[str, Any]) -> None:
        await self._client.set(_REDIS_KEY_PREFIX + record["job_id"], json.dumps(record))

    async def load_all(self) -> list[dict[str, Any]]:
        keys = [key async for key in self._client.scan_iter(match=_REDIS_KEY_PREFIX + "*")]
        if not keys:
            return []
        return _decode_records(await self._client.mget(keys))

    async def close(self) -> None:
        await self._client.aclose()


def _open_redis_client(url: str) -> Any:
    # Imported lazily: redis is an optional extra (`blazecrawl-core[redis]`).
    import redis.asyncio as aioredis

    return aioredis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=_PING_TIMEOUT_SECONDS,
        socket_timeout=5.0,
    )


async def _connect_redis(url: str) -> RedisJobStore:
    # Retries cover the startup race where the API starts before Redis accepts connections.
    client = _open_redis_client(url)
    last_error: Exception | None = None
    for attempt in range(_CONNECT_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_CONNECT_RETRY_DELAY_SECONDS)
        try:
            await asyncio.wait_for(client.ping(), timeout=_PING_TIMEOUT_SECONDS)
            return RedisJobStore(client)
        except Exception as e:
            last_error = e
    with suppress(Exception):
        await client.aclose()
    raise last_error


async def build_job_store() -> JobStore:
    """Return the Redis store when ``REDIS_URL`` is reachable, otherwise the memory store."""
    if not settings.REDIS_URL:
        return MemoryJobStore()
    try:
        store = await _connect_redis(settings.REDIS_URL)
    except Exception as e:
        logger.warning(
            "Redis unavailable; falling back to in-memory crawl job store",
            error=str(e) or type(e).__name__,
        )
        return MemoryJobStore()
    logger.info("Using Redis crawl job store")
    return store
