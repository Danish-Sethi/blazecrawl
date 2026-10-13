"""Crawl checkpoints and resume: lifecycle, shutdown/restart, invalid records, and polling after restart."""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from blazecrawl_core.api import main
from blazecrawl_core.engine import crawler, job_store, robots
from blazecrawl_core.engine.crawler import CrawlJobState, CrawlManager
from blazecrawl_core.engine.job_store import DiskJobStore, MemoryJobStore, RedisJobStore
from blazecrawl_core.network import ssrf
from tests.fakes import FakeRedis

SEED = "https://example.com/"
A = "https://example.com/a"
B = "https://example.com/b"
C = "https://example.com/c"
D = "https://example.com/d"
GRAPH = {SEED: [A, B], A: [C], B: [D], C: [], D: []}
ALL_PAGES = [SEED, A, B, C, D]
PAGE = {"url": SEED, "status_code": 200, "title": "Home", "markdown": "# Home"}


class FakeWeb:
    """Serves GRAPH. URLs in ``blocked`` stay in flight until the crawl is cancelled."""

    def __init__(self) -> None:
        self.blocked: set[str] = set()
        self.started: list[str] = []
        self.completed: list[str] = []

    async def scrape(self, url, **_kwargs):
        self.started.append(url)
        if url in self.blocked:
            await asyncio.Event().wait()
        self.completed.append(url)
        return SimpleNamespace(
            final_url=url,
            status_code=200,
            metadata={"title": url},
            markdown=f"# {url}",
            links=[{"url": link} for link in GRAPH[url]],
        )


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
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(crawler, "scrape", fake.scrape)
    monkeypatch.setattr(crawler, "validate_and_pin", AsyncMock(return_value=None))
    monkeypatch.setattr(ssrf, "validate_and_pin", AsyncMock(return_value=None))

    async def allowed(url, user_agent=robots.DEFAULT_USER_AGENT):
        return True

    monkeypatch.setattr(crawler.robots, "is_allowed", allowed)
    return fake


@pytest.fixture(params=["memory", "redis", "disk"])
def store_factory(request, tmp_path):
    """Return a callable that opens a store over the same backing data (a simulated restart)."""
    if request.param == "memory":
        shared = MemoryJobStore()
        return lambda: shared
    if request.param == "redis":
        client = FakeRedis()
        return lambda: RedisJobStore(client)
    root = tmp_path / "checkpoints"
    return lambda: DiskJobStore(root)


@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setattr(main.settings, "BLAZECRAWL_AUTH_DISABLED", True)
    monkeypatch.setattr(main.settings, "BLAZECRAWL_HOST", "127.0.0.1")
    monkeypatch.setattr(main.settings, "REDIS_URL", None)
    monkeypatch.setattr(main.settings, "CRAWL_CHECKPOINTS_ENABLED", False)
    monkeypatch.setattr(
        main,
        "get_browser_pool",
        lambda: SimpleNamespace(startup=AsyncMock(), shutdown=AsyncMock()),
    )
    monkeypatch.setattr(crawler, "_manager", None)
    monkeypatch.setattr(job_store, "_CONNECT_RETRY_DELAY_SECONDS", 0)


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.005)


async def _settle(manager: CrawlManager, job_id: str) -> None:
    await asyncio.gather(manager._tasks[job_id], return_exceptions=True)


async def _stored(store) -> dict:
    return {state["job_id"]: (state, pages) for state, pages in await store.load_jobs()}


def _poll(client: TestClient, job_id: str, done) -> dict:
    body: dict = {}
    for _ in range(500):
        body = client.get(f"/v1/crawl/{job_id}").json()
        if done(body):
            return body
        time.sleep(0.01)
    raise AssertionError(f"job did not reach the expected state: {body}")


