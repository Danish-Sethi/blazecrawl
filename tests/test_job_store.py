"""Crawl job store backends: memory, Redis (injected fake client), and disk checkpoints."""

import json
import os
from pathlib import Path

import pytest

from blazecrawl_core.engine import job_store
from blazecrawl_core.engine.job_store import (
    DiskJobStore,
    MemoryJobStore,
    RedisJobStore,
    build_job_store,
)
from tests.fakes import FakeRedis

STATE = {
    "job_id": "job-1",
    "seed": "https://example.com/",
    "status": "running",
    "pages_crawled": 1,
    "frontier": [["https://example.com/a", 1]],
    "visited": ["https://example.com/"],
}
PAGE0 = {"url": "https://example.com/", "markdown": "# Home"}
PAGE1 = {"url": "https://example.com/a", "markdown": "# A"}


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


@pytest.fixture
def checkpoints_on(monkeypatch, tmp_path):
    monkeypatch.setattr(job_store.settings, "CRAWL_CHECKPOINTS_ENABLED", True)
    monkeypatch.setenv("BLAZECRAWL_STATE_DIR", str(tmp_path))
    return tmp_path


async def test_memory_store_round_trips_state_and_pages():
    store = MemoryJobStore()
    await store.commit("job-1", STATE, {0: PAGE0})
    await store.commit("job-1", {**STATE, "pages_crawled": 2}, {1: PAGE1})
    assert await store.load_jobs() == [({**STATE, "pages_crawled": 2}, {0: PAGE0, 1: PAGE1})]


async def test_memory_store_isolates_records_from_later_mutation():
    store = MemoryJobStore()
    state = {**STATE, "visited": []}
    page = {"url": "u", "markdown": "m"}
    await store.commit("job-1", state, {0: page})
    state["status"] = "failed"
    page["markdown"] = "changed"
    loaded_state, loaded_pages = (await store.load_jobs())[0]
    assert loaded_state["status"] == "running"
    assert loaded_pages[0]["markdown"] == "m"


async def test_redis_commit_writes_pages_and_state_in_one_transaction():
    client = FakeRedis()
    store = RedisJobStore(client)
    await store.commit("job-1", STATE, {0: PAGE0, 1: PAGE1})
    assert client.executed_pipelines == 1
    assert json.loads(client.values["bc:crawl:job:job-1"]) == STATE
    assert {k: json.loads(v) for k, v in client.hashes["bc:crawl:pages:job-1"].items()} == {
        "0": PAGE0,
        "1": PAGE1,
    }


async def test_redis_store_round_trips_and_skips_corrupt_records(log):
    client = FakeRedis()
    store = RedisJobStore(client)
    await store.commit("job-1", STATE, {0: PAGE0})
    client.values["bc:crawl:job:not-object"] = "[1, 2]"
    client.values["bc:crawl:job:bad-json"] = "{"
    client.values["bc:scrape:unrelated"] = json.dumps({"markdown": "cached"})
    client.hashes["bc:crawl:pages:job-1"]["x"] = json.dumps(PAGE1)
    client.hashes["bc:crawl:pages:job-1"]["1"] = "not json"

    assert await store.load_jobs() == [(STATE, {0: PAGE0})]
    assert "Skipping crawl record that is not a JSON object" in log.warnings
    assert "Skipping unreadable crawl record" in log.warnings
    assert "Skipping crawl page with invalid index" in log.warnings


async def test_redis_close_closes_client():
    client = FakeRedis()
    await RedisJobStore(client).close()
    assert client.closed


async def test_disk_store_round_trips_state_and_pages(tmp_path):
    store = DiskJobStore(tmp_path / "checkpoints")
    await store.commit("job-1", STATE, {0: PAGE0})
    await store.commit("job-1", {**STATE, "pages_crawled": 2}, {1: PAGE1})
    assert await store.load_jobs() == [({**STATE, "pages_crawled": 2}, {0: PAGE0, 1: PAGE1})]


async def test_disk_store_load_is_empty_before_first_commit(tmp_path):
    assert await DiskJobStore(tmp_path / "missing").load_jobs() == []


async def test_disk_failed_state_write_keeps_previous_checkpoint(tmp_path, monkeypatch):
    store = DiskJobStore(tmp_path / "checkpoints")
    await store.commit("job-1", STATE, {0: PAGE0})

    real_replace = os.replace

    def failing_replace(src, dst):
        if Path(dst).name == "state.json":
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(job_store.os, "replace", failing_replace)
    with pytest.raises(OSError):
        await store.commit("job-1", {**STATE, "pages_crawled": 2}, {1: PAGE1})

    job_dir = tmp_path / "checkpoints" / "job-1"
    assert list(job_dir.glob("*.tmp")) == []
    assert list((job_dir / "pages").glob("*.tmp")) == []
    loaded_state, loaded_pages = (await store.load_jobs())[0]
    assert loaded_state == STATE
    # The page written before the failed state write is an orphan: loaders keep it, but
    # the manager only uses the first pages_crawled entries.
    assert loaded_pages == {0: PAGE0, 1: PAGE1}


