"""API journey tests using FastAPI TestClient (auth-disabled loopback mode)."""

import base64
import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("BLAZECRAWL_AUTH_DISABLED", "true")
os.environ.setdefault("BLAZECRAWL_HOST", "127.0.0.1")

from fastapi.testclient import TestClient  # noqa: E402

from blazecrawl_core import __version__  # noqa: E402
from blazecrawl_core.api.main import app  # noqa: E402
from blazecrawl_core.config import settings  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # settings is built at import time, so the env default above can lose to an earlier import.
    # Pin auth-disabled on the settings object itself so these tests don't depend on import order.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "BLAZECRAWL_AUTH_DISABLED", True)
        with TestClient(app) as c:
            yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert data["status"] == "ok"
    assert isinstance(data["status"], str)
    assert data["version"] == __version__
    assert isinstance(data["version"], str)
    assert data["mode"] == "oss"
    assert isinstance(data["mode"], str)


def test_ready(client):
    r = client.get("/ready")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert data["ready"] is True
    assert isinstance(data["ready"], bool)
    assert isinstance(data["cache"], str)
    assert data["cache"] in ("memory", "redis")
    assert isinstance(data["queue"], str)
    assert bool(data["queue"])

    browser = data["browser"]
    assert isinstance(browser, dict)
    assert isinstance(browser["pool_size"], int)
    assert isinstance(browser["active_contexts"], int)
    assert isinstance(browser["idle_contexts"], int)
    assert isinstance(browser["total_pages_created"], int)
    assert isinstance(browser["total_errors"], int)
    assert isinstance(browser["avg_page_creation_time_ms"], (int, float))
    assert isinstance(browser["is_healthy"], bool)
    assert isinstance(browser["is_shutdown"], bool)


def test_scrape_requires_url(client):
    r = client.post("/v1/scrape", json={})
    assert r.status_code == 422


def test_scrape_blocks_ssrf(client):
    for bad in ["http://127.0.0.1/", "http://169.254.169.254/", "http://[::1]/"]:
        r = client.post("/v1/scrape", json={"url": bad})
        assert r.status_code == 400, bad
        assert r.json()["detail"]["error"] == "url_rejected"


def test_scrape_example_com(client):
    r = client.post("/v1/scrape", json={"url": "https://example.com"})
    assert r.status_code == 200
    d = r.json()
    assert d["success"] is True
    assert "documentation examples" in (d["data"]["markdown"] or "")


def test_map_example_com(client):
    r = client.post("/v1/map", json={"url": "https://example.com"})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert isinstance(r.json()["urls"], list)


def test_crawl_lifecycle(client, monkeypatch):
    # Deterministic: the crawl runs against a fixed page result instead of live DNS, robots.txt or
    # example.com. Real egress behaviour stays covered by the SSRF tests in this file and tests/security.
    from blazecrawl_core.engine import crawler, robots
    from blazecrawl_core.engine.scraper import ScrapeResult
    from blazecrawl_core.network import ssrf

    page = ScrapeResult(
        url="https://example.com",
        final_url="https://example.com/",
        status_code=200,
        markdown="# Example Domain",
        html=None,
        plaintext=None,
        links=[],
        images=None,
        metadata={"title": "Example Domain"},
        render="static",
        duration_ms=1.0,
    )
    monkeypatch.setattr(crawler, "scrape", AsyncMock(return_value=page))
    monkeypatch.setattr(robots, "is_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(ssrf, "validate_and_pin", AsyncMock(return_value=None))

    r = client.post(
        "/v1/crawl", json={"url": "https://example.com", "max_pages": 1, "max_depth": 0}
    )
    assert r.status_code == 202
    jid = r.json()["job_id"]
    import time

    for _ in range(40):
        g = client.get(f"/v1/crawl/{jid}")
        assert g.status_code == 200
        if g.json()["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.3)
    assert g.json()["status"] == "completed"
    assert g.json()["pages"][0]["markdown"] == "# Example Domain"


def test_crawl_blocks_ssrf(client):
    r = client.post("/v1/crawl", json={"url": "http://192.168.0.1/"})
    assert r.status_code == 400


def test_crawl_not_found(client):
    assert client.get("/v1/crawl/does-not-exist").status_code == 404


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def test_scrape_screenshot_returns_base64_png(client):
    r = client.post(
        "/v1/scrape",
        json={"url": "https://example.com", "formats": ["screenshot"], "use_cache": False},
    )
    assert r.status_code == 200
    png = base64.b64decode(r.json()["data"]["screenshot"], validate=True)
    assert png.startswith(PNG_SIGNATURE)


def test_scrape_screenshot_over_cap_returns_413_without_data(client, monkeypatch):
    monkeypatch.setattr(settings, "SCREENSHOT_MAX_BYTES", 1024)
    r = client.post(
        "/v1/scrape",
        json={"url": "https://example.com", "formats": ["screenshot"], "use_cache": False},
    )
    assert r.status_code == 413
    body = r.json()
    assert "data" not in body
    assert body["detail"]["error"] == "screenshot_too_large"
    assert body["detail"]["max_bytes"] == 1024


def test_cached_screenshot_over_lowered_cap_returns_413(client, monkeypatch):
    body = {"url": "https://example.com", "formats": ["screenshot"]}
    first = client.post("/v1/scrape", json=body)
    assert first.status_code == 200
    monkeypatch.setattr(settings, "SCREENSHOT_MAX_BYTES", 1024)
    r = client.post("/v1/scrape", json=body)
    assert r.status_code == 413
    assert "data" not in r.json()
    assert r.json()["detail"]["error"] == "screenshot_too_large"


def test_scrape_screenshot_rejects_static_render(client):
    r = client.post(
        "/v1/scrape",
        json={"url": "https://example.com", "formats": ["screenshot"], "render": "static"},
    )
    assert r.status_code == 422
