"""Deterministic page admission, retries, cancellation and checkpoint interoperability."""

import asyncio
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from blazecrawl_core.config import Settings
from blazecrawl_core.engine import crawler, robots, scraper
from blazecrawl_core.engine.crawler import CrawlManager
from blazecrawl_core.engine.governor import (
    CrawlBudgetExceeded,
    CrawlGovernor,
    retry_after_seconds,
)
from blazecrawl_core.engine.job_store import DiskJobStore, MemoryJobStore, RedisJobStore
from blazecrawl_core.network.ssrf import SSRFValidationError
from tests.fakes import FakeRedis

URL = "https://example.com/"
OTHER = "https://other.example/"
HTML = "<html><body><article>" + "Useful article content. " * 60 + "</article></body></html>"


class Clock:
    def __init__(self, *, automatic=True):
        self.now = 0.0
        self.automatic = automatic
        self.waiting = []
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        if self.automatic:
            self.now += seconds
            await asyncio.sleep(0)
        else:
            future = asyncio.get_running_loop().create_future()
            self.waiting.append((self.now + seconds, future))
            try:
                await future
            finally:
                self.waiting = [(end, f) for end, f in self.waiting if f is not future]

    def advance(self, seconds):
        self.now += seconds
        for end, future in self.waiting:
            if end <= self.now and not future.done():
                future.set_result(None)


async def until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("task did not reach expected state")


def config(**overrides):
    result = Settings()
    for name, value in overrides.items():
        setattr(result, name, value)
    return result