async def test_checkpoints_follow_lifecycle(web):
    store = MemoryJobStore()
    statuses: list[str] = []
    commit = store.commit

    async def recording_commit(job_id, state, new_pages):
        statuses.append(state["status"])
        await commit(job_id, state, new_pages)

    store.commit = recording_commit
    manager = CrawlManager(store)
    job = await manager.create_job(SEED, 10, 2)
    await _settle(manager, job.job_id)

    assert statuses[:2] == ["queued", "running"]
    assert statuses[-1] == "completed"
    assert web.completed == ALL_PAGES
    state, pages = (await _stored(store))[job.job_id]
    assert state["status"] == "completed"
    assert len(pages) == len(ALL_PAGES)


async def test_shutdown_suspends_and_restart_resumes_without_refetching(store_factory, web):
    web.blocked = {B}
    store = store_factory()
    manager = CrawlManager(store)
    job = await manager.create_job(SEED, 10, 2)
    await _until(lambda: len(web.completed) == 2)
    await manager.shutdown()

    state, pages = (await _stored(store))[job.job_id]
    assert state["status"] == "running"
    assert state["pages_crawled"] == 2
    assert state["frontier"] == [[B, 1], [C, 2]]

    web.blocked = set()
    restarted = CrawlManager(store_factory())
    await restarted.restore()
    await _settle(restarted, job.job_id)

    final = restarted.get_job(job.job_id)
    assert final.status == "completed"
    assert [page["url"] for page in final.pages] == ALL_PAGES
    assert web.completed == ALL_PAGES  # no page completed twice
    assert web.started == [SEED, A, B, B, C, D]  # only the in-flight page was fetched again


async def test_shutdown_joins_crawl_tasks_before_closing_store(web):
    web.blocked = {B}
    store = MemoryJobStore()
    manager = CrawlManager(store)
    job = await manager.create_job(SEED, 10, 2)
    await _until(lambda: len(web.completed) == 2)
    task = manager._tasks[job.job_id]

    closed_while_task_running: list[bool] = []
    close = store.close

    async def checked_close():
        closed_while_task_running.append(not task.done())
        await close()

    store.close = checked_close
    await manager.shutdown()

    assert closed_while_task_running == [False]
    assert task.done()
    state, _ = (await _stored(store))[job.job_id]
    assert state["status"] == "running"


async def test_user_cancel_stays_cancelled_after_restart(store_factory, web):
    web.blocked = {B}
    store = store_factory()
    manager = CrawlManager(store)
    job = await manager.create_job(SEED, 10, 2)
    await _until(lambda: len(web.completed) == 2)
    assert await manager.cancel(job.job_id)
    await _settle(manager, job.job_id)
    state, _ = (await _stored(store))[job.job_id]
    assert state["status"] == "cancelled"

    restarted = CrawlManager(store_factory())
    await restarted.restore()
    assert restarted.get_job(job.job_id).status == "cancelled"
    assert job.job_id not in restarted._tasks
    started = list(web.started)
    await asyncio.sleep(0.02)
    assert web.started == started


async def test_cancel_before_crawl_starts_is_not_resumed(store_factory, web):
    store = store_factory()
    manager = CrawlManager(store)
    job = await manager.create_job(SEED, 10, 2)
    # The task has been created but has not run yet.
    assert await manager.cancel(job.job_id)

    state, _ = (await _stored(store))[job.job_id]
    assert state["status"] == "cancelled"

    restarted = CrawlManager(store_factory())
    await restarted.restore()
    assert restarted._tasks == {}
    assert web.started == []


async def test_completed_job_is_not_resumed(store_factory, web):
    store = store_factory()
    manager = CrawlManager(store)
    job = await manager.create_job(SEED, 10, 2)
    await _settle(manager, job.job_id)
    started = list(web.started)

    restarted = CrawlManager(store_factory())
    await restarted.restore()
    assert restarted._tasks == {}
    assert restarted.get_job(job.job_id).status == "completed"
    assert web.started == started


async def test_queued_job_is_resumed_on_restart(store_factory, web):
    store = store_factory()
    queued = CrawlJobState(job_id="queued-1", seed=SEED, max_pages=10, max_depth=2)
    await store.commit(queued.job_id, queued.to_state(), {})

    manager = CrawlManager(store_factory())
    await manager.restore()
    await _settle(manager, "queued-1")
    assert manager.get_job("queued-1").status == "completed"
    assert web.completed == ALL_PAGES


