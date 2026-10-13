"""Crawler for BlazeCrawl Core (BFS, same-origin, robots-respecting).

Live jobs run as in-process asyncio tasks. Every lifecycle transition is also
snapshotted to a job store (``engine/job_store.py``): Redis when ``REDIS_URL``
is reachable, otherwise memory. Persisted jobs stay readable after a restart,
but crawl tasks cannot survive one, so interrupted jobs are marked ``failed``.

Security invariants preserved on every page fetch:
* every URL is SSRF-validated and pinned before fetching
* robots.txt is enforced (cannot be disabled in OSS core)
* only same-origin http/https URLs are followed
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urlsplit

from blazecrawl_core.config import settings
from blazecrawl_core.engine import robots
from blazecrawl_core.engine.job_store import JobStore, MemoryJobStore, build_job_store
from blazecrawl_core.engine.scraper import scrape
from blazecrawl_core.logging import get_logger
from blazecrawl_core.network.ssrf import SSRFValidationError, validate_and_pin

logger = get_logger(__name__)

_ACTIVE_STATUSES = frozenset({"queued", "running"})


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

    @classmethod
    def from_dict(cls, data: dict) -> CrawlJobState:
        return cls(
            job_id=data["job_id"],
            seed=data["seed"],
            status=data["status"],
            max_pages=data["max_pages"],
            max_depth=data["max_depth"],
            user_agent=data["user_agent"],
            pages=list(data["pages"]),
            pages_crawled=data["pages_crawled"],
            pages_failed=data["pages_failed"],
            error=data["error"],
            created_at=data["created_at"],
            finished_at=data["finished_at"],
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


class CrawlManager:
    """Crawl job manager + executor (single-node), with snapshots in a ``JobStore``."""

    def __init__(self, store: JobStore | None = None) -> None:
        self._store: JobStore = store or MemoryJobStore()
        self._jobs: dict[str, CrawlJobState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._sem = asyncio.Semaphore(settings.CRAWL_MAX_CONCURRENCY)

    async def restore(self) -> None:
        """Load persisted jobs. Jobs cut off by a restart are marked ``failed`` (no resume)."""
        try:
            records = await self._store.load_all()
        except Exception as e:
            logger.warning("Could not load persisted crawl jobs", error=str(e))
            return
        restored = 0
        interrupted = 0
        for record in records:
            try:
                job = CrawlJobState.from_dict(record)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed persisted crawl job", error=str(e))
                continue
            if job.status in _ACTIVE_STATUSES:
                job.status = "failed"
                job.error = "interrupted by restart"
                job.finished_at = time.time()
                await self._persist(job)
                interrupted += 1
            self._jobs[job.job_id] = job
            restored += 1
        logger.info("Restored crawl jobs", restored=restored, interrupted=interrupted)

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
        # Persist before the task exists so snapshots are written in order: queued → running → final.
        await self._persist(job)
        self._jobs[job.job_id] = job
        self._tasks[job.job_id] = asyncio.create_task(self._run(job))
        return job

    def get_job(self, job_id: str) -> CrawlJobState | None:
        return self._jobs.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        task = self._tasks.get(job_id)
        if not job or not task:
            return False
        task.cancel()
        job.status = "cancelled"
        job.finished_at = time.time()
        await self._persist(job)
        return True

    async def close(self) -> None:
        await self._store.close()

    async def _persist(self, job: CrawlJobState) -> None:
        try:
            await self._store.save(job.to_dict())
        except Exception as e:
            logger.warning("Failed to persist crawl job", job_id=job.job_id, error=str(e))

    async def _run(self, job: CrawlJobState) -> None:
        job.status = "running"
        await self._persist(job)
        try:
            await self._crawl(job)
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            logger.error("Crawl failed", job_id=job.job_id, error=str(e))
        finally:
            job.finished_at = time.time()
            await self._persist(job)

    async def _crawl(self, job: CrawlJobState) -> None:
        seed_host = urlsplit(job.seed).netloc
        visited: set[str] = set()
        frontier: list[tuple[str, int]] = [(job.seed, 0)]

        while frontier and job.pages_crawled < job.max_pages:
            url, depth = frontier.pop(0)
            url = urldefrag(url)[0]
            if url in visited or depth > job.max_depth:
                continue
            visited.add(url)

            # SSRF validation (defense-in-depth; seed validated at enqueue).
            try:
                await validate_and_pin(url)
            except SSRFValidationError:
                logger.warning("Crawl skipping SSRF-blocked URL", url=url)
                continue

            # robots.txt
            if settings.RESPECT_ROBOTS_TXT and not await robots.is_allowed(
                url, user_agent=job.user_agent
            ):
                logger.info("Crawl skipping robots-disallowed URL", url=url)
                continue

            async with self._sem:
                try:
                    result = await scrape(
                        url,
                        formats=["markdown", "links"],
                        render="auto",
                        user_agent=job.user_agent,
                    )
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
                except SSRFValidationError:
                    continue
                except Exception as e:
                    job.pages_failed += 1
                    logger.warning("Crawl page failed", url=url, error=str(e))


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
        with suppress(Exception):
            await manager.close()
