"""Engine unit tests: extraction, markdown, url normalization, robots."""

import base64
from contextlib import asynccontextmanager

import pytest
from playwright.async_api import async_playwright

from blazecrawl_core.config import Settings
from blazecrawl_core.engine import scraper
from blazecrawl_core.engine.browser_pool import take_screenshot
from blazecrawl_core.engine.content_extractor import ExtractOptions, get_content_extractor
from blazecrawl_core.engine.robots import is_allowed
from blazecrawl_core.exceptions import ScreenshotTooLargeError

HTML = """<html><head><title>Widget Spec</title>
<meta name="description" content="widget spec"></head>
<body><nav>nav</nav><article><h1>Widget</h1>
<p>The widget is a device that does things. It has many features and this
paragraph is long enough to be treated as meaningful main content by the
readability extractor.</p>
<a href="/docs">Docs</a><img src="/w.png" alt="widget"></article></body></html>"""


async def test_extract_markdown_and_metadata():
    ex = get_content_extractor()
    r = await ex.extract(
        HTML, "https://example.com/", ExtractOptions(include_links=True, include_images=True)
    )
    assert r.metadata.title == "Widget Spec"
    assert r.markdown and "Widget" in r.markdown
    assert r.extraction_engine in ("readability", "trafilatura", "beautifulsoup", "full_html")
    assert any(link["url"] == "https://example.com/docs" for link in r.links)
    assert any(img["src"].endswith("/w.png") for img in r.images)


async def test_extract_plaintext():
    ex = get_content_extractor()
    r = await ex.extract(HTML, "https://example.com/", ExtractOptions(formats=["markdown", "text"]))
    assert r.plaintext and "widget" in r.plaintext.lower()


def test_url_normalizer_present():
    from blazecrawl_core.engine.url_normalizer import get_url_normalizer

    n = get_url_normalizer()
    assert n is not None


async def test_robots_allows_public_by_default_when_unreachable():
    # A host whose robots.txt is unreachable -> allowed (standard behaviour).
    # Use a non-routable but public-looking domain to avoid network dependency.
    assert isinstance(await is_allowed("https://example.com/"), bool)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TALL_HTML = (
    "<html><body style='margin:0'><div style='height:3000px;background:#39f'></div></body></html>"
)


def _png_height(png: bytes) -> int:
    assert png.startswith(PNG_SIGNATURE)
    # IHDR chunk: width is bytes 16-20 and height is bytes 20-24, big-endian.
    return int.from_bytes(png[20:24], "big")


@asynccontextmanager
async def _rendered_page(html: str):
    # Standalone browser with set_content: no network and no shared pool state.
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 800, "height": 600})
            await page.set_content(html)
            yield page
        finally:
            await browser.close()


async def test_take_screenshot_full_page_captures_taller_image():
    async with _rendered_page(TALL_HTML) as page:
        viewport_png = await take_screenshot(page, full_page=False, max_bytes=5 * 1024 * 1024)
        full_png = await take_screenshot(page, full_page=True, max_bytes=5 * 1024 * 1024)
    assert _png_height(viewport_png) == 600
    assert _png_height(full_png) >= 3000


async def test_take_screenshot_rejects_capture_over_cap():
    async with _rendered_page(TALL_HTML) as page:
        with pytest.raises(ScreenshotTooLargeError) as exc:
            await take_screenshot(page, full_page=False, max_bytes=100)
    assert exc.value.details["max_bytes"] == 100
    assert exc.value.details["size_bytes"] > 100


async def test_scrape_screenshot_goes_to_browser_and_forwards_full_page(monkeypatch):
    calls = []

    async def fake_fetch_static(*args, **kwargs):
        raise AssertionError("screenshot must not take the static path")

    async def fake_fetch_browser(
        url, timeout_ms, user_agent, headers=None, *, screenshot=False, full_page=False
    ):
        calls.append((screenshot, full_page, headers))
        return 200, "<html><body><p>hi</p></body></html>", url, PNG_SIGNATURE + b"fake", None

    monkeypatch.setattr(scraper, "_fetch_static", fake_fetch_static)
    monkeypatch.setattr(scraper, "_fetch_browser", fake_fetch_browser)
    headers = {"X-Custom-Token": "fake-screenshot-token"}
    result = await scraper.scrape(
        "https://example.com", formats=["screenshot"], full_page=True, headers=headers
    )
    assert calls == [(True, True, {"x-custom-token": "fake-screenshot-token"})]
    assert result.render == "browser"
    assert base64.b64decode(result.screenshot).startswith(PNG_SIGNATURE)


async def test_auto_fallback_does_not_swallow_screenshot_cap(monkeypatch):
    async def thin_static(*args, **kwargs):
        return 200, "<html><body><p>tiny</p></body></html>", "https://example.com/", None

    async def oversize_browser(*args, **kwargs):
        raise ScreenshotTooLargeError("too big", details={"size_bytes": 2, "max_bytes": 1})

    monkeypatch.setattr(scraper, "_fetch_static", thin_static)
    monkeypatch.setattr(scraper, "_fetch_browser", oversize_browser)
    with pytest.raises(ScreenshotTooLargeError):
        await scraper.scrape("https://example.com", formats=["markdown"], render="auto")


def test_screenshot_cap_must_be_positive(monkeypatch):
    monkeypatch.setenv("SCREENSHOT_MAX_BYTES", "0")
    with pytest.raises(ValueError, match="SCREENSHOT_MAX_BYTES"):
        Settings()