async def test_page_records_past_pages_crawled_are_ignored(web):
    store = MemoryJobStore()
    done = CrawlJobState(
        job_id="orphans", seed=SEED, status="completed", pages=[PAGE], pages_crawled=1
    )
    orphan = {"url": A, "markdown": "written before the state commit failed"}
    await store.commit(done.job_id, done.to_state(), {0: PAGE, 1: orphan})

    manager = CrawlManager(store)
    await manager.restore()
    restored = manager.get_job("orphans")
    assert restored.pages == [PAGE]
    assert restored.pages_crawled == 1


async def test_invalid_records_are_skipped_and_valid_jobs_still_load(monkeypatch):
    logs = RecordingLogger()
    monkeypatch.setattr(crawler, "logger", logs)
    client = FakeRedis()
    valid = CrawlJobState(
        job_id="good", seed=SEED, status="completed", pages=[PAGE], pages_crawled=1, finished_at=2.0
    ).to_state()
    client.values["bc:crawl:job:good"] = json.dumps(valid)
    client.hashes["bc:crawl:pages:good"] = {"0": json.dumps(PAGE)}

    bad_states = {
        "not-object": "[1, 2]",
        "bad-json": "{",
        "bad-status": json.dumps({**valid, "job_id": "bad-status", "status": "bogus"}),
        "bad-type": json.dumps({**valid, "job_id": "bad-type", "max_pages": "10"}),
        "bad-frontier": json.dumps(
            {**valid, "job_id": "bad-frontier", "status": "running", "frontier": [["only-url"]]}
        ),
        "bad-visited": json.dumps({**valid, "job_id": "bad-visited", "visited": [1]}),
        "negative-count": json.dumps({**valid, "job_id": "negative", "pages_crawled": -1}),
        "missing-pages": json.dumps({**valid, "job_id": "missing", "pages_crawled": 1}),
        "bad-page": json.dumps({**valid, "job_id": "bad-page", "pages_crawled": 1}),
    }
    for job_id, raw in bad_states.items():
        client.values[f"bc:crawl:job:{job_id}"] = raw
    client.hashes["bc:crawl:pages:bad-page"] = {"0": "[]"}

    manager = CrawlManager(RedisJobStore(client))
    await manager.restore()

    assert list(manager._jobs) == ["good"]
    assert manager._tasks == {}
    assert logs.warnings.count("Skipping unreadable persisted crawl job") == 7


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", 1),
        ("seed", None),
        ("status", "nope"),
        ("max_pages", "10"),
        ("max_depth", True),
        ("user_agent", 3),
        ("pages_crawled", 1.5),
        ("pages_failed", -1),
        ("error", 4),
        ("created_at", "now"),
        ("finished_at", []),
        ("frontier", "x"),
        ("visited", "x"),
        ("pages", ["not-object"]),
    ],
)
def test_from_dict_rejects_invalid_field_types(field, value):
    record = CrawlJobState(job_id="j", seed=SEED, status="completed").to_dict()
    record[field] = value
    with pytest.raises(ValueError):
        CrawlJobState.from_dict(record)


def test_from_dict_rejects_non_object_records():
    with pytest.raises(ValueError):
        CrawlJobState.from_dict([1, 2])


async def test_failed_checkpoint_is_logged_and_pending_pages_are_saved_later(monkeypatch, web):
    logs = RecordingLogger()
    monkeypatch.setattr(crawler, "logger", logs)

    class FlakyStore(MemoryJobStore):
        failures_left = 1

        async def commit(self, job_id, state, new_pages):
            if self.failures_left:
                self.failures_left -= 1
                raise ConnectionError("Redis went away")
            await super().commit(job_id, state, new_pages)

    store = FlakyStore()
    manager = CrawlManager(store)
    job = await manager.create_job(SEED, 10, 2)
    await _settle(manager, job.job_id)

    assert job.status == "completed"
    assert logs.warnings.count("Failed to checkpoint crawl job") == 1
    state, pages = (await _stored(store))[job.job_id]
    assert state["status"] == "completed"
    assert len(pages) == len(ALL_PAGES)


