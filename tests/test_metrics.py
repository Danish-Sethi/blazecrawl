"""Pool metrics without Chromium, external network access, or a global registry."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from playwright.async_api import Browser, BrowserContext, Page

from blazecrawl_core.config import settings
from blazecrawl_core.engine import browser_pool
from blazecrawl_core.exceptions import BrowserPoolExhaustedError

prometheus = pytest.importorskip("prometheus_client")


@pytest_asyncio.fixture
async def pool(monkeypatch):
    monkeypatch.setattr(settings, "METRICS_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_CONCURRENT_SCRAPES", 2)
    monkeypatch.setattr(browser_pool.BrowserPool, "_instance", None)
    monkeypatch.setattr(browser_pool.BrowserPool, "_lock", None)
    browser = MagicMock(spec=Browser)
    browser.is_connected.return_value = True

    def make_context(**kwargs):
        context = MagicMock(spec=BrowserContext)
        context.browser = browser
        context.pages = []

        async def make_page():
            page = MagicMock(spec=Page)
            context.pages.append(page)
            return page

        context.new_page.side_effect = make_page
        return context

    browser.new_context.side_effect = make_context
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock()
    )
    monkeypatch.setattr(
        browser_pool,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=playwright)),
    )
    proxy = SimpleNamespace(
        start=AsyncMock(return_value="http://127.0.0.1:1234"), close=AsyncMock()
    )
    monkeypatch.setattr(browser_pool, "BrowserEgressProxy", MagicMock(return_value=proxy))
    monkeypatch.setattr(browser_pool, "_guard_new_pages", lambda context: None)
    instance = browser_pool.BrowserPool(pool_size=2)
    monkeypatch.setattr(browser_pool, "_browser_pool", instance)
    yield instance
    await instance.shutdown()


def sample(pool, name):
    return pool.metrics.registry.get_sample_value(f"blazecrawl_browser_pool_{name}")


@pytest.fixture
def client(pool, monkeypatch):
    monkeypatch.setattr(settings, "BLAZECRAWL_API_KEY", "metrics-test-key")
    path = Path(browser_pool.__file__).parents[1] / "api" / "main.py"
    spec = importlib.util.spec_from_file_location("metrics_test_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with TestClient(module.app) as test_client:
        yield test_client


def test_metrics_endpoint_uses_dedicated_registry(client, pool):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == prometheus.CONTENT_TYPE_LATEST
    text = response.text
    assert "blazecrawl_browser_pool_size 2.0" in text
    assert "blazecrawl_browser_pool_active_contexts 0.0" in text
    assert "blazecrawl_browser_pool_errors_total 0.0" in text
    assert "blazecrawl_browser_pool_page_creation_seconds_bucket" in text
    assert "blazecrawl_browser_pool_page_creation_seconds_count 0.0" in text
    assert "python_gc_" not in text
    assert "process_" not in text
    assert "metrics-test-key" not in text
    context = client.portal.call(pool.acquire)
    client.portal.call(pool.new_page, context)
    text = client.get("/metrics").text
    assert "blazecrawl_browser_pool_active_contexts 1.0" in text
    assert "blazecrawl_browser_pool_page_creation_seconds_count 1.0" in text
    client.portal.call(pool.release, context)
    assert "blazecrawl_browser_pool_active_contexts 0.0" in client.get("/metrics").text
    context = client.portal.call(pool.acquire)
    context.new_page.side_effect = RuntimeError("https://private.example/token=secret")
    with pytest.raises(RuntimeError):
        client.portal.call(pool.new_page, context)
    text = client.get("/metrics").text
    assert "blazecrawl_browser_pool_errors_total 1.0" in text
    assert "private.example" not in text
    client.portal.call(pool.release, context)


async def test_pool_lifecycle_and_monotonic_metrics(pool):
    assert sample(pool, "size") == 0
    await pool.startup()
    await pool.startup()
    browser_pool.BrowserEgressProxy.assert_called_once_with(
        upstream_proxy_url=settings.EGRESS_UPSTREAM_PROXY
    )
    assert sample(pool, "size") == 2
    context = await pool.acquire()
    assert sample(pool, "active_contexts") == 1
    await pool.new_page(context)
    assert sample(pool, "page_creation_seconds_count") == 1
    assert sample(pool, "page_creation_seconds_sum") >= 0
    await pool.release(context)
    assert sample(pool, "active_contexts") == 0
    assert pool._available_contexts.qsize() == 2
    await pool.shutdown()
    assert sample(pool, "size") == 0
    assert sample(pool, "active_contexts") == 0
    assert pool._available_contexts.qsize() == 0
    await pool.startup()
    assert sample(pool, "size") == 2
    assert sample(pool, "page_creation_seconds_count") == 1


@pytest.mark.parametrize("stage", ["context", "launch"])
async def test_startup_failure_resets_actual_size_and_retries(pool, stage):
    launch = browser_pool.async_playwright().start.return_value.chromium.launch
    browser = launch.return_value
    make_context = browser.new_context.side_effect
    if stage == "context":
        browser.new_context.side_effect = [make_context(), RuntimeError("context failed")]
    else:
        launch.side_effect = RuntimeError("launch failed")
    with pytest.raises(browser_pool.BrowserPoolError):
        await pool.startup()
    assert sample(pool, "size") == 0
    assert sample(pool, "active_contexts") == 0
    assert sample(pool, "errors_total") == 1
    assert pool._available_contexts.qsize() == 0
    assert pool._is_shutdown
    browser.new_context.side_effect = make_context
    launch.side_effect = None
    await pool.startup()
    assert sample(pool, "size") == 2
    assert sample(pool, "errors_total") == 1


async def test_restart_does_not_requeue_old_leases(pool, monkeypatch):
    await pool.startup()
    old_context = await pool.acquire()
    await pool.new_page(old_context)
    old_context.new_page.side_effect = RuntimeError("page failed")
    with pytest.raises(RuntimeError):
        await pool.new_page(old_context)
    monkeypatch.setattr(browser_pool.asyncio, "sleep", AsyncMock())
    await pool._restart_browser()
    assert sample(pool, "size") == 2
    assert sample(pool, "active_contexts") == 0
    fresh_context = await pool.acquire()
    await pool.release(old_context)
    assert sample(pool, "active_contexts") == 1
    assert pool._available_contexts.qsize() == 1
    await pool.release(fresh_context)
    assert pool._available_contexts.qsize() == 2
    assert sample(pool, "page_creation_seconds_count") == 2
    assert sample(pool, "errors_total") == 1


@pytest.mark.parametrize("stage", ["semaphore", "queue", "health"])
async def test_acquisition_failure_preserves_capacity(pool, monkeypatch, stage):
    await pool.startup()
    contexts = []
    if stage == "semaphore":
        contexts = [await pool.acquire(), await pool.acquire()]
    elif stage == "queue":
        while not pool._available_contexts.empty():
            contexts.append(pool._available_contexts.get_nowait())
    else:
        monkeypatch.setattr(
            pool, "_is_context_healthy", AsyncMock(side_effect=RuntimeError("health failed"))
        )
    with pytest.raises((BrowserPoolExhaustedError, RuntimeError)):
        await pool.acquire(timeout_ms=10)
    assert sample(pool, "errors_total") == 1
    assert sample(pool, "active_contexts") == (2 if stage == "semaphore" else 0)
    assert pool._semaphore._value == (0 if stage == "semaphore" else 2)
    if stage == "health":
        assert pool._available_contexts.qsize() == 2


@pytest.mark.parametrize("stage", ["new_page", "route"])
async def test_page_creation_failure_records_error_and_latency(pool, stage):
    context = await pool.acquire()
    if stage == "new_page":
        context.new_page.side_effect = RuntimeError("https://secret.example/token=private")
    else:
        page = await context.new_page()
        context.new_page.side_effect = None
        context.new_page.return_value = page
        page.route.side_effect = RuntimeError("route failed")
    with pytest.raises(RuntimeError):
        await pool.new_page(context)
    assert sample(pool, "errors_total") == 1
    assert sample(pool, "page_creation_seconds_count") == 1
    assert pool._total_pages_created == 0
    text = prometheus.generate_latest(pool.metrics.registry).decode()
    assert "secret.example" not in text
    assert "private" not in text
    await pool.release(context)
    assert sample(pool, "active_contexts") == 0


async def test_release_failure_recycles_context(pool):
    context = await pool.acquire()
    context.clear_cookies.side_effect = RuntimeError("cookies failed")
    await pool.release(context)
    assert sample(pool, "errors_total") == 1
    assert sample(pool, "size") == 2
    assert sample(pool, "active_contexts") == 0
    assert context not in pool._all_contexts
    assert pool._available_contexts.qsize() == 2


async def test_unhealthy_context_is_replaced_on_acquisition(pool):
    await pool.startup()
    old_context = pool._all_contexts[0]
    old_context.browser = None
    context = await pool.acquire()
    assert old_context not in pool._all_contexts
    assert sample(pool, "errors_total") == 1
    assert sample(pool, "size") == 2
    assert sample(pool, "active_contexts") == 1
    await pool.release(context)
    assert pool._available_contexts.qsize() == 2


async def test_cancelled_startup_cleans_partial_pool(pool):
    browser = browser_pool.async_playwright().start.return_value.chromium.launch.return_value
    create_context = browser.new_context.side_effect
    entered = asyncio.Event()
    first = create_context()

    async def create(**kwargs):
        if not entered.is_set():
            entered.set()
            return first
        await asyncio.Event().wait()

    browser.new_context.side_effect = create
    task = asyncio.create_task(pool.startup())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert sample(pool, "size") == 0
    assert sample(pool, "active_contexts") == 0
    assert sample(pool, "errors_total") == 0
    assert pool._available_contexts.qsize() == 0
    first.close.assert_awaited_once()
    browser.close.assert_awaited_once()


async def test_replacement_failure_reports_smaller_actual_pool(pool, monkeypatch):
    monkeypatch.setattr(pool, "_restart_browser", AsyncMock())
    context = await pool.acquire()
    context.clear_cookies.side_effect = RuntimeError("cookies failed")
    pool._browser.new_context.side_effect = RuntimeError("replacement failed")
    await pool.release(context)
    assert sample(pool, "errors_total") == 2
    assert sample(pool, "size") == 1
    assert sample(pool, "active_contexts") == 0
    assert pool._available_contexts.qsize() == 1


@pytest.mark.parametrize("stage", ["semaphore", "queue", "health", "new_page", "release"])
async def test_cancellation_does_not_leak_leases(pool, monkeypatch, stage):
    await pool.startup()
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def wait(*args):
        entered.set()
        await finish.wait()
        return True

    context = None
    leases = []
    if stage == "semaphore":
        leases = [await pool.acquire(), await pool.acquire()]
        acquire = pool._semaphore.acquire

        async def wait_for_capacity():
            entered.set()
            return await acquire()

        monkeypatch.setattr(pool._semaphore, "acquire", wait_for_capacity)
        operation = pool.acquire()
    elif stage == "queue":
        monkeypatch.setattr(pool._available_contexts, "get", wait)
        operation = pool.acquire()
    elif stage == "health":
        monkeypatch.setattr(pool, "_is_context_healthy", wait)
        operation = pool.acquire()
    else:
        context = await pool.acquire()
        if stage == "new_page":
            context.new_page.side_effect = wait
            operation = pool.new_page(context)
        else:
            context.clear_cookies.side_effect = wait
            operation = pool.release(context)
    task = asyncio.create_task(operation)
    await entered.wait()
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    if stage == "new_page":
        assert sample(pool, "page_creation_seconds_count") == 1
        await pool.release(context)
    for lease in leases:
        await pool.release(lease)
    assert sample(pool, "errors_total") == 0
    assert sample(pool, "active_contexts") == 0
    assert pool._available_contexts.qsize() == 2
    assert pool._semaphore._value == 2