async def test_disk_store_skips_corrupt_files_and_leftover_temp_files(tmp_path, log):
    root = tmp_path / "checkpoints"
    good = root / "good"
    (good / "pages").mkdir(parents=True)
    (good / "state.json").write_text(json.dumps(STATE), encoding="utf-8")
    (good / "pages" / "00000000.json").write_text(json.dumps(PAGE0), encoding="utf-8")
    (good / "pages" / "00000001.json").write_text("{broken", encoding="utf-8")
    (good / "pages" / "notes.json").write_text(json.dumps(PAGE1), encoding="utf-8")
    (good / "pages" / "00000002.json.abc.tmp").write_text("partial", encoding="utf-8")
    broken = root / "broken"
    broken.mkdir()
    (broken / "state.json").write_text("[]", encoding="utf-8")
    (root / "stray-file.txt").write_text("not a job", encoding="utf-8")
    (root / "tmp-only").mkdir()
    (root / "tmp-only" / "state.json.abc.tmp").write_text("{}", encoding="utf-8")

    assert await DiskJobStore(root).load_jobs() == [(STATE, {0: PAGE0})]
    assert "Skipping crawl record that is not a JSON object" in log.warnings


async def test_disk_store_rejects_unsafe_job_ids(tmp_path):
    with pytest.raises(ValueError):
        await DiskJobStore(tmp_path).commit("../escape", STATE, {})


async def test_build_without_configuration_uses_memory(monkeypatch, log):
    monkeypatch.setattr(job_store.settings, "REDIS_URL", None)
    monkeypatch.setattr(job_store.settings, "CRAWL_CHECKPOINTS_ENABLED", False)
    assert isinstance(await build_job_store(), MemoryJobStore)
    assert log.warnings == []


async def test_build_uses_redis_when_reachable(monkeypatch, redis_url):
    client = FakeRedis()
    monkeypatch.setattr(job_store, "_open_redis_client", lambda url: client)
    assert isinstance(await build_job_store(), RedisJobStore)
    assert client.ping_calls == 1


async def test_build_retries_transient_redis_failure(monkeypatch, redis_url):
    client = FakeRedis(fail_pings=1)
    monkeypatch.setattr(job_store, "_open_redis_client", lambda url: client)
    assert isinstance(await build_job_store(), RedisJobStore)
    assert client.ping_calls == 2


async def test_unreachable_redis_falls_back_to_memory_and_warns(monkeypatch, redis_url, log):
    client = FakeRedis(fail_pings=job_store._CONNECT_ATTEMPTS)
    monkeypatch.setattr(job_store, "_open_redis_client", lambda url: client)
    monkeypatch.setattr(job_store.settings, "CRAWL_CHECKPOINTS_ENABLED", False)
    store = await build_job_store()
    assert isinstance(store, MemoryJobStore)
    assert client.ping_calls == job_store._CONNECT_ATTEMPTS
    assert client.closed
    assert log.warnings == ["Redis unavailable; falling back to a non-Redis crawl job store"]


async def test_unreachable_redis_falls_back_to_disk_when_enabled(
    monkeypatch, redis_url, checkpoints_on, log
):
    client = FakeRedis(fail_pings=job_store._CONNECT_ATTEMPTS)
    monkeypatch.setattr(job_store, "_open_redis_client", lambda url: client)
    monkeypatch.setattr(job_store.settings, "CRAWL_CHECKPOINTS_ENABLED", True)
    store = await build_job_store()
    assert isinstance(store, DiskJobStore)
    assert store.root == checkpoints_on / "crawl-checkpoints"
    assert store.root.is_dir()
    assert log.warnings == ["Redis unavailable; falling back to a non-Redis crawl job store"]


async def test_checkpoint_directory_outside_writable_location_falls_back_to_memory(
    monkeypatch, tmp_path, log
):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("BLAZECRAWL_STATE_DIR", str(blocker))
    monkeypatch.setattr(job_store.settings, "REDIS_URL", None)
    monkeypatch.setattr(job_store.settings, "CRAWL_CHECKPOINTS_ENABLED", True)
    assert isinstance(await build_job_store(), MemoryJobStore)
    assert log.warnings == [
        "Crawl checkpoint directory unusable; falling back to in-memory crawl job store"
    ]