@pytest.mark.parametrize("backend", ["redis", "disk"])
def test_polling_resumes_after_restart(api_env, web, monkeypatch, tmp_path, backend):
    if backend == "redis":
        client = FakeRedis()
        monkeypatch.setattr(main.settings, "REDIS_URL", "redis://cache:6379/0")
        monkeypatch.setattr(job_store, "_open_redis_client", lambda url: client)
    else:
        monkeypatch.setattr(main.settings, "CRAWL_CHECKPOINTS_ENABLED", True)
        monkeypatch.setenv("BLAZECRAWL_STATE_DIR", str(tmp_path))
    web.blocked = {B}

    with TestClient(main.app) as first:
        created = first.post("/v1/crawl", json={"url": SEED, "max_pages": 10, "max_depth": 2})
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        _poll(first, job_id, lambda body: body["pages_crawled"] == 2)

    web.blocked = set()
    with TestClient(main.app) as second:
        body = _poll(second, job_id, lambda body: body["status"] == "completed")

    assert [page["url"] for page in body["pages"]] == ALL_PAGES
    assert web.completed == ALL_PAGES


def test_startup_serves_completed_job_from_redis(api_env, monkeypatch):
    client = FakeRedis()
    done = CrawlJobState(
        job_id="job-done", seed=SEED, status="completed", pages=[PAGE], pages_crawled=1
    )
    client.values["bc:crawl:job:job-done"] = json.dumps(done.to_state())
    client.hashes["bc:crawl:pages:job-done"] = {"0": json.dumps(PAGE)}
    monkeypatch.setattr(main.settings, "REDIS_URL", "redis://cache:6379/0")
    monkeypatch.setattr(job_store, "_open_redis_client", lambda url: client)

    with TestClient(main.app) as api:
        response = api.get("/v1/crawl/job-done")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["pages"] == [PAGE]
    assert client.closed


def test_api_falls_back_to_memory_when_redis_unreachable(api_env, web, monkeypatch):
    monkeypatch.setattr(main.settings, "REDIS_URL", "redis://cache:6379/0")
    monkeypatch.setattr(
        job_store,
        "_open_redis_client",
        lambda url: FakeRedis(fail_pings=job_store._CONNECT_ATTEMPTS),
    )

    with TestClient(main.app) as api:
        assert isinstance(crawler._manager._store, MemoryJobStore)
        created = api.post("/v1/crawl", json={"url": SEED, "max_pages": 10, "max_depth": 2})
        assert created.status_code == 202
        body = _poll(api, created.json()["job_id"], lambda body: body["status"] == "completed")

    assert body["pages_crawled"] == len(ALL_PAGES)


def test_hard_crash_mid_page_resumes_in_a_new_process(tmp_path):
    root = tmp_path / "checkpoints"
    fetch_log = tmp_path / "fetches.log"
    repo_root = Path(__file__).resolve().parents[1]

    def run_worker(mode: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "tests.crash_worker", mode, str(root), str(fetch_log)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )

    crashed = run_worker("crash")
    assert crashed.returncode == 1, crashed.stderr
    assert fetch_log.read_text(encoding="utf-8").splitlines() == [SEED, A, B]

    resumed = run_worker("resume")
    assert resumed.returncode == 0, resumed.stderr
    ((state, pages),) = asyncio.run(DiskJobStore(root).load_jobs())
    assert state["status"] == "completed"
    assert [pages[index]["url"] for index in sorted(pages)] == ALL_PAGES
    # Seed and A were checkpointed before the crash and are not fetched again.
    assert fetch_log.read_text(encoding="utf-8").splitlines() == [SEED, A, B, B, C, D]
