"""Scrape orchestration for BlazeCrawl Core.

Hybrid strategy: try a fast SSRF-pinned HTTP fetch first (cheap), and fall back
to a headless-browser render when the static fetch yields too little usable
content (JS-heavy pages). Both paths are egress-guarded.

This is deliberately simpler than the commercial hybrid renderer (which adds
managed proxy escalation and ML routing heuristics); it keeps the security
guarantees while staying dependency-light and self-hostable.
"""

from __future__ import annotations

import base64
import contextlib
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from blazecrawl_core.config import settings
from blazecrawl_core.engine.browser_pool import get_browser_pool, take_screenshot
from blazecrawl_core.engine.content_extractor import (
    ExtractedContent,
    ExtractOptions,
    get_content_extractor,
)
from blazecrawl_core.engine.governor import CrawlFetchDenied
from blazecrawl_core.exceptions import ScreenshotTooLargeError
from blazecrawl_core.logging import get_logger
from blazecrawl_core.network.egress import install_browser_ssrf_guard, safe_fetch
from blazecrawl_core.network.headers import validate_headers, validate_user_agent
from blazecrawl_core.network.ssrf import SSRFValidationError

logger = get_logger(__name__)

# If static extraction yields fewer words than this, try the browser path.
_MIN_STATIC_WORDS = 40


@dataclass
class ScrapeResult:
    url: str
    final_url: str
    status_code: int
    markdown: str | None
    html: str | None
    plaintext: str | None
    links: list[dict] | None
    images: list[dict] | None
    metadata: dict
    render: str  # "static" | "browser"
    duration_ms: float
    screenshot: str | None = None  # base64-encoded PNG
    retry_after: str | None = None  # Internal scheduling metadata, not part of the API response.

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "markdown": self.markdown,
            "html": self.html,
            "plaintext": self.plaintext,
            "links": self.links,
            "images": self.images,
            "screenshot": self.screenshot,
            "metadata": self.metadata,
            "render": self.render,
            "duration_ms": round(self.duration_ms, 2),
        }


def _options_from(formats: list[str] | None, only_main_content: bool) -> ExtractOptions:
    fmts = formats or ["markdown"]
    return ExtractOptions(
        only_main_content=only_main_content,
        include_links=("links" in fmts),
        include_images=("images" in fmts),
        include_metadata=True,
        formats=fmts,
    )


def _to_result(
    url: str,
    status: int,
    ext: ExtractedContent,
    render: str,
    t0: float,
    final_url: str,
    screenshot: bytes | None = None,
    *,
    retry_after: str | None = None,
) -> ScrapeResult:
    return ScrapeResult(
        url=url,
        final_url=final_url,
        status_code=status,
        markdown=ext.markdown,
        html=ext.html,
        plaintext=ext.plaintext,
        links=ext.links,
        images=ext.images,
        metadata={
            "title": ext.metadata.title,
            "description": ext.metadata.description,
            "language": ext.metadata.language,
            "author": ext.metadata.author,
            "published_date": ext.metadata.published_date,
            "og_image": ext.metadata.og_image,
            "canonical_url": ext.metadata.canonical_url,
            "favicon": ext.metadata.favicon,
            "extraction_engine": ext.extraction_engine,
        },
        render=render,
        duration_ms=(time.perf_counter() - t0) * 1000,
        retry_after=retry_after,
        screenshot=base64.b64encode(screenshot).decode("ascii") if screenshot else None,
    )


async def _fetch_static(
    url: str, timeout_ms: int, user_agent: str, headers: dict[str, str] | None = None
) -> tuple[int, str, str, str | None]:
    resp = await safe_fetch(
        url,
        timeout_s=timeout_ms / 1000,
        max_redirects=settings.MAX_REDIRECTS,
        max_bytes=settings.MAX_RESPONSE_BYTES,
        user_agent=user_agent,
        headers=headers,
    )
    return resp.status_code, resp.text, resp.final_url, resp.headers.get("retry-after")


async def _fetch_browser(
    url: str,
    timeout_ms: int,
    user_agent: str,
    headers: dict[str, str] | None = None,
    *,
    screenshot: bool = False,
    full_page: bool = False,
) -> tuple[int, str, str, bytes | None, str | None]:
    headers = validate_headers(headers)
    validate_user_agent(user_agent)
    pool = get_browser_pool()
    context = await pool.acquire(timeout_ms=timeout_ms)
    try:
        page = await pool.new_page(context, timeout_ms=timeout_ms)
        await page.set_extra_http_headers({"User-Agent": user_agent})
        await install_browser_ssrf_guard(
            page, initial_url=url, headers=headers, user_agent=user_agent
        )
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        # Allow JS to settle briefly for SPA content.
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
        html = await page.content()
        png = None
        if screenshot:
            png = await take_screenshot(
                page, full_page=full_page, max_bytes=settings.SCREENSHOT_MAX_BYTES
            )
        status = response.status if response else 200
        final_url = page.url
        retry_after = await response.header_value("retry-after") if response else None
        return status, html, final_url, png, retry_after
    finally:
        await pool.release(context)


