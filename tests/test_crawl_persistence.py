"""Crawl job persistence: lifecycle snapshots, restart recovery, and polling after restart."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from blazecrawl_core.api import main
from blazecrawl_core.engine import crawler, job_store
from blazecrawl_core.engine.crawler import CrawlJobState, CrawlManager
from blazecrawl_core.engine.job_store import MemoryJobStore, RedisJobStore
from tests.fakes import FakeRedis

SEED = "https://example.com/"
PAGE = {"url": SEED, "status_code": 200, "title": "Example", "markdown": "# Example"}


async def _fake_crawl(self, job):
    job.pages.append(PAGE)
    job.pages_crawled += 1


async def _blocked_crawl(self, job):
    await asyncio.Event().wait()


async def _settle(manager: CrawlManager, job_id: str) -> None:
    await asyncio.gather(manager._tasks[job_id], return_exceptions=True)


@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setattr(main.settings, "BLAZECRAWL_AUTH_DISABLED", True)
    monkeypatch.setattr(main.settings, "BLAZECRAWL_HOST", "127.0.0.1")
    monkeypatch.setattr(
        main,
        "get_browser_pool",
        lambda: SimpleNamespace(startup=AsyncMock(), shutdown=AsyncMock()),
    )
    monkeypatch.setattr(crawler, "_manager", None)
    monkeypatch.setattr(job_store, "_CONNECT_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(main.settings, "REDIS_URL", None)


async def test_job_snapshots_follow_lifecycle(monkeypatch):
    monkeypatch.setattr(CrawlManager, "_crawl", _fake_crawl)
    store = MemoryJobStore()
    seen: list[str] = []
    original_save = store.save

    async def recording_save(record):
        seen.append(record["status"])
        await original_save(record)

    store.save = recording_save
    manager = CrawlManager(store)
    job = await manager.create_job(SEED, 1, 0)
    await _settle(manager, job.job_id)

    assert seen == ["queued", "running", "completed"]
    stored = (await store.load_all())[0]
    assert stored["status"] == "completed"
    assert stored["pages"] == [PAGE]
    assert stored["finished_at"] is not None


async def test_cancel_is_persisted(monkeypatch):
    monkeypatch.setattr(CrawlManager, "_crawl", _blocked_crawl)
    store = MemoryJobStore()
    manager = CrawlManager(store)
    job = await manager.create_job(SEED, 1, 0)
    await asyncio.sleep(0)  # let the task reach "running"
    assert await manager.cancel(job.job_id)
    await _settle(manager, job.job_id)

    stored = {r["job_id"]: r for r in await store.load_all()}[job.job_id]
    assert stored["status"] == "cancelled"
    assert stored["finished_at"] is not None


async def test_new_manager_loads_persisted_jobs_and_fails_interrupted_ones():
    store = MemoryJobStore()
    await store.save(
        CrawlJobState(
            job_id="finished", seed=SEED, status="completed", pages=[PAGE], pages_crawled=1
        ).to_dict()
    )
    await store.save(CrawlJobState(job_id="queued", seed=SEED).to_dict())
    await store.save(CrawlJobState(job_id="running", seed=SEED, status="running").to_dict())

    restarted = CrawlManager(store)
    await restarted.restore()

    finished = restarted.get_job("finished")
    assert finished.status == "completed"
    assert finished.pages == [PAGE]
    for job_id in ("queued", "running"):
        job = restarted.get_job(job_id)
        assert job.status == "failed"
        assert job.error == "interrupted by restart"
        assert job.finished_at is not None
    stored = {r["job_id"]: r["status"] for r in await store.load_all()}
    assert stored == {"finished": "completed", "queued": "failed", "running": "failed"}


async def test_redis_jobs_survive_manager_restart(monkeypatch):
    monkeypatch.setattr(CrawlManager, "_crawl", _fake_crawl)
    client = FakeRedis()
    first = CrawlManager(RedisJobStore(client))
    job = await first.create_job(SEED, 1, 0)
    await _settle(first, job.job_id)

    restarted = CrawlManager(RedisJobStore(client))
    await restarted.restore()

    restored = restarted.get_job(job.job_id)
    assert restored is not None
    assert restored.to_dict() == first.get_job(job.job_id).to_dict()


async def test_store_write_failure_does_not_stop_crawl(monkeypatch):
    monkeypatch.setattr(CrawlManager, "_crawl", _fake_crawl)
    store = MemoryJobStore()

    async def failing_save(record):
        raise ConnectionError("Redis went away")

    store.save = failing_save
    manager = CrawlManager(store)
    job = await manager.create_job(SEED, 1, 0)
    await _settle(manager, job.job_id)

    assert job.status == "completed"
    assert job.pages_crawled == 1


def test_startup_restores_persisted_jobs_for_polling(api_env, monkeypatch):
    client_store = FakeRedis()
    for job in (
        CrawlJobState(job_id="job-done", seed=SEED, status="completed", pages=[PAGE]),
        CrawlJobState(job_id="job-cut", seed=SEED, status="running"),
    ):
        client_store.values[f"bc:crawl:job:{job.job_id}"] = json.dumps(job.to_dict())
    monkeypatch.setattr(main.settings, "REDIS_URL", "redis://cache:6379/0")
    monkeypatch.setattr(job_store, "_open_redis_client", lambda url: client_store)

    with TestClient(main.app) as client:
        done = client.get("/v1/crawl/job-done")
        cut = client.get("/v1/crawl/job-cut")

    assert done.status_code == 200
    assert done.json()["status"] == "completed"
    assert done.json()["pages"] == [PAGE]
    assert cut.status_code == 200
    assert cut.json()["status"] == "failed"
    assert cut.json()["error"] == "interrupted by restart"
    assert json.loads(client_store.values["bc:crawl:job:job-cut"])["status"] == "failed"
    assert client_store.closed


def test_api_falls_back_to_memory_when_redis_unreachable(api_env, monkeypatch):
    monkeypatch.setattr(main.settings, "REDIS_URL", "redis://cache:6379/0")
    monkeypatch.setattr(
        job_store,
        "_open_redis_client",
        lambda url: FakeRedis(fail_pings=job_store._CONNECT_ATTEMPTS),
    )
    monkeypatch.setattr(CrawlManager, "_crawl", _fake_crawl)
    monkeypatch.setattr(
        "blazecrawl_core.network.ssrf.validate_and_pin", AsyncMock(return_value=None)
    )

    with TestClient(main.app) as client:
        assert isinstance(crawler._manager._store, MemoryJobStore)
        created = client.post("/v1/crawl", json={"url": SEED, "max_pages": 1, "max_depth": 0})
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        body = {}
        for _ in range(100):
            body = client.get(f"/v1/crawl/{job_id}").json()
            if body["status"] == "completed":
                break
            time.sleep(0.02)

    assert body["status"] == "completed"
    assert body["pages_crawled"] == 1