def governor(clock, **overrides):
    cfg = config(**overrides)
    return CrawlGovernor(
        asyncio.Semaphore(cfg.CRAWL_MAX_CONCURRENCY),
        cfg,
        clock=clock,
        sleep=clock.sleep,
        wall_clock=lambda: 1000.0,
    )


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(crawler, "validate_and_pin", AsyncMock())
    monkeypatch.setattr(robots, "is_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(robots, "get_crawl_delay", AsyncMock(return_value=None))
    calls = []

    async def static(url, *_args):
        calls.append(url)
        return 200, HTML, url, None

    monkeypatch.setattr(scraper, "_fetch_static", static)
    return calls


def manager(clock, store=None, **overrides):
    result = CrawlManager(store)
    result._governor = governor(clock, **overrides)
    return result


async def settle(manager, *jobs):
    await asyncio.gather(*(manager._tasks[job.job_id] for job in jobs))


async def test_bucket_burst_refill_and_hostname_scope():
    clock = Clock()
    gov = governor(clock)
    times = []
    for url in [URL, "http://EXAMPLE.com:8080/a", URL, URL]:
        async with gov.reserve(url):
            times.append(clock())
    assert times == [0, 0, 1, 2]
    clock.now += 10
    async with gov.reserve(URL):
        pass
    async with gov.reserve(URL):
        pass
    async with gov.reserve(URL):
        assert clock() == 13
    assert len(gov._hosts) == 1


async def test_robots_delay_and_bucket_both_apply():
    clock = Clock()
    gov = governor(clock, CRAWL_HOST_RATE=0.5, CRAWL_HOST_BURST=1)
    times = []
    for delay in [3, 3, 1]:
        async with gov.reserve(URL, delay):
            times.append(clock())
    assert times == [0, 3, 6]  # strongest observed delay is retained across jobs
    gov = governor(clock, CRAWL_HOST_RATE=0.25, CRAWL_HOST_BURST=1)
    async with gov.reserve(URL, 1):
        pass
    async with gov.reserve(URL, 1):
        assert clock() == 10  # bucket, not robots, is now the limiting factor


async def test_host_concurrency_shared_by_separate_jobs_and_other_host_independent(
    web, monkeypatch
):
    clock = Clock(automatic=False)
    mgr = manager(clock, CRAWL_HOST_RATE=100.0, CRAWL_HOST_BURST=10)
    release = asyncio.Event()
    active = 0
    peak = 0
    started = []

    async def fetch(url, *_args):
        nonlocal active, peak
        started.append(url)
        if urlsplit(url).hostname == "example.com":
            active += 1
            peak = max(peak, active)
            try:
                await release.wait()
            finally:
                active -= 1
        return 200, HTML, url, None

    monkeypatch.setattr(scraper, "_fetch_static", fetch)
    jobs = [await mgr.create_job(URL + str(i), 1, 0) for i in range(3)]
    await until(lambda: len(started) == 2)
    independent = await mgr.create_job(OTHER, 1, 0)
    await settle(mgr, independent)
    assert independent.status == "completed"
    assert len(started) == 3 and peak == 2
    release.set()
    await settle(mgr, *jobs)
    assert peak == 2
    assert all(job.status == "completed" for job in jobs)
    assert mgr._governor.attempts == 4


async def test_global_concurrency_still_bounds_separate_hosts(web, monkeypatch):
    clock = Clock(automatic=False)
    mgr = manager(clock, CRAWL_MAX_CONCURRENCY=1)
    started = []
    release = asyncio.Event()

    async def fetch(url, *_args):
        started.append(url)
        await release.wait()
        return 200, HTML, url, None

    monkeypatch.setattr(scraper, "_fetch_static", fetch)
    first = await mgr.create_job(URL, 1, 0)
    await until(lambda: started)
    second = await mgr.create_job(OTHER, 1, 0)
    await until(lambda: mgr._governor._hosts.get("other.example"))
    assert started == [URL]
    release.set()
    await settle(mgr, first, second)
    assert started == [URL, OTHER]


async def test_crawler_passes_user_agent_and_robots_delay(web):
    clock = Clock()
    mgr = manager(clock)
    robots.get_crawl_delay.return_value = 3
    first = await mgr.create_job(URL, 1, 0, user_agent="ResearchBot/2")
    await settle(mgr, first)
    second = await mgr.create_job(URL + "a", 1, 0, user_agent="ResearchBot/2")
    await settle(mgr, second)
    assert clock() == 3
    robots.get_crawl_delay.assert_any_await(URL, user_agent="ResearchBot/2")
    assert mgr._governor.attempts == 2


@pytest.mark.parametrize("status", [429, 500, 503, 599])
async def test_bounded_exponential_backoff_and_terminal_failure(web, monkeypatch, status):
    clock = Clock()
    mgr = manager(clock, CRAWL_PAGE_ATTEMPTS=4, CRAWL_HOST_BURST=10, CRAWL_BACKOFF_MAX_SECONDS=2.0)
    times = []

    async def fetch(url, *_args):
        times.append(clock())
        return status, HTML, url, None

    monkeypatch.setattr(scraper, "_fetch_static", fetch)
    job = await mgr.create_job(URL, 1, 0)
    await settle(mgr, job)
    assert times == [0, 1, 3, 5]
    assert job.status == "completed"
    assert job.pages_failed == 1 and job.pages_crawled == 0
    assert job.visited == {URL} and job.frontier == []
    assert mgr._governor.attempts == 4


@pytest.mark.parametrize("header", ["5", formatdate(1005, usegmt=True)])
async def test_retry_after_seconds_and_http_date_then_success(web, monkeypatch, header):
    clock = Clock()
    mgr = manager(clock)
    times = []

    async def fetch(url, *_args):
        times.append(clock())
        return (429 if len(times) == 1 else 200), HTML, url, header

    monkeypatch.setattr(scraper, "_fetch_static", fetch)
    job = await mgr.create_job(URL, 1, 0)
    await settle(mgr, job)
    assert times == [0, 5]
    assert job.pages_crawled == 1 and job.pages_failed == 0
    assert mgr._governor.attempts == 2


@pytest.mark.parametrize("value", ["NaN", "inf", "-1", "1.5", "garbage", "9" * 400])
def test_invalid_retry_after_is_ignored(value):
    assert retry_after_seconds(value, 1000) is None


def test_past_retry_after_date_has_zero_delay():
    assert retry_after_seconds(formatdate(999, usegmt=True), 1000) == 0


async def test_retry_after_beyond_deadline_is_terminal_without_sleep(web, monkeypatch):
    clock = Clock()
    mgr = manager(clock, CRAWL_PAGE_DEADLINE_SECONDS=10.0)
    monkeypatch.setattr(scraper, "_fetch_static", AsyncMock(return_value=(429, HTML, URL, "100")))
    job = await mgr.create_job(URL, 1, 0)
    await settle(mgr, job)
    assert job.pages_failed == 1 and job.pages_crawled == 0
    assert mgr._governor.attempts == 1
    assert clock.sleeps == []


async def test_admission_deadline_does_not_spend_waiting_budget(web):
    clock = Clock()
    mgr = manager(clock, CRAWL_PAGE_DEADLINE_SECONDS=2.0)
    robots.get_crawl_delay.return_value = 5
    first = await mgr.create_job(URL, 1, 0)
    await settle(mgr, first)
    second = await mgr.create_job(URL + "a", 1, 0)
    await settle(mgr, second)
    assert first.pages_crawled == 1
    assert second.pages_failed == 1 and second.pages_crawled == 0
    assert mgr._governor.attempts == 1 and clock.sleeps == []


async def test_fetch_deadline_is_terminal_and_releases_permits(web, monkeypatch):
    clock = Clock()
    mgr = manager(clock, CRAWL_PAGE_DEADLINE_SECONDS=2.0)

    async def slow(url, *_args):
        clock.advance(3)
        return 200, HTML, url, None

    monkeypatch.setattr(scraper, "_fetch_static", slow)
    job = await mgr.create_job(URL, 1, 0)
    await settle(mgr, job)
    assert job.pages_failed == 1 and job.pages_crawled == 0
    assert mgr._governor.attempts == 1 and mgr._sem._value == 4
    assert mgr._governor._hosts["example.com"].semaphore._value == 2


@pytest.mark.parametrize("delay", [-1, float("nan"), float("inf")])
async def test_invalid_robots_delay_fails_without_sleep_or_budget(delay):
    clock = Clock()
    gov = governor(clock)
    with pytest.raises(ValueError, match="crawl-delay"):
        async with gov.reserve(URL, delay):
            pass
    assert gov.attempts == 0 and clock.sleeps == []


async def test_same_host_job_cannot_bypass_another_jobs_robots_wait(web):
    clock = Clock(automatic=False)
    mgr = manager(clock)
    robots.get_crawl_delay.return_value = 5
    first = await mgr.create_job(URL, 1, 0)
    await settle(mgr, first)
    second = await mgr.create_job(URL + "a", 1, 0)
    third = await mgr.create_job(URL + "b", 1, 0)
    await until(lambda: len(clock.waiting) == 2)
    assert web == [URL] and mgr._governor.attempts == 1
    independent = await mgr.create_job(OTHER, 1, 0)
    await settle(mgr, independent)
    assert web == [URL, OTHER]
    clock.advance(5)
    await until(lambda: len(web) == 3)
    assert len(clock.waiting) == 1
    clock.advance(5)
    await settle(mgr, second, third)
    assert mgr._governor.attempts == 4
    assert clock.now == 10


async def test_shared_budget_counts_retries_and_explicitly_fails_jobs(web, monkeypatch):
    clock = Clock()
    mgr = manager(clock, CRAWL_FETCH_BUDGET=2)
    monkeypatch.setattr(scraper, "_fetch_static", AsyncMock(return_value=(503, HTML, URL, None)))
    first = await mgr.create_job(URL, 1, 0)
    await settle(mgr, first)
    second = await mgr.create_job(OTHER, 1, 0)
    await settle(mgr, second)
    assert mgr._governor.attempts == 2
    for job in [first, second]:
        assert job.status == "failed" and "budget exhausted (2)" in job.error
        assert job.pages_crawled == 0 and not job.visited
        assert job.frontier == [(job.seed, 0)]
    assert scraper._fetch_static.await_count == 2
    assert clock.sleeps == [1.0]  # don't wait for another retry after the budget is spent


async def test_concurrent_jobs_cannot_overspend_shared_budget(web, monkeypatch):
    clock = Clock(automatic=False)
    mgr = manager(clock, CRAWL_FETCH_BUDGET=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(url, *_args):
        started.set()
        await release.wait()
        return 200, HTML, url, None

    monkeypatch.setattr(scraper, "_fetch_static", fetch)
    first = await mgr.create_job(URL, 1, 0)
    await started.wait()
    second = await mgr.create_job(URL + "a", 1, 0)
    third = await mgr.create_job(OTHER, 1, 0)
    await settle(mgr, second, third)
    assert second.status == third.status == "failed"
    assert "budget exhausted" in second.error and "budget exhausted" in third.error
    release.set()
    await settle(mgr, first)
    assert first.pages_crawled == 1 and mgr._governor.attempts == 1


async def test_auto_browser_fallback_is_another_budgeted_page_attempt(web, monkeypatch):
    clock = Clock()
    mgr = manager(clock, CRAWL_FETCH_BUDGET=1)
    monkeypatch.setattr(
        scraper, "_fetch_static", AsyncMock(return_value=(200, "<p>Thin</p>", URL, None))
    )
    browser = AsyncMock(return_value=(200, HTML, URL, None))
    monkeypatch.setattr(scraper, "_fetch_browser", browser)
    job = await mgr.create_job(URL, 1, 0)
    await settle(mgr, job)
    assert job.status == "failed" and "budget exhausted" in job.error
    assert mgr._governor.attempts == 1
    browser.assert_not_awaited()


async def test_successful_browser_fallback_obeys_robots_delay_and_counts_both_attempts(
    web, monkeypatch
):
    clock = Clock()
    mgr = manager(clock)
    robots.get_crawl_delay.return_value = 3
    times = []

    async def static(url, *_args):
        times.append(clock())
        return 200, "<p>Thin</p>", url, None

    async def browser(url, *_args):
        times.append(clock())
        return 200, HTML, url, None

    monkeypatch.setattr(scraper, "_fetch_static", static)
    monkeypatch.setattr(scraper, "_fetch_browser", browser)
    job = await mgr.create_job(URL, 1, 0)
    await settle(mgr, job)
    assert times == [0, 3]
    assert job.pages_crawled == 1 and mgr._governor.attempts == 2


async def test_static_network_error_fallback_cannot_bypass_page_attempt_limit(web, monkeypatch):
    mgr = manager(Clock(), CRAWL_PAGE_ATTEMPTS=1)
    monkeypatch.setattr(scraper, "_fetch_static", AsyncMock(side_effect=OSError("offline")))
    browser = AsyncMock()
    monkeypatch.setattr(scraper, "_fetch_browser", browser)
    job = await mgr.create_job(URL, 1, 0)
    await settle(mgr, job)
    assert job.pages_failed == 1 and job.pages_crawled == 0
    assert mgr._governor.attempts == 1
    browser.assert_not_awaited()


async def test_browser_retry_respects_page_attempt_limit(web, monkeypatch):
    clock = Clock()
    mgr = manager(clock, CRAWL_PAGE_ATTEMPTS=2)
    monkeypatch.setattr(
        scraper, "_fetch_static", AsyncMock(return_value=(200, "<p>Thin</p>", URL, None))
    )
    monkeypatch.setattr(scraper, "_fetch_browser", AsyncMock(return_value=(503, HTML, URL, "5")))
    job = await mgr.create_job(URL, 1, 0)
    await settle(mgr, job)
    assert job.pages_failed == 1 and job.pages_crawled == 0
    assert mgr._governor.attempts == 2
    assert clock.sleeps == []


async def test_backoff_is_shared_only_with_same_host_and_cancellation_cleans_waiters(
    web, monkeypatch
):
    clock = Clock(automatic=False)
    mgr = manager(clock)
    started = []

    async def fetch(url, *_args):
        started.append(url)
        return (429 if url == URL else 200), HTML, url, "5"

    monkeypatch.setattr(scraper, "_fetch_static", fetch)
    first = await mgr.create_job(URL, 1, 0)
    await until(lambda: len(clock.waiting) == 1)
    second = await mgr.create_job(URL + "a", 1, 0)
    await until(lambda: len(clock.waiting) == 2)
    independent = await mgr.create_job(OTHER, 1, 0)
    await settle(mgr, independent)
    assert independent.status == "completed" and started == [URL, OTHER]
    assert mgr._sem._value == 4
    assert mgr._governor._hosts["example.com"].semaphore._value == 2
    assert await mgr.cancel(first.job_id)
    assert len(clock.waiting) == 1
    clock.advance(5)
    await settle(mgr, second)
    assert second.status == "completed" and clock.waiting == []
    assert mgr._governor.attempts == 3


@pytest.mark.parametrize("waiting_for", ["host", "global", "delay"])
async def test_cancel_active_and_waiting_reservations(waiting_for):
    clock = Clock(automatic=False)
    gov = governor(clock, CRAWL_HOST_CONCURRENCY=1, CRAWL_MAX_CONCURRENCY=1)
    started = asyncio.Event()

    async def hold(url, delay=None):
        async with gov.reserve(url, delay):
            started.set()
            await asyncio.Event().wait()

    active = asyncio.create_task(hold(URL, 5 if waiting_for == "delay" else None))
    await started.wait()
    waiting = asyncio.create_task(hold(OTHER if waiting_for == "global" else URL))
    await until(lambda: clock.waiting if waiting_for == "delay" else not waiting.done())
    await asyncio.sleep(0)
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    active.cancel()
    await asyncio.gather(active, return_exceptions=True)
    assert gov.attempts == 1  # only the active fetch spent budget
    assert gov._semaphore._value == 1
    assert all(host.semaphore._value == 1 for host in gov._hosts.values())
    assert clock.waiting == []


async def test_cancel_job_releases_active_fetch_and_next_job_can_start(web, monkeypatch):
    clock = Clock(automatic=False)
    mgr = manager(clock, CRAWL_HOST_CONCURRENCY=1)
    started = asyncio.Event()

    async def blocked(url, *_args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scraper, "_fetch_static", blocked)
    first = await mgr.create_job(URL, 1, 0)
    await started.wait()
    assert await mgr.cancel(first.job_id)
    assert mgr._sem._value == 4
    assert mgr._governor._hosts["example.com"].semaphore._value == 1
    monkeypatch.setattr(scraper, "_fetch_static", AsyncMock(return_value=(200, HTML, URL, None)))
    second = await mgr.create_job(URL, 1, 0)
    await settle(mgr, second)
    assert second.status == "completed"
    assert mgr._governor.attempts == 2  # active cancellation is not refunded


@pytest.mark.parametrize("store_kind", ["memory", "redis", "disk"])
async def test_shutdown_during_retry_keeps_frontier_and_restart_resets_governor(
    web, monkeypatch, tmp_path, store_kind
):
    clock = Clock(automatic=False)
    if store_kind == "redis":
        store = RedisJobStore(FakeRedis())
    elif store_kind == "disk":
        store = DiskJobStore(tmp_path / "checkpoints")
    else:
        store = MemoryJobStore()
    mgr = manager(clock, store)
    monkeypatch.setattr(scraper, "_fetch_static", AsyncMock(return_value=(503, HTML, URL, "5")))
    job = await mgr.create_job(URL, 1, 0)
    await until(lambda: clock.waiting)
    assert not job.visited and job.frontier == [(URL, 0)]
    await mgr.shutdown()
    state, _pages = (await store.load_jobs())[0]
    assert state["status"] == "running" and state["frontier"] == [[URL, 0]]
    assert state["visited"] == [] and state["pages_crawled"] == 0
    monkeypatch.setattr(scraper, "_fetch_static", AsyncMock(return_value=(200, HTML, URL, None)))
    restarted = manager(Clock(), store)
    await restarted.restore()
    final = restarted.get_job(job.job_id)
    await settle(restarted, final)
    assert final.status == "completed" and final.pages_crawled == 1
    assert final.visited == {URL} and final.frontier == []
    assert restarted._governor.attempts == 1


async def test_robots_disallow_and_ssrf_denial_never_spend_budget(web, monkeypatch):
    mgr = manager(Clock())
    robots.is_allowed.return_value = False
    first = await mgr.create_job(URL, 1, 0)
    await settle(mgr, first)
    assert first.visited == {URL} and first.pages_crawled == 0
    robots.is_allowed.return_value = True
    monkeypatch.setattr(
        crawler, "validate_and_pin", AsyncMock(side_effect=SSRFValidationError("bad"))
    )
    second = await mgr.create_job(OTHER, 1, 0)
    await settle(mgr, second)
    assert second.visited == {OTHER} and mgr._governor.attempts == 0
    assert web == []


@pytest.mark.parametrize(
    "name",
    [
        "CRAWL_MAX_CONCURRENCY",
        "CRAWL_HOST_CONCURRENCY",
        "CRAWL_HOST_RATE",
        "CRAWL_HOST_BURST",
        "CRAWL_FETCH_BUDGET",
        "CRAWL_PAGE_ATTEMPTS",
        "CRAWL_PAGE_DEADLINE_SECONDS",
        "CRAWL_BACKOFF_SECONDS",
        "CRAWL_BACKOFF_MAX_SECONDS",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "NaN", "inf", "invalid"])
def test_invalid_scheduling_settings_fail_explicitly(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings()


def test_rate_reciprocal_must_be_finite(monkeypatch):
    monkeypatch.setenv("CRAWL_HOST_RATE", "1e-320")
    with pytest.raises(ValueError, match="finite reciprocal"):
        Settings()


async def test_budget_exception_is_not_swallowed_by_scraper(web):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def deny():
        raise CrawlBudgetExceeded("spent")
        yield

    with pytest.raises(CrawlBudgetExceeded, match="spent"):
        await scraper.scrape(URL, fetch_context=deny)
    assert web == []


async def test_fetch_helpers_capture_retry_after_without_changing_public_response(monkeypatch):
    monkeypatch.setattr(
        scraper,
        "safe_fetch",
        AsyncMock(
            return_value=SimpleNamespace(
                status_code=429, text=HTML, final_url=URL, headers={"retry-after": "7"}
            )
        ),
    )
    result = await scraper.scrape(URL, render="static")
    assert result.retry_after == "7"
    assert "retry_after" not in result.to_dict()


async def test_browser_fetch_captures_retry_after_and_releases_context(monkeypatch):
    response = SimpleNamespace(status=429, header_value=AsyncMock(return_value="9"))
    page = SimpleNamespace(
        set_extra_http_headers=AsyncMock(),
        goto=AsyncMock(return_value=response),
        wait_for_load_state=AsyncMock(),
        content=AsyncMock(return_value=HTML),
        url=URL,
    )
    context = object()
    pool = SimpleNamespace(
        acquire=AsyncMock(return_value=context),
        new_page=AsyncMock(return_value=page),
        release=AsyncMock(),
    )
    monkeypatch.setattr(scraper, "get_browser_pool", lambda: pool)
    monkeypatch.setattr(scraper, "install_browser_ssrf_guard", AsyncMock())
    result = await scraper.scrape(URL, render="browser")
    assert result.status_code == 429 and result.retry_after == "9"
    pool.release.assert_awaited_once_with(context)
