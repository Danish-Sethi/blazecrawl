"""Crawl job store backends: memory, Redis (injected fake client), and Redis fallback."""

import json

import pytest

from blazecrawl_core.engine import job_store
from blazecrawl_core.engine.job_store import MemoryJobStore, RedisJobStore, build_job_store
from tests.fakes import FakeRedis

RECORD = {
    "job_id": "job-1",
    "seed": "https://example.com/",
    "status": "completed",
    "pages": [{"url": "https://example.com/", "markdown": "# Example"}],
}


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def info(self, msg, **kwargs):
        pass

    def warning(self, msg, **kwargs):
        self.warnings.append(msg)

    def error(self, msg, **kwargs):
        pass


@pytest.fixture
def log(monkeypatch):
    recorder = RecordingLogger()
    monkeypatch.setattr(job_store, "logger", recorder)
    return recorder


@pytest.fixture
def redis_url(monkeypatch):
    monkeypatch.setattr(job_store.settings, "REDIS_URL", "redis://cache:6379/0")
    monkeypatch.setattr(job_store, "_CONNECT_RETRY_DELAY_SECONDS", 0)


async def test_memory_store_round_trips_records():
    store = MemoryJobStore()
    assert await store.load_all() == []
    await store.save(RECORD)
    assert await store.load_all() == [RECORD]


async def test_memory_store_replaces_snapshot_with_same_job_id():
    store = MemoryJobStore()
    await store.save({**RECORD, "status": "running"})
    await store.save(RECORD)
    assert await store.load_all() == [RECORD]


async def test_memory_store_isolates_snapshots_from_later_mutation():
    store = MemoryJobStore()
    record = {**RECORD, "pages": []}
    await store.save(record)
    record["status"] = "failed"
    assert (await store.load_all())[0]["status"] == "completed"


async def test_redis_store_round_trips_under_prefixed_keys():
    client = FakeRedis()
    store = RedisJobStore(client)
    await store.save(RECORD)
    assert json.loads(client.values["bc:crawl:job:job-1"]) == RECORD
    assert await store.load_all() == [RECORD]


async def test_redis_store_ignores_other_keys_and_skips_corrupt_entries(log):
    client = FakeRedis()
    client.values["bc:scrape:abc"] = json.dumps({"markdown": "cached"})
    client.values["bc:crawl:job:broken"] = "{not json"
    store = RedisJobStore(client)
    await store.save(RECORD)
    assert await store.load_all() == [RECORD]
    assert log.warnings == ["Skipping unreadable crawl job record"]


async def test_redis_store_close_closes_client():
    client = FakeRedis()
    await RedisJobStore(client).close()
    assert client.closed


async def test_build_job_store_without_redis_url_uses_memory(monkeypatch):
    monkeypatch.setattr(job_store.settings, "REDIS_URL", None)
    assert isinstance(await build_job_store(), MemoryJobStore)


async def test_build_job_store_uses_redis_when_reachable(monkeypatch, redis_url):
    client = FakeRedis()
    monkeypatch.setattr(job_store, "_open_redis_client", lambda url: client)
    store = await build_job_store()
    assert isinstance(store, RedisJobStore)
    assert client.ping_calls == 1


async def test_build_job_store_retries_transient_redis_failure(monkeypatch, redis_url):
    client = FakeRedis(fail_pings=1)
    monkeypatch.setattr(job_store, "_open_redis_client", lambda url: client)
    assert isinstance(await build_job_store(), RedisJobStore)
    assert client.ping_calls == 2


async def test_unreachable_redis_falls_back_to_memory_and_warns(monkeypatch, redis_url, log):
    client = FakeRedis(fail_pings=job_store._CONNECT_ATTEMPTS)
    monkeypatch.setattr(job_store, "_open_redis_client", lambda url: client)
    store = await build_job_store()
    assert isinstance(store, MemoryJobStore)
    assert client.ping_calls == job_store._CONNECT_ATTEMPTS
    assert client.closed
    assert log.warnings == ["Redis unavailable; falling back to in-memory crawl job store"]


async def test_missing_redis_package_falls_back_to_memory(monkeypatch, redis_url, log):
    def no_redis_package(url):
        raise ImportError("No module named 'redis'")

    monkeypatch.setattr(job_store, "_open_redis_client", no_redis_package)
    assert isinstance(await build_job_store(), MemoryJobStore)
    assert len(log.warnings) == 1
