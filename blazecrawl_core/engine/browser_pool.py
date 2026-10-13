"""Playwright browser pool for BlazeCrawl Core (OSS single-node).

A self-contained pool that preserves the security-relevant behaviour of the
original implementation:

* Chromium is launched with container hardening flags and WebRTC
  non-proxied-UDP disabled (IP-leak mitigation).
* Every context gets the context-level SSRF popup guard so pages a hostile page
  opens itself (``window.open``/``target=_blank``) are still egress-guarded.
* Contexts are recycled on failure and the browser is restarted on crash.

The commercial stealth-fingerprint / residential-proxy / persona subsystems are
intentionally NOT part of the OSS core.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections import deque
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from blazecrawl_core.config import settings
from blazecrawl_core.exceptions import (
    BlazeCrawlError,
    BrowserPoolExhaustedError,
    ScreenshotTooLargeError,
)
from blazecrawl_core.logging import get_logger
from blazecrawl_core.network.browser_proxy import BrowserEgressProxy

logger = get_logger(__name__)

# Container-hardening + WebRTC IP-leak-mitigation launch flags.
_BASE_BROWSER_LAUNCH_ARGS: list[str] = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-first-run",
    # Restrict WebRTC to proxied UDP only; with no proxy this suppresses
    # non-proxied UDP ICE gathering entirely, preventing local/public IP leaks.
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]


def get_browser_launch_args() -> list[str]:
    """Return platform-appropriate Chromium launch flags.

    The Chromium sandbox is intentionally ENABLED. It runs correctly in the
    reference container (verified under `no-new-privileges`, `cap_drop: ALL`,
    and a read-only root filesystem). If a downstream environment cannot create
    user namespaces, Chromium will fail closed at launch rather than render
    untrusted content without isolation.
    """
    return list(_BASE_BROWSER_LAUNCH_ARGS)


# Blocked sub-resource types (bandwidth + speed). Never blocks documents.
DEFAULT_BLOCKED_RESOURCES: set[str] = {"image", "media", "font"}

METRICS_WINDOW_SIZE: int = 100
MAX_RESTART_ATTEMPTS: int = 3

_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Safari/537.36",
]


class BrowserPoolError(BlazeCrawlError):
    """Base exception for browser pool errors."""


class BrowserCrashError(BrowserPoolError):
    """Raised when the browser crashes and cannot be restarted."""


def _guard_new_pages(context: BrowserContext) -> None:
    """Install the SSRF popup guard on every page a context ever creates."""
    from blazecrawl_core.network.egress import install_context_popup_ssrf_guard

    install_context_popup_ssrf_guard(context)


async def take_screenshot(page: Page, *, full_page: bool, max_bytes: int) -> bytes:
    """Capture ``page`` as PNG, rejecting the whole capture if it exceeds ``max_bytes``.

    The size check happens before anything is returned, so an oversized
    capture never leaves this function, not even partially.
    """
    png = await page.screenshot(type="png", full_page=full_page)
    if len(png) > max_bytes:
        raise ScreenshotTooLargeError(
            f"Screenshot is {len(png)} bytes, which exceeds the {max_bytes}-byte limit "
            "(SCREENSHOT_MAX_BYTES).",
            details={"size_bytes": len(png), "max_bytes": max_bytes},
        )
    return png


class BrowserPool:
    """Manages a pool of reusable Playwright browser contexts (singleton)."""

    _instance: BrowserPool | None = None
    _lock: asyncio.Lock | None = None

    def __new__(cls, *args: Any, **kwargs: Any) -> BrowserPool:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, pool_size: int | None = None) -> None:
        if getattr(self, "_initialized", False):
            return
        self.pool_size = pool_size or settings.BROWSER_POOL_SIZE
        self.max_concurrent = settings.MAX_CONCURRENT_SCRAPES
        self._playwright = None
        self._browser: Browser | None = None
        self._egress_proxy: BrowserEgressProxy | None = None
        self._available_contexts: asyncio.Queue[BrowserContext] = asyncio.Queue()
        self._all_contexts: list[BrowserContext] = []
        self._semaphore: asyncio.Semaphore | None = None
        self._pool_lock = asyncio.Lock()
        self._total_pages_created = 0
        self._total_errors = 0
        self._page_creation_times: deque[float] = deque(maxlen=METRICS_WINDOW_SIZE)
        self._active_count = 0
        self._restart_attempts = 0
        self._is_shutdown = True
        self._initialized = True

    @classmethod
    def _cls_lock(cls) -> asyncio.Lock:
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    async def _create_context(self) -> BrowserContext:
        context = await self._browser.new_context(
            viewport={"width": random.randint(1280, 1920), "height": random.randint(720, 1080)},
            user_agent=random.choice(_USER_AGENTS),
            geolocation=None,
            permissions=[],
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
            },
        )
        await context.add_init_script(
            "() => { Object.defineProperty(navigator, 'webdriver', "
            "{ get: () => undefined, configurable: true }); }"
        )
        _guard_new_pages(context)
        return context

    async def startup(self) -> None:
        async with self._cls_lock():
            if self._browser is not None:
                return
            logger.info("Starting browser pool", pool_size=self.pool_size)
            self._is_shutdown = False
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
            try:
                self._playwright = await async_playwright().start()
                self._egress_proxy = BrowserEgressProxy()
                proxy_server = await self._egress_proxy.start()
                self._browser = await self._playwright.chromium.launch(
                    headless=settings.BROWSER_HEADLESS,
                    args=get_browser_launch_args(),
                    proxy={"server": proxy_server, "bypass": ""},
                )
                for _ in range(self.pool_size):
                    context = await self._create_context()
                    self._all_contexts.append(context)
                    await self._available_contexts.put(context)
                logger.info("Browser pool started", contexts=len(self._all_contexts))
            except Exception as e:
                logger.error("Failed to start browser pool", error=str(e))
                raise BrowserPoolError(f"Failed to start browser pool: {e}") from e

    async def shutdown(self) -> None:
        async with self._cls_lock():
            if self._is_shutdown:
                return
            self._is_shutdown = True
            for context in self._all_contexts:
                with contextlib.suppress(Exception):
                    await context.close()
            if self._browser:
                with contextlib.suppress(Exception):
                    await self._browser.close()
            if self._playwright:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
            if self._egress_proxy:
                with contextlib.suppress(Exception):
                    await self._egress_proxy.close()
            self._egress_proxy = None
            self._browser = None
            self._playwright = None
            logger.info("Browser pool shutdown complete")

    async def acquire(self, timeout_ms: int = 30000) -> BrowserContext:
        if self._browser is None or self._is_shutdown:
            await self.startup()
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        timeout_s = timeout_ms / 1000
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout_s)
        except TimeoutError:
            self._total_errors += 1
            raise BrowserPoolExhaustedError(
                "No available browser contexts. Please try again later.",
                details={"pool_size": self.pool_size, "timeout_ms": timeout_ms},
            ) from None
        try:
            context = await asyncio.wait_for(
                self._available_contexts.get(), timeout=min(timeout_s, 5.0)
            )
        except TimeoutError:
            self._semaphore.release()
            self._total_errors += 1
            raise BrowserPoolExhaustedError(
                "No available browser contexts.",
                details={"pool_size": self.pool_size, "timeout_ms": timeout_ms},
            ) from None
        except Exception:
            self._semaphore.release()
            raise
        if not await self._is_context_healthy(context):
            await self._recycle_context(context)
            try:
                context = await asyncio.wait_for(self._available_contexts.get(), timeout=5.0)
            except TimeoutError:
                self._semaphore.release()
                raise BrowserPoolExhaustedError(
                    "No healthy browser contexts available.",
                    details={"pool_size": self.pool_size},
                ) from None
        async with self._pool_lock:
            self._active_count += 1
        return context

    async def release(self, context: BrowserContext) -> None:
        try:
            if not await self._is_context_healthy(context):
                await self._recycle_context(context)
                return
            for page in context.pages:
                with contextlib.suppress(Exception):
                    await page.close()
            try:
                await context.clear_cookies()
            except Exception:
                await self._recycle_context(context)
                return
            await self._available_contexts.put(context)
        except Exception:
            await self._recycle_context(context)
        finally:
            async with self._pool_lock:
                self._active_count = max(0, self._active_count - 1)
            if self._semaphore:
                self._semaphore.release()

    async def _is_context_healthy(self, context: BrowserContext) -> bool:
        try:
            browser = context.browser
            if browser is None or not browser.is_connected():
                return False
            _ = context.pages
            return True
        except Exception:
            return False

    async def _recycle_context(self, old_context: BrowserContext) -> None:
        with contextlib.suppress(Exception):
            await old_context.close()
        if old_context in self._all_contexts:
            self._all_contexts.remove(old_context)
        if self._browser and not self._is_shutdown:
            try:
                new_context = await self._create_context()
                self._all_contexts.append(new_context)
                await self._available_contexts.put(new_context)
            except Exception as e:
                self._total_errors += 1
                logger.error("Failed to create replacement context", error=str(e))
                if self._total_errors % max(1, self.pool_size) == 0:
                    asyncio.create_task(self._restart_browser())

    async def new_page(
        self,
        context: BrowserContext,
        timeout_ms: int = 30000,
        block_resources: bool = True,
        blocked_types: set[str] | None = None,
    ) -> Page:
        start_time = time.perf_counter()
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)
        if block_resources:
            types_to_block = blocked_types or DEFAULT_BLOCKED_RESOURCES

            async def handle_route(route: Any) -> None:
                if route.request.resource_type in types_to_block:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", handle_route)
        self._page_creation_times.append(time.perf_counter() - start_time)
        self._total_pages_created += 1
        return page

    async def health_check(self) -> dict[str, Any]:
        avg = (
            sum(self._page_creation_times) / len(self._page_creation_times)
            if self._page_creation_times
            else 0
        )
        connected = False
        if self._browser is not None and not self._is_shutdown:
            with contextlib.suppress(Exception):
                connected = bool(self._browser.is_connected())
        return {
            "pool_size": self.pool_size,
            "active_contexts": self._active_count,
            "idle_contexts": self._available_contexts.qsize(),
            "total_pages_created": self._total_pages_created,
            "total_errors": self._total_errors,
            "avg_page_creation_time_ms": round(avg * 1000, 2),
            "is_healthy": connected,
            "is_shutdown": self._is_shutdown,
        }

    async def _restart_browser(self) -> None:
        if self._restart_attempts >= MAX_RESTART_ATTEMPTS:
            raise BrowserCrashError(
                "Browser crashed and could not be restarted",
                details={"attempts": self._restart_attempts},
            )
        self._restart_attempts += 1
        await self.shutdown()
        await asyncio.sleep(1)
        await self.startup()
        self._restart_attempts = 0


_browser_pool: BrowserPool | None = None


def get_browser_pool() -> BrowserPool:
    global _browser_pool
    if _browser_pool is None:
        _browser_pool = BrowserPool()
    return _browser_pool
