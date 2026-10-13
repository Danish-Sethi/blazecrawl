"""Subprocess worker for the hard-crash resume test (used only by tests/test_crawl_persistence.py).

``crash`` creates a job and dies with ``os._exit`` while the second page is in flight, so no
shutdown hook or final checkpoint runs. ``resume`` restores the job from disk in a new process
and runs it to completion.
"""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from blazecrawl_core.engine import crawler, robots
from blazecrawl_core.engine.crawler import CrawlManager
from blazecrawl_core.engine.job_store import DiskJobStore
from blazecrawl_core.network import ssrf

SEED = "https://example.com/"
A = "https://example.com/a"
B = "https://example.com/b"
C = "https://example.com/c"
D = "https://example.com/d"
GRAPH = {SEED: [A, B], A: [C], B: [D], C: [], D: []}


def _install_fakes(mode: str, fetch_log: Path) -> None:
    async def scrape(url, **_kwargs):
        with fetch_log.open("a", encoding="utf-8") as handle:
            handle.write(url + "\n")
        if mode == "crash" and url == B:
            os._exit(1)
        await asyncio.sleep(0.05)
        return SimpleNamespace(
            final_url=url,
            status_code=200,
            metadata={"title": url},
            markdown=f"# {url}",
            links=[{"url": link} for link in GRAPH[url]],
        )

    async def allowed(url, user_agent=robots.DEFAULT_USER_AGENT):
        return True

    crawler.scrape = scrape
    crawler.validate_and_pin = AsyncMock(return_value=None)
    ssrf.validate_and_pin = AsyncMock(return_value=None)
    crawler.robots.is_allowed = allowed
    crawler.robots.get_crawl_delay = AsyncMock(return_value=None)


async def _main() -> None:
    mode, root, fetch_log = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    _install_fakes(mode, fetch_log)
    manager = CrawlManager(DiskJobStore(root))
    if mode == "crash":
        await manager.create_job(SEED, 10, 2)
        await asyncio.sleep(30)  # os._exit inside scrape ends the process first
        return
    await manager.restore()
    for task in list(manager._tasks.values()):
        await task


if __name__ == "__main__":
    asyncio.run(_main())
