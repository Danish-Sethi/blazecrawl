"""Crawler for BlazeCrawl Core (BFS, same-origin, robots-respecting).

Each job runs as an in-process asyncio task. After every fetched page, the
job's frontier, visited URLs, results and counters are checkpointed to a job
store (``engine/job_store.py``). On startup, queued and running jobs are
reloaded and resumed from their last checkpoint: pages fetched before that
checkpoint are not fetched again, and pages fetched after it are. A normal
shutdown suspends running jobs without marking them cancelled. Only an explicit
cancel makes a job terminal.

Security invariants preserved on every page fetch:
* every URL is SSRF-validated and pinned before fetching
* robots.txt is enforced (cannot be disabled in OSS core)
* only same-origin http/https URLs are followed
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag, urlsplit

from blazecrawl_core.config import settings
from blazecrawl_core.engine import robots
from blazecrawl_core.engine.governor import (
    CrawlBudgetExceeded,
    CrawlFetchDenied,
    CrawlGovernor,
    retry_after_seconds,
)
from blazecrawl_core.engine.job_store import JobStore, MemoryJobStore, build_job_store
from blazecrawl_core.engine.scraper import ScrapeResult, scrape
from blazecrawl_core.logging import get_logger
from blazecrawl_core.network.ssrf import SSRFValidationError, validate_and_pin

logger = get_logger(__name__)

_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled"})
_ACTIVE_STATUSES = frozenset({"queued", "running"})


def _field(
    data: dict[str, Any], key: str, kind: type | tuple[type, ...], *, nullable: bool = False
) -> Any:
    value = data.get(key)
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, kind):
        raise ValueError(f"crawl job field {key!r} has an invalid type")
    return value


def _count(data: dict[str, Any], key: str) -> int:
    value = _field(data, key, int)
    if value < 0:
        raise ValueError(f"crawl job field {key!r} must not be negative")
    return value


def _frontier_from(raw: Any) -> list[tuple[str, int]] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("crawl job field 'frontier' must be a list")
    frontier: list[tuple[str, int]] = []
    for entry in raw:
        if not (isinstance(entry, list) and len(entry) == 2):
            raise ValueError("frontier entries must be [url, depth] pairs")
        url, depth = entry
        if not isinstance(url, str) or isinstance(depth, bool) or not isinstance(depth, int):
            raise ValueError("frontier entries must be [url, depth] pairs")
        frontier.append((url, depth))
    return frontier


def _visited_from(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if not isinstance(raw, list) or not all(isinstance(url, str) for url in raw):
        raise ValueError("crawl job field 'visited' must be a list of URLs")
    return set(raw)


@dataclass
class CrawlJobState:
    job_id: str
    seed: str
    status: str = "queued"  # queued|running|completed|failed|cancelled
    max_pages: int = settings.CRAWL_MAX_PAGES_DEFAULT
    max_depth: int = settings.CRAWL_MAX_DEPTH_DEFAULT
    user_agent: str = robots.DEFAULT_USER_AGENT
    pages: list[dict] = field(default_factory=list)
    pages_crawled: int = 0
    pages_failed: int = 0
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # Next URLs to fetch as (url, depth); None until the crawl first runs. While a page
    # is in flight its URL is still in the last checkpoint, so a restart fetches it again.
    frontier: list[tuple[str, int]] | None = None
    visited: set[str] = field(default_factory=set)
    # Entries of ``pages`` already written to the store.
    persisted_pages: int = field(default=0, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: Any) -> CrawlJobState:
        """Build a job from ``to_dict()`` or ``to_state()`` output (with ``pages`` attached)."""
        if not isinstance(data, dict):
            raise ValueError("crawl job record must be a JSON object")
        status = _field(data, "status", str)
        if status not in _STATUSES:
            raise ValueError(f"unknown crawl status {status!r}")
        pages = _field(data, "pages", list)
        if any(not isinstance(page, dict) for page in pages):
            raise ValueError("crawl job pages must be objects")
        pages_crawled = _count(data, "pages_crawled")
        if len(pages) < pages_crawled:
            raise ValueError("crawl job has fewer page records than pages_crawled")
        return cls(
            job_id=_field(data, "job_id", str),
            seed=_field(data, "seed", str),
            status=status,
            max_pages=_count(data, "max_pages"),
            max_depth=_count(data, "max_depth"),
            user_agent=_field(data, "user_agent", str),
            pages=pages[:pages_crawled],
            pages_crawled=pages_crawled,
            pages_failed=_count(data, "pages_failed"),
            error=_field(data, "error", str, nullable=True),
            created_at=_field(data, "created_at", (int, float)),
            finished_at=_field(data, "finished_at", (int, float), nullable=True),
            frontier=_frontier_from(data.get("frontier")),
            visited=_visited_from(data.get("visited")),
        )

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "seed": self.seed,
            "status": self.status,
            "pages_crawled": self.pages_crawled,
            "pages_failed": self.pages_failed,
            "max_pages": self.max_pages,
            "max_depth": self.max_depth,
            "user_agent": self.user_agent,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "pages": self.pages,
        }

    def to_state(self) -> dict:
        """Everything needed to resume the job, without page records (stored separately)."""
        state = self.to_dict()
        del state["pages"]
        state["frontier"] = (
            None if self.frontier is None else [[url, depth] for url, depth in self.frontier]
        )
        state["visited"] = list(self.visited)
        return state


def _job_from_record(state: Any, page_records: dict[int, dict]) -> CrawlJobState:
    """Combine a stored state with its page records. Pages past pages_crawled are ignored."""
    if not isinstance(state, dict):
        raise TypeError("crawl job state must be a JSON object")
    count = state.get("pages_crawled")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("crawl job field 'pages_crawled' must be a non-negative integer")
    missing = [index for index in range(count) if index not in page_records]
    if missing:
        raise ValueError(f"crawl job is missing {len(missing)} page record(s)")
    return CrawlJobState.from_dict({**state, "pages": [page_records[i] for i in range(count)]})


class CrawlManager:
    """Crawl job manager and executor (single-node), checkpointed to a ``JobStore``."""

    def __init__(self, store: JobStore | None = None) -> None:
        self._store: JobStore = store or MemoryJobStore()
        self._jobs: dict[str, CrawlJobState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._sem = asyncio.Semaphore(settings.CRAWL_MAX_CONCURRENCY)
        self._governor = CrawlGovernor(self._sem)

    async def restore(self) -> None:
        """Load persisted jobs and resume the ones that were queued or running."""
        try:
            records = await self._store.load_jobs()
        except Exception as e:
            logger.warning("Could not load persisted crawl jobs", error=str(e) or type(e).__name__)
            return
        restored = resumed = 0
        for state, page_records in records:
            try:
                job = _job_from_record(state, page_records)
            except (TypeError, ValueError) as e:
                logger.warning("Skipping unreadable persisted crawl job", error=str(e))
                continue
            job.persisted_pages = len(job.pages)
            self._jobs[job.job_id] = job
            restored += 1
            if job.status in _ACTIVE_STATUSES:
                self._start(job)
                resumed += 1
        logger.info("Restored crawl jobs", restored=restored, resumed=resumed)

    async def create_job(
        self,
        seed: str,
        max_pages: int | None,
        max_depth: int | None,
        user_agent: str = robots.DEFAULT_USER_AGENT,
    ) -> CrawlJobState:
        job = CrawlJobState(
            job_id=str(uuid.uuid4()),
            seed=seed,
            max_pages=max_pages or settings.CRAWL_MAX_PAGES_DEFAULT,
            max_depth=max_depth or settings.CRAWL_MAX_DEPTH_DEFAULT,
            user_agent=user_agent,
        )
        # Checkpoint before the task starts, so a job accepted with 202 survives a crash.
        await self._checkpoint(job)
        self._jobs[job.job_id] = job
        self._start(job)
        return job

    def get_job(self, job_id: str) -> CrawlJobState | None:
        return self._jobs.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        task = self._tasks.get(job_id)
        if not job or not task:
            return False
        job.status = "cancelled"
        job.finished_at = time.time()
        if not task.done():
            task.cancel()
            # Wait for the crawl to unwind, so this write is the only writer left. This also
            # covers a task cancelled before its body ran, which would otherwise persist nothing.
            await asyncio.gather(task, return_exceptions=True)
        await self._checkpoint(job)
        return True

    async def shutdown(self) -> None:
        """Stop running jobs without cancelling them, then close the store.

        Suspended jobs keep their last checkpoint and resume on the next startup.
        """
        running = [task for task in self._tasks.values() if not task.done()]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await self._store.close()

    def _start(self, job: CrawlJobState) -> None:
        self._tasks[job.job_id] = asyncio.create_task(self._run(job))

    async def _checkpoint(self, job: CrawlJobState) -> None:
        """Write unsaved pages, then the state. Failures are logged and the crawl continues."""
        new_pages = {
            index: job.pages[index] for index in range(job.persisted_pages, len(job.pages))
        }
        try:
            await self._store.commit(job.job_id, job.to_state(), new_pages)
        except Exception as e:
            logger.warning(
                "Failed to checkpoint crawl job",
                job_id=job.job_id,
                error=str(e) or type(e).__name__,
            )
            return
        job.persisted_pages = len(job.pages)

    async def _run(self, job: CrawlJobState) -> None:
        try:
            job.status = "running"
            await self._checkpoint(job)
            await self._crawl(job)
            job.status = "completed"
        except asyncio.CancelledError:
            # cancel() checkpoints the cancelled state after this task unwinds. On shutdown the
            # last checkpoint stays the resume point.
            raise
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            logger.error("Crawl failed", job_id=job.job_id, error=str(e))
        job.finished_at = time.time()
        await self._checkpoint(job)

    async def _crawl(self, job: CrawlJobState) -> None:
        seed_host = urlsplit(job.seed).netloc
        if job.frontier is None:
            job.frontier = [(job.seed, 0)]
            await self._checkpoint(job)
        frontier = job.frontier
        visited = job.visited

        while frontier and job.pages_crawled < job.max_pages:
            url, depth = frontier[0]
            url = urldefrag(url)[0]
            if url in visited or depth > job.max_depth:
                frontier.pop(0)
                continue

            # SSRF validation (defense-in-depth; seed validated at enqueue).
            try:
                await validate_and_pin(url)
            except SSRFValidationError:
                logger.warning("Crawl skipping SSRF-blocked URL", url=url)
                visited.add(url)
                frontier.pop(0)
                await self._checkpoint(job)
                continue

            # robots.txt
            if settings.RESPECT_ROBOTS_TXT and not await robots.is_allowed(
                url, user_agent=job.user_agent
            ):
                logger.info("Crawl skipping robots-disallowed URL", url=url)
                visited.add(url)
                frontier.pop(0)
                await self._checkpoint(job)
                continue

            try:
                delay = await robots.get_crawl_delay(url, user_agent=job.user_agent)
                result = await self._fetch_page(job, url, delay)
                job.pages_crawled += 1
                job.pages.append(
                    {
                        "url": result.final_url,
                        "status_code": result.status_code,
                        "title": result.metadata.get("title"),
                        "markdown": result.markdown,
                    }
                )
                if depth < job.max_depth:
                    for link in result.links or []:
                        lu = link.get("url")
                        if not lu:
                            continue
                        lu = urldefrag(lu)[0]
                        if lu in visited:
                            continue
                        if urlsplit(lu).netloc != seed_host:
                            continue
                        if urlsplit(lu).scheme not in ("http", "https"):
                            continue
                        frontier.append((lu, depth + 1))
            except CrawlBudgetExceeded:
                raise
            except SSRFValidationError:
                logger.warning("Crawl skipping SSRF-blocked URL", url=url)
            except Exception as e:
                job.pages_failed += 1
                logger.warning("Crawl page failed", url=url, error=str(e))
            visited.add(url)
            frontier.pop(0)
            await self._checkpoint(job)

    async def _fetch_page(
        self, job: CrawlJobState, url: str, crawl_delay: float | None
    ) -> ScrapeResult:
        governor = self._governor
        config = governor.config
        deadline = governor.clock() + config.CRAWL_PAGE_DEADLINE_SECONDS
        attempts = 0
        backoff = min(config.CRAWL_BACKOFF_SECONDS, config.CRAWL_BACKOFF_MAX_SECONDS)

        @asynccontextmanager
        async def admission() -> AsyncIterator[None]:
            nonlocal attempts
            if attempts >= config.CRAWL_PAGE_ATTEMPTS:
                raise CrawlFetchDenied("Crawl page-fetch attempt limit exhausted")
            async with governor.reserve(url, crawl_delay, deadline=deadline):
                attempts += 1
                yield

        # The real asyncio deadline also bounds slow network/renderer calls; monotonic
        # checks cover injected clocks and prevent Retry-After from extending it.
        async with asyncio.timeout(config.CRAWL_PAGE_DEADLINE_SECONDS):
            while True:
                if governor.clock() >= deadline:
                    raise CrawlFetchDenied("Crawl page deadline exhausted")
                result = await scrape(
                    url,
                    formats=["markdown", "links"],
                    render="auto",
                    user_agent=job.user_agent,
                    fetch_context=admission,
                )
                if governor.clock() >= deadline:
                    raise CrawlFetchDenied("Crawl page deadline exhausted")
                if result.status_code != 429 and not 500 <= result.status_code <= 599:
                    return result
                retry_after = retry_after_seconds(result.retry_after, governor.wall_clock())
                wait = max(backoff, retry_after or 0.0)
                governor.defer(url, wait)
                if attempts >= config.CRAWL_PAGE_ATTEMPTS:
                    raise CrawlFetchDenied(
                        f"HTTP {result.status_code}: crawl page-fetch attempt limit exhausted"
                    )
                if wait >= deadline - governor.clock():
                    raise CrawlFetchDenied(
                        f"HTTP {result.status_code}: crawl page deadline exhausted"
                    )
                logger.info("Crawl retrying page", url=url, status=result.status_code, delay=wait)
                await governor.sleep(wait)
                backoff = min(backoff * 2, config.CRAWL_BACKOFF_MAX_SECONDS)


_manager: CrawlManager | None = None
_manager_lock = asyncio.Lock()


async def get_crawl_manager() -> CrawlManager:
    global _manager
    async with _manager_lock:
        if _manager is None:
            manager = CrawlManager(await build_job_store())
            await manager.restore()
            _manager = manager
        return _manager


async def close_crawl_manager() -> None:
    global _manager
    manager, _manager = _manager, None
    if manager is not None:
        try:
            await manager.shutdown()
        except Exception as e:
            logger.warning("Crawl manager shutdown failed", error=str(e) or type(e).__name__)
