"""Crawl job persistence backends for BlazeCrawl Core.

Each backend keeps two kinds of record per job: the job *state* (status,
counters, settings, frontier and visited URLs) and one *page* record per
fetched page, keyed by its index. ``commit`` writes new page records and the
state together. The state is the commit point: page records beyond the
state's ``pages_crawled`` are ignored on load and rewritten by a later commit.

Backends:
* ``MemoryJobStore`` (default): process-local; nothing survives a restart.
* ``RedisJobStore``: used when ``REDIS_URL`` is reachable at startup. A commit is one MULTI transaction.
* ``DiskJobStore``: opt-in (``CRAWL_CHECKPOINTS_ENABLED=true``). JSON files under
  ``<state dir>/crawl-checkpoints``, written atomically (temp file, fsync, rename).

If Redis is unreachable at startup, the disk store (when enabled) or the memory
store is used and a warning is logged. Write failures at runtime are logged by
the manager, and the crawl continues.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from blazecrawl_core.api.keyfile import state_dir
from blazecrawl_core.config import settings
from blazecrawl_core.logging import get_logger

logger = get_logger(__name__)

PageRecords = dict[int, dict[str, Any]]
StoredJob = tuple[dict[str, Any], PageRecords]

_REDIS_STATE_PREFIX = "bc:crawl:job:"
_REDIS_PAGES_PREFIX = "bc:crawl:pages:"
_CHECKPOINT_DIR_NAME = "crawl-checkpoints"
_JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CONNECT_ATTEMPTS = 3
_CONNECT_RETRY_DELAY_SECONDS = 1.0
_PING_TIMEOUT_SECONDS = 2.0


class JobStore(Protocol):
    async def commit(self, job_id: str, state: dict[str, Any], new_pages: PageRecords) -> None:
        """Persist ``new_pages``, then ``state``. The state write is the commit point."""

    async def load_jobs(self) -> list[StoredJob]:
        """Return ``(state, pages_by_index)`` for each stored job. Unreadable records are skipped."""

    async def close(self) -> None: ...


def _decode_object(raw: Any) -> dict[str, Any] | None:
    """Decode a stored JSON object. Log and return None for anything else."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning("Skipping unreadable crawl record", error=str(e))
        return None
    if not isinstance(value, dict):
        logger.warning("Skipping crawl record that is not a JSON object")
        return None
    return value


class MemoryJobStore:
    def __init__(self) -> None:
        # JSON text, so stored records never alias live job objects.
        self._states: dict[str, str] = {}
        self._pages: dict[str, dict[int, str]] = {}

    async def commit(self, job_id: str, state: dict[str, Any], new_pages: PageRecords) -> None:
        encoded_pages = {index: json.dumps(page) for index, page in new_pages.items()}
        encoded_state = json.dumps(state)
        self._pages.setdefault(job_id, {}).update(encoded_pages)
        self._states[job_id] = encoded_state

    async def load_jobs(self) -> list[StoredJob]:
        records: list[StoredJob] = []
        for job_id, raw_state in self._states.items():
            state = _decode_object(raw_state)
            if state is None:
                continue
            pages: PageRecords = {}
            for index, raw_page in self._pages.get(job_id, {}).items():
                page = _decode_object(raw_page)
                if page is not None:
                    pages[index] = page
            records.append((state, pages))
        return records

    async def close(self) -> None:
        return None


class RedisJobStore:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def commit(self, job_id: str, state: dict[str, Any], new_pages: PageRecords) -> None:
        async with self._client.pipeline(transaction=True) as pipe:
            for index, page in new_pages.items():
                pipe.hset(_REDIS_PAGES_PREFIX + job_id, str(index), json.dumps(page))
            pipe.set(_REDIS_STATE_PREFIX + job_id, json.dumps(state))
            await pipe.execute()

    async def load_jobs(self) -> list[StoredJob]:
        keys = [key async for key in self._client.scan_iter(match=_REDIS_STATE_PREFIX + "*")]
        records: list[StoredJob] = []
        for key in keys:
            state = _decode_object(await self._client.get(key))
            if state is None:
                continue
            job_id = key[len(_REDIS_STATE_PREFIX) :]
            pages: PageRecords = {}
            raw_pages = await self._client.hgetall(_REDIS_PAGES_PREFIX + job_id)
            for field_name, raw_page in raw_pages.items():
                try:
                    index = int(field_name)
                except ValueError:
                    logger.warning("Skipping crawl page with invalid index", job_id=job_id)
                    continue
                page = _decode_object(raw_page)
                if page is not None:
                    pages[index] = page
            records.append((state, pages))
        return records

    async def close(self) -> None:
        await self._client.aclose()


class DiskJobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def commit(self, job_id: str, state: dict[str, Any], new_pages: PageRecords) -> None:
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            raise ValueError("job id is not safe to use as a checkpoint directory name")
        job_dir = self.root / job_id
        pages_dir = job_dir / "pages"
        pages_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for index, page in new_pages.items():
            _write_json_atomic(pages_dir / f"{index:08d}.json", page)
        _write_json_atomic(job_dir / "state.json", state)

    async def load_jobs(self) -> list[StoredJob]:
        if not self.root.is_dir():
            return []
        records: list[StoredJob] = []
        for job_dir in sorted(self.root.iterdir()):
            if not job_dir.is_dir():
                continue
            state = _decode_object(_read_text(job_dir / "state.json"))
            if state is None:
                continue
            pages: PageRecords = {}
            for path in (job_dir / "pages").glob("*.json"):
                if not path.stem.isdigit():
                    continue
                page = _decode_object(_read_text(path))
                if page is not None:
                    pages[int(path.stem)] = page
            records.append((state, pages))
        return records

    async def close(self) -> None:
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Skipping unreadable crawl checkpoint file", file=path.name, error=str(e))
        return None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if os.name == "posix":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


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


def checkpoint_dir() -> Path:
    return state_dir() / _CHECKPOINT_DIR_NAME


async def build_job_store() -> JobStore:
    """Choose the store: Redis if reachable, else disk checkpoints if enabled, else memory."""
    if settings.REDIS_URL:
        try:
            return await _connect_redis(settings.REDIS_URL)
        except Exception as e:
            logger.warning(
                "Redis unavailable; falling back to a non-Redis crawl job store",
                error=str(e) or type(e).__name__,
            )
    if settings.CRAWL_CHECKPOINTS_ENABLED:
        disk = DiskJobStore(checkpoint_dir())
        try:
            disk.ensure_root()
        except OSError as e:
            logger.warning(
                "Crawl checkpoint directory unusable; falling back to in-memory crawl job store",
                error=str(e),
            )
        else:
            logger.info("Using disk crawl checkpoints", path=str(disk.root))
            return disk
    logger.info("Using in-memory crawl job store")
    return MemoryJobStore()
