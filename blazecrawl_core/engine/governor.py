"""Single-manager page-fetch admission; no policy for browser subresources."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from blazecrawl_core.config import Settings, settings


class CrawlFetchDenied(RuntimeError):
    """Admission was denied; a scraper must not hide it with browser fallback."""


class CrawlBudgetExceeded(CrawlFetchDenied):
    """The manager's finite page-fetch-attempt allowance has been spent."""


def retry_after_seconds(value: str | None, now: float) -> float | None:
    """Parse HTTP Retry-After (integer seconds or an aware HTTP date)."""
    if not value:
        return None
    value = value.strip()
    try:
        if value.isascii() and value.isdecimal():
            delay = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            delay = max(0.0, date.timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return None
    return delay if math.isfinite(delay) else None


@dataclass
class _Host:
    semaphore: asyncio.Semaphore
    tokens: float
    updated: float
    last_started: float | None = None
    crawl_delay: float = 0.0
    retry_at: float = 0.0


class CrawlGovernor:
    """Shared host buckets/semaphores and budget for every job in one manager."""

    def __init__(
        self,
        semaphore: asyncio.Semaphore,
        config: Settings = settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.clock = clock
        self.sleep = sleep
        self.wall_clock = wall_clock
        self.attempts = 0
        self._semaphore = semaphore
        self._hosts: dict[str, _Host] = {}

    def check_budget(self) -> None:
        if self.attempts >= self.config.CRAWL_FETCH_BUDGET:
            raise CrawlBudgetExceeded(
                f"Crawl page-fetch attempt budget exhausted ({self.config.CRAWL_FETCH_BUDGET})"
            )

    def _delay(self, host: _Host) -> float:
        now = self.clock()
        host.tokens = min(
            self.config.CRAWL_HOST_BURST,
            host.tokens + (now - host.updated) * self.config.CRAWL_HOST_RATE,
        )
        host.updated = now
        bucket_delay = max(0.0, (1 - host.tokens) / self.config.CRAWL_HOST_RATE)
        robots_delay = (
            0.0
            if host.last_started is None
            else max(0.0, host.crawl_delay - (now - host.last_started))
        )
        return max(bucket_delay, robots_delay, host.retry_at - now)

    def defer(self, url: str, seconds: float) -> None:
        """Apply a retry cooldown to this host, not to unrelated jobs/hosts."""
        host = self._hosts[urlsplit(url).hostname]
        retry_at = self.clock() + seconds
        if not math.isfinite(retry_at):
            raise CrawlFetchDenied("Retry-After exceeds the monotonic clock range")
        host.retry_at = max(host.retry_at, retry_at)

    @asynccontextmanager
    async def reserve(
        self, url: str, crawl_delay: float | None = None, *, deadline: float | None = None
    ) -> AsyncIterator[None]:
        hostname = urlsplit(url).hostname
        if hostname is None:
            raise ValueError("Crawl page-fetch URL must have a hostname")
        delay = crawl_delay if crawl_delay is not None else 0.0
        if delay < 0 or not math.isfinite(delay):
            raise ValueError("robots crawl-delay must be non-negative and finite")
        host = self._hosts.setdefault(
            hostname,
            _Host(
                asyncio.Semaphore(self.config.CRAWL_HOST_CONCURRENCY),
                self.config.CRAWL_HOST_BURST,
                self.clock(),
            ),
        )
        host.crawl_delay = max(host.crawl_delay, delay)
        while True:
            self.check_budget()
            wait = self._delay(host)
            if deadline is not None and wait >= deadline - self.clock():
                raise CrawlFetchDenied("Crawl page deadline exhausted while waiting for admission")
            if wait:
                await self.sleep(wait)
                continue
            await host.semaphore.acquire()
            try:
                await self._semaphore.acquire()
                try:
                    self.check_budget()
                    # Another job may have admitted while these semaphores were awaited.
                    if self._delay(host):
                        continue
                    if deadline is not None and self.clock() >= deadline:
                        raise CrawlFetchDenied("Crawl page deadline exhausted")
                    host.tokens -= 1
                    host.last_started = self.clock()
                    self.attempts += 1
                    yield
                    return
                finally:
                    self._semaphore.release()
            finally:
                host.semaphore.release()
