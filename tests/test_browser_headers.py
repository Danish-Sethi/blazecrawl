"""Real Chromium regressions: headers, redirects, resources and route composition."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Error, async_playwright

from blazecrawl_core.engine import scraper
from blazecrawl_core.network import egress
from blazecrawl_core.network.ssrf import PinnedTarget, SSRFValidationError

CUSTOM = {
    "authorization": "Bearer fake-browser-token",
    "CoOkIe": "session=fake-browser-cookie",
    "X-Custom-Token": "fake-browser-token",
    "uSeR-aGeNt": "CustomBrowser/1.0",
    "Accept-Language": "fr-FR",
}


@pytest.fixture
def origins():
    received = []
    servers = []
    urls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.server.server_port, self.path, dict(self.headers)))
            redirects = {
                "/same": urls[0] + "/page",
                "/cross": urls[1] + "/page",
                "/host": urls[0].replace("127.0.0.1", "localhost") + "/page",
                "/chain": urls[0] + "/cross",
                "/blocked": urls[2] + "/private",
            }
            self.send_response(302 if self.path in redirects else 200)
            if self.path in redirects:
                self.send_header("Location", redirects[self.path])
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if self.path == "/page":
                # iframes exercise navigation and subresources on both origins.
                self.wfile.write(
                    (
                        f'<html><body><iframe src="{urls[0]}/resource"></iframe>'
                        f'<iframe src="{urls[1]}/resource"></iframe>'
                        f'<iframe src="{urls[2]}/private"></iframe>'
                        f'<iframe src="{urls[0]}/route-blocked"></iframe>'
                        "<p>Header fixture</p></body></html>"
                    ).encode()
                )
            else:
                self.wfile.write(b"<html><body>resource</body></html>")

        def log_message(self, *args):
            pass

    try:
        for _ in range(3):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            servers.append(server)
            urls.append(f"http://127.0.0.1:{server.server_port}")
            threading.Thread(target=server.serve_forever, daemon=True).start()
        yield urls, received
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize("path", ["/page", "/same", "/cross", "/host", "/chain"])
@pytest.mark.parametrize("screenshot", [False, True])
async def test_real_browser_scopes_headers_on_every_hop_and_resource(
    origins, monkeypatch, path, screenshot
):
    urls, received = origins
    validations = []

    async def fixture_pin(url):
        validations.append(url)
        if url.startswith(urls[2]):
            raise SSRFValidationError("blocked fixture origin")
        parts = urlsplit(url)
        return PinnedTarget(
            url, parts.scheme, parts.hostname, parts.port, "127.0.0.1", ("127.0.0.1",)
        )

    monkeypatch.setattr(egress, "validate_and_pin", fixture_pin)
    async with async_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("Run playwright install chromium for the real-browser header regression")
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        pages = []

        async def new_page(context, **kwargs):
            page = await context.new_page()
            pages.append(page)

            async def prior_guard(route):
                if route.request.url.endswith("/route-blocked"):
                    await route.abort()
                else:
                    await route.fallback()

            await page.route("**/*", prior_guard)
            return page

        release = AsyncMock()
        pool = SimpleNamespace(
            acquire=AsyncMock(return_value=context), new_page=new_page, release=release
        )
        monkeypatch.setattr(scraper, "get_browser_pool", lambda: pool)
        try:
            status, html, final_url, png = await scraper._fetch_browser(
                urls[0] + path,
                10000,
                "BaselineBrowser/1.0",
                CUSTOM,
                screenshot=screenshot,
                full_page=screenshot,
            )
            assert status == 200
            if screenshot:
                assert png.startswith(b"\x89PNG\r\n\x1a\n")
            else:
                assert png is None
            assert "Header fixture" in html
            if path == "/host":
                assert final_url == urls[0].replace("127.0.0.1", "localhost") + "/page"
            else:
                assert final_url == (urls[1] if path in ("/cross", "/chain") else urls[0]) + "/page"
            assert any(
                p == "/resource" and port == servers_port(urls[0]) for port, p, _ in received
            )
            assert any(
                p == "/resource" and port == servers_port(urls[1]) for port, p, _ in received
            )
            assert any(url.startswith(urls[2]) for url in validations)
            assert not any(p in ("/private", "/route-blocked") for _, p, _ in received)
            for port, request_path, raw in received:
                headers = {k.lower(): v for k, v in raw.items()}
                same_origin = port == servers_port(urls[0]) and not (
                    path == "/host" and request_path == "/page"
                )
                # An iframe back on the initial origin regains its scoped headers.
                if same_origin:
                    for name, value in CUSTOM.items():
                        assert headers[name.lower()] == value
                else:
                    for name in ("authorization", "cookie", "x-custom-token"):
                        assert name not in headers
                    assert headers["user-agent"] == "BaselineBrowser/1.0"
                    assert headers.get("accept-language") != "fr-FR"
            release.assert_awaited_once_with(context)
        finally:
            await context.close()
            await browser.close()


def servers_port(url):
    return urlsplit(url).port


async def test_browser_header_scope_rejects_blocked_redirect_before_connection(
    origins, monkeypatch
):
    urls, received = origins

    async def fixture_pin(url):
        if url.startswith(urls[2]):
            raise SSRFValidationError("blocked fixture origin")
        return SimpleNamespace()

    monkeypatch.setattr(egress, "validate_and_pin", fixture_pin)
    async with async_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("Run playwright install chromium for the real-browser header regression")
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await page.set_extra_http_headers({"User-Agent": "BaselineBrowser/1.0"})
            await egress.install_browser_ssrf_guard(
                page, initial_url=urls[0], headers=CUSTOM, user_agent="BaselineBrowser/1.0"
            )
            with pytest.raises(Error, match="ERR_BLOCKED_BY_CLIENT"):
                await page.goto(urls[0] + "/blocked")
            assert [path for _, path, _ in received] == ["/blocked"]
        finally:
            await browser.close()


async def test_browser_header_interception_fails_closed_when_unavailable():
    page = SimpleNamespace(
        route=AsyncMock(),
        context=SimpleNamespace(
            new_cdp_session=AsyncMock(side_effect=RuntimeError("no interception"))
        ),
    )
    with pytest.raises(RuntimeError, match="no interception"):
        await egress.install_browser_ssrf_guard(
            page, initial_url="https://example.test", headers=CUSTOM, user_agent="Baseline/1.0"
        )


@pytest.mark.parametrize(
    ("destination", "same_origin"),
    [
        ("https://EXAMPLE.test:443/end", True),
        ("https://other.test/end", False),
        ("https://example.test:8443/end", False),
        ("http://example.test/end", False),
    ],
)
async def test_browser_header_scope_compares_scheme_host_and_effective_port(
    monkeypatch, destination, same_origin
):
    session = SimpleNamespace(on=Mock(), send=AsyncMock())
    page = SimpleNamespace(
        route=AsyncMock(),
        context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)),
    )
    monkeypatch.setattr(egress, "validate_and_pin", AsyncMock())
    await egress.install_browser_ssrf_guard(
        page, initial_url="https://example.test/start", headers=CUSTOM, user_agent="Baseline/1.0"
    )
    callback = session.on.call_args.args[1]
    await callback(
        {
            "requestId": "fixture-request",
            "request": {
                "url": destination,
                "headers": {**CUSTOM, "Accept": "text/html"},
            },
        }
    )
    command, args = session.send.call_args.args
    assert command == "Fetch.continueRequest"
    forwarded = {item["name"]: item["value"] for item in args["headers"]}
    assert forwarded["accept"] == "text/html"
    if same_origin:
        assert all(forwarded[name.lower()] == value for name, value in CUSTOM.items())
    else:
        assert not any(name in forwarded for name in ("authorization", "cookie", "x-custom-token"))
        assert forwarded["user-agent"] == "Baseline/1.0"
