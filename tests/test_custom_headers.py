"""Header validation, cache compatibility and pinned outbound forwarding."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blazecrawl_core.api.schemas import ScrapeRequest
from blazecrawl_core.engine import cache, scraper
from blazecrawl_core.network import egress
from blazecrawl_core.network.headers import (
    MAX_HEADER_BYTES,
    MAX_HEADER_COUNT,
    validate_headers,
)
from blazecrawl_core.network.ssrf import PinnedTarget, SSRFValidationError

URL = "https://example.test/page"
CUSTOM = {
    "aUtHoRiZaTiOn": "Bearer fake-demo-token",
    "cookie": "session=fake-demo-cookie",
    "X-Custom-Token": "fake-demo-token",
    "uSeR-aGeNt": "CustomBot/1.0",
    "AcCePt-LaNgUaGe": "fr-FR",
}
INVALID = [
    {"Host": "evil.test"},
    {"content-LENGTH": "12"},
    {"Transfer-Encoding": "chunked"},
    {"CONNECTION": "x-token"},
    {"Keep-Alive": "timeout=5"},
    {"proxy-connection": "keep-alive"},
    {"Proxy-Authorization": "fake"},
    {"Proxy-Authenticate": "fake"},
    {"TE": "trailers"},
    {"Trailer": "x-token"},
    {"Upgrade": "websocket"},
    {"HTTP2-Settings": "fake"},
    {"Expect": "100-continue"},
    {"Bad Name": "value"},
    {"": "value"},
    {"Bad:Name": "value"},
    {"Bad\r\nName": "value"},
    {"X-Token": "value\r\nInjected: bad"},
    {"X-Token": "value\n"},
    {"X-Token": "nul\x00"},
    {"X-Token": "tab\t"},
    {"X-Token": "del\x7f"},
    {"X-Token": "control\x85"},
    {"X-Token": "\u2028"},
    {"X-Token": "x" * 4097},
    {"X" * 129: "value"},
    {"X-Token": "one", "x-token": "two"},
    {"User-Agent": ""},
    {"user-agent": "x" * 201},
    {f"X-{i}": "value" for i in range(MAX_HEADER_COUNT + 1)},
    {f"X-{i}": "x" * 3000 for i in range(3)},
]


@pytest.mark.parametrize("headers", INVALID)
async def test_api_and_engine_reject_invalid_headers_before_io(headers, monkeypatch):
    with pytest.raises(ValidationError):
        ScrapeRequest(url=URL, headers=headers)
    fetch = AsyncMock()
    monkeypatch.setattr(scraper, "_fetch_static", fetch)
    monkeypatch.setattr(scraper, "_fetch_browser", fetch)
    for render in ("static", "browser", "auto"):
        with pytest.raises(ValueError):
            await scraper.scrape(URL, headers=headers, render=render)
    fetch.assert_not_awaited()


@pytest.mark.parametrize("value", ["bad\r\nInjected: yes", "bad\x00", "x" * 201, ""])
async def test_invalid_baseline_user_agent_rejected(value):
    with pytest.raises(ValidationError):
        ScrapeRequest(url=URL, user_agent=value)
    with pytest.raises(ValueError):
        await scraper.scrape(URL, user_agent=value)


def test_validation_bounds_and_case_normalization():
    assert validate_headers(CUSTOM) == {k.lower(): v for k, v in CUSTOM.items()}
    assert validate_headers({"!#$%&'*+-.^_`|~": ""})
    assert (
        len(validate_headers({f"X-{i}": "" for i in range(MAX_HEADER_COUNT)})) == MAX_HEADER_COUNT
    )
    at_limit = {"X-A": "a" * 4096, "X-B": "b" * (MAX_HEADER_BYTES - 4096 - 14)}
    assert validate_headers(at_limit) == {k.lower(): v for k, v in at_limit.items()}
    with pytest.raises(ValueError, match="8192"):
        validate_headers({**at_limit, "X-C": ""})
    for invalid in ([], {"X": 1}, {1: "value"}):
        with pytest.raises(ValueError):
            validate_headers(invalid)


def test_legacy_keys_and_canonical_header_order():
    base = f"{URL}|{['markdown']}|1|auto|BlazeCrawl/0.1.0"
    legacy = "bc:scrape:" + hashlib.sha256(base.encode()).hexdigest()
    assert cache.make_key(URL, None, True) == legacy
    assert cache.make_key(URL, None, True, headers={}) == legacy
    first = cache.make_key(URL, None, True, headers=CUSTOM)
    reordered = {k.lower(): v for k, v in reversed(list(CUSTOM.items()))}
    assert cache.make_key(URL, None, True, headers=reordered) == first
    assert cache.make_key(URL, None, True, headers={"X-Custom-Token": "other"}) != first
    assert cache.make_key(URL, None, True, user_agent="Other/1.0", headers=CUSTOM) != first
    assert first != legacy
    assert "fake-demo-token" not in first


async def test_api_cache_keeps_authenticated_results_isolated(monkeypatch):
    from blazecrawl_core.api import main

    monkeypatch.setattr(cache, "_cache", cache._MemoryCache())
    fetch = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"markdown": "private"}))
    monkeypatch.setattr(main, "scrape", fetch)
    first = await main.scrape_endpoint(ScrapeRequest(url=URL, headers=CUSTOM))
    reordered = {k.lower(): v for k, v in reversed(list(CUSTOM.items()))}
    hit = await main.scrape_endpoint(ScrapeRequest(url=URL, headers=reordered))
    assert first.data == {"markdown": "private"}
    assert hit.data == {"markdown": "private", "cached": True}
    assert fetch.await_count == 1
    assert fetch.call_args.kwargs["headers"] == {k.lower(): v for k, v in CUSTOM.items()}
    await main.scrape_endpoint(ScrapeRequest(url=URL))
    await main.scrape_endpoint(ScrapeRequest(url=URL, headers={"X-Custom-Token": "other"}))
    assert fetch.await_count == 3


def test_invalid_headers_return_422(monkeypatch):
    from blazecrawl_core.api import main

    fetch = AsyncMock()
    monkeypatch.setattr(main, "scrape", fetch)
    monkeypatch.setattr(main.settings, "BLAZECRAWL_API_KEY", "fake-local-api-key")
    # No lifespan/browser startup is needed for request validation.
    client = TestClient(main.app)
    try:
        response = client.post(
            "/v1/scrape",
            json={"url": URL, "headers": {"Host": "evil.test"}},
            headers={"Authorization": "Bearer fake-local-api-key"},
        )
    finally:
        client.close()
    assert response.status_code == 422
    fetch.assert_not_awaited()


@pytest.fixture
def outbound(monkeypatch):
    captured = []
    responses = []
    validated = []
    real_client = httpx.AsyncClient

    async def pin(url):
        validated.append(url)
        parts = urlsplit(url)
        if parts.hostname == "private.test":
            raise SSRFValidationError("blocked test target")
        return PinnedTarget(
            url=url,
            scheme=parts.scheme,
            hostname=parts.hostname,
            port=parts.port or (443 if parts.scheme == "https" else 80),
            ip="8.8.8.8",
            all_ips=("8.8.8.8",),
        )

    def handler(request):
        captured.append(request)
        return responses.pop(0)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(egress, "validate_and_pin", pin)
    monkeypatch.setattr(egress, "validate_proxy_endpoint", AsyncMock())
    monkeypatch.setattr(egress, "_pinned_transport", lambda *a, **kw: transport)

    def client(**kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(egress.httpx, "AsyncClient", client)
    return captured, responses, validated


@pytest.mark.parametrize("proxy", [False, True])
@pytest.mark.parametrize(
    ("start", "destination", "same_origin"),
    [
        (URL, "https://other.test/end", False),
        (URL, "https://example.test:8443/end", False),
        ("http://example.test/start", "https://example.test/end", False),
        (URL, "https://EXAMPLE.test:443/end", True),
        ("http://example.test/start", "http://EXAMPLE.test:80/end", True),
        (URL, "/end", True),
    ],
)
async def test_pinned_direct_and_proxy_redirect_header_scope(
    outbound, proxy, start, destination, same_origin
):
    captured, responses, validated = outbound
    responses.extend(
        [httpx.Response(302, headers={"Location": destination}), httpx.Response(200, text="ok")]
    )
    client = (
        egress.PinnedProxyClient("http://proxy.test", user_agent="Baseline/1.0")
        if proxy
        else egress.PinnedAsyncClient(user_agent="Baseline/1.0")
    )
    result = await client.get(start, headers=CUSTOM)
    assert result.status_code == 200
    assert len(validated) == 2
    for name, value in CUSTOM.items():
        assert captured[0].headers[name] == value
        if same_origin:
            assert captured[1].headers[name] == value
        elif name.lower() == "user-agent":
            assert captured[1].headers[name] == "Baseline/1.0"
        else:
            assert name not in captured[1].headers
    assert len(captured[0].headers.get_list("user-agent")) == 1
    assert captured[0].headers["host"] == "example.test"
    assert CUSTOM["uSeR-aGeNt"] == "CustomBot/1.0"


@pytest.mark.parametrize("proxy", [False, True])
async def test_headers_stay_dropped_if_static_redirect_returns_to_initial_origin(outbound, proxy):
    captured, responses, _ = outbound
    responses.extend(
        [
            httpx.Response(302, headers={"Location": "https://other.test/"}),
            httpx.Response(302, headers={"Location": URL}),
            httpx.Response(200),
        ]
    )
    client = (
        egress.PinnedProxyClient("http://proxy.test", user_agent="Baseline/1.0")
        if proxy
        else egress.PinnedAsyncClient(user_agent="Baseline/1.0")
    )
    await client.get(URL, headers=CUSTOM)
    for request in captured[1:]:
        assert request.headers["user-agent"] == "Baseline/1.0"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "x-custom-token" not in request.headers


@pytest.mark.parametrize("proxy", [False, True])
async def test_egress_rejects_managed_headers_before_validation(outbound, proxy):
    captured, _, validated = outbound
    client = egress.PinnedProxyClient("http://proxy.test") if proxy else egress.PinnedAsyncClient()
    with pytest.raises(ValueError, match="managed"):
        await client.get(URL, headers={"hOsT": "evil.test"})
    assert captured == validated == []


@pytest.mark.parametrize("proxy", [False, True])
async def test_scoped_headers_do_not_bypass_redirect_ssrf_checks(outbound, proxy):
    captured, responses, validated = outbound
    responses.append(httpx.Response(302, headers={"Location": "https://private.test/"}))
    client = egress.PinnedProxyClient("http://proxy.test") if proxy else egress.PinnedAsyncClient()
    with pytest.raises(SSRFValidationError, match="blocked test target"):
        await client.get(URL, headers=CUSTOM)
    assert len(captured) == 1
    assert validated[-1] == "https://private.test/"


async def test_static_scraper_forwards_validated_headers(outbound):
    captured, responses, _ = outbound
    responses.append(httpx.Response(200, text="<html><body><p>Header demo</p></body></html>"))
    result = await scraper.scrape(URL, render="static", headers=CUSTOM)
    assert result.render == "static"
    assert captured[0].headers["user-agent"] == "CustomBot/1.0"
    assert captured[0].headers["x-custom-token"] == "fake-demo-token"


async def test_auto_fallback_keeps_header_policy(monkeypatch):
    html = "<html><body><article><p>Header demo</p></article></body></html>"
    static = AsyncMock(side_effect=OSError("fixture failure"))
    browser = AsyncMock(return_value=(200, html, URL))
    monkeypatch.setattr(scraper, "_fetch_static", static)
    monkeypatch.setattr(scraper, "_fetch_browser", browser)
    result = await scraper.scrape(URL, headers=CUSTOM)
    assert result.render == "browser"
    assert static.call_args.args[3] == browser.call_args.args[3] == validate_headers(CUSTOM)