async def scrape(
    url: str,
    *,
    formats: list[str] | None = None,
    only_main_content: bool = True,
    timeout_ms: int | None = None,
    render: str = "auto",
    user_agent: str = "BlazeCrawl/0.1.0",
    full_page: bool = False,
    headers: dict[str, str] | None = None,
    fetch_context: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> ScrapeResult:
    """Scrape a URL to clean Markdown/HTML/text, egress-guarded.

    Args:
        url: Target URL (http/https only; private/reserved addresses rejected).
        formats: Subset of {"markdown","html","text"/"plaintext","links","images","screenshot"}.
            "screenshot" needs a browser render, so it skips the static fetch and
            is rejected with render="static".
        only_main_content: Strip boilerplate via readability/trafilatura.
        timeout_ms: Request timeout.
        render: "auto" (static w/ browser fallback), "static", or "browser".
        user_agent: Outbound User-Agent header used for static and browser fetches.
        full_page: Capture the full scrollable page for "screenshot" (default: viewport).
        headers: Bounded custom HTTP headers, scoped to the initial origin.
            A case-insensitive User-Agent overrides user_agent on that origin.
        fetch_context: Internal crawler admission hook, once per static/browser page attempt.
    """
    t0 = time.perf_counter()
    headers = validate_headers(headers)
    validate_user_agent(user_agent)
    timeout_ms = timeout_ms or settings.DEFAULT_TIMEOUT_MS
    extractor = get_content_extractor()
    opts = _options_from(formats, only_main_content)

    async def fetch(browser: bool = False) -> tuple[int, str, str, bytes | None, str | None]:
        context = fetch_context() if fetch_context else contextlib.nullcontext()
        async with context:
            if browser:
                return await _fetch_browser(
                    url,
                    timeout_ms,
                    user_agent,
                    headers,
                    screenshot=want_screenshot,
                    full_page=full_page,
                )
            status, html, final_url, retry_after = await _fetch_static(
                url, timeout_ms, user_agent, headers
            )
            return status, html, final_url, None, retry_after

    if render not in ("auto", "static", "browser"):
        raise ValueError("render must be one of auto|static|browser")
    want_screenshot = "screenshot" in (formats or [])
    if want_screenshot and render == "static":
        raise ValueError(
            "formats 'screenshot' requires browser rendering; use render auto or browser"
        )

    # Browser-only path. Screenshots always need a rendered page, so they go
    # straight to the browser even when render is "auto".
    if render == "browser" or want_screenshot:
        status, html, final_url, png, retry_after = await fetch(browser=True)
        ext = await extractor.extract(html, final_url, opts)
        return _to_result(url, status, ext, "browser", t0, final_url, png, retry_after=retry_after)

    # Static path (with auto fallback to browser).
    try:
        status, html, final_url, _, retry_after = await fetch()
    except (SSRFValidationError, CrawlFetchDenied):
        raise
    except Exception as e:
        if render == "static":
            raise
        logger.info("Static fetch failed, falling back to browser", url=url, error=str(e))
        status, html, final_url, _, retry_after = await fetch(browser=True)
        ext = await extractor.extract(html, final_url, opts)
        return _to_result(url, status, ext, "browser", t0, final_url, retry_after=retry_after)

    ext = await extractor.extract(html, final_url, opts)

    # Auto fallback: too little usable content from static fetch.
    if render == "auto":
        words = len((ext.markdown or "").split())
        if words < _MIN_STATIC_WORDS and status == 200:
            logger.info("Static content thin; retrying with browser", url=url, words=words)
            try:
                bstatus, bhtml, bfinal, _, bretry_after = await fetch(browser=True)
                if fetch_context and (bstatus == 429 or 500 <= bstatus <= 599):
                    return _to_result(
                        url, bstatus, ext, "browser", t0, bfinal, retry_after=bretry_after
                    )
                bext = await extractor.extract(bhtml, bfinal, opts)
                if len((bext.markdown or "").split()) > words:
                    return _to_result(
                        url, bstatus, bext, "browser", t0, bfinal, retry_after=bretry_after
                    )
            except (SSRFValidationError, CrawlFetchDenied, ScreenshotTooLargeError):
                raise
            except Exception as e:
                logger.warning("Browser fallback failed; using static result", error=str(e))

    return _to_result(url, status, ext, "static", t0, final_url, retry_after=retry_after)
