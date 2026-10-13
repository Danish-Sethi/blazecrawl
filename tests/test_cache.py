"""Cache expiry and bypass without sleeps, browsers or a live Redis server."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from blazecrawl_core.engine import cache

URL = "https://example.com/page"


@pytest.fixture
def memory(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cache.time, "time", lambda: now[0])
    monkeypatch.setattr(cache.settings, "REDIS_URL", "")
    monkeypatch.setattr(cache.settings, "CACHE_DEFAULT_TTL_SECONDS", 10)
    monkeypatch.setattr(cache, "_cache", None)
    return now


async def test_memory_miss_hit_and_expiry(memory):
    assert await cache.get_cached(URL, None, True) is None
    await cache.set_cached(URL, None, True, {"markdown": "hello"}, ttl=5)
    memory[0] = 104.999
    assert await cache.get_cached(URL, ["markdown"], True) == {"markdown": "hello"}
    memory[0] = 105.001
    assert await cache.get_cached(URL, None, True) is None
    assert await cache.get_cached(URL, None, True) is None


async def test_replacement_refreshes_default_ttl_and_payload_is_not_shared(memory):
    payload = {"markdown": "old"}
    await cache.set_cached(URL, None, True, payload)
    payload["markdown"] = "mutated"
    assert await cache.get_cached(URL, None, True) == {"markdown": "old"}
    memory[0] = 109.0
    await cache.set_cached(URL, None, True, {"markdown": "new"})
    memory[0] = 111.0
    assert await cache.get_cached(URL, None, True) == {"markdown": "new"}
    memory[0] = 119.001
    assert await cache.get_cached(URL, None, True) is None


async def test_cache_keys_keep_options_separate(memory):
    await cache.set_cached(URL, ["html", "markdown"], True, {"value": 1}, render="static")
    assert await cache.get_cached(URL, ["markdown", "html"], True, "static") == {"value": 1}
    assert await cache.get_cached(URL, ["markdown"], True, "static") is None
    assert await cache.get_cached(URL, ["html", "markdown"], False, "static") is None
    assert await cache.get_cached(URL, ["html", "markdown"], True, "browser") is None


async def test_use_cache_false_bypasses_read_and_write(memory, monkeypatch):
    from blazecrawl_core.api import main
    from blazecrawl_core.api.schemas import ScrapeRequest

    await cache.set_cached(URL, None, True, {"markdown": "cached"})
    scrape = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"markdown": "fresh"}))
    monkeypatch.setattr(main, "scrape", scrape)
    hit = await main.scrape_endpoint(ScrapeRequest(url=URL))
    assert hit.data == {"markdown": "cached", "cached": True}
    scrape.assert_not_awaited()
    fresh = await main.scrape_endpoint(ScrapeRequest(url=URL, use_cache=False))
    assert fresh.data == {"markdown": "fresh"}
    scrape.assert_awaited_once()
    assert await cache.get_cached(URL, None, True) == {"markdown": "cached"}


async def test_redis_passes_ttl_and_uses_decoded_responses(monkeypatch):
    # Adapter contract only: Redis expiration itself requires an integration test.
    aioredis = pytest.importorskip("redis.asyncio")
    client = SimpleNamespace(get=AsyncMock(return_value='{"value": 1}'), set=AsyncMock())
    factory_calls = []

    def from_url(url, **kwargs):
        factory_calls.append((url, kwargs))
        return client

    monkeypatch.setattr(aioredis, "from_url", from_url)
    backend = cache._RedisCache("redis://localhost:6379/0")
    await backend.set("key", '{"value": 1}', 7)
    assert await backend.get("key") == '{"value": 1}'
    client.set.assert_awaited_once_with("key", '{"value": 1}', ex=7)
    assert factory_calls == [("redis://localhost:6379/0", {"decode_responses": True})]


def test_make_key_varies_full_page_only_for_screenshot_requests():
    shot_viewport = cache.make_key(URL, ["screenshot"], True, "auto", "ua", full_page=False)
    shot_full = cache.make_key(URL, ["screenshot"], True, "auto", "ua", full_page=True)
    assert shot_viewport != shot_full

    md_viewport = cache.make_key(URL, ["markdown"], True, "auto", "ua", full_page=False)
    md_full = cache.make_key(URL, ["markdown"], True, "auto", "ua", full_page=True)
    assert md_viewport == md_full
