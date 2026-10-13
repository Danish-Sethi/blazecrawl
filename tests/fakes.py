"""In-memory stand-in for the ``redis.asyncio`` client surface used by ``RedisJobStore``."""

from __future__ import annotations

import fnmatch
from collections.abc import AsyncIterator
from typing import Any


class FakePipeline:
    def __init__(self, client: FakeRedis) -> None:
        self._client = client
        self._ops: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def hset(self, name: str, key: str, value: str) -> FakePipeline:
        self._ops.append(("hset", name, key, value))
        return self

    def set(self, key: str, value: str) -> FakePipeline:
        self._ops.append(("set", key, value))
        return self

    async def execute(self) -> None:
        # Applied together, mirroring a MULTI/EXEC transaction.
        self._client.executed_pipelines += 1
        for op in self._ops:
            if op[0] == "hset":
                _, name, key, value = op
                self._client.hashes.setdefault(name, {})[key] = value
            else:
                _, key, value = op
                self._client.values[key] = value
        self._ops = []


class FakeRedis:
    def __init__(self, fail_pings: int = 0) -> None:
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.fail_pings = fail_pings
        self.ping_calls = 0
        self.executed_pipelines = 0
        self.closed = False

    async def ping(self) -> bool:
        self.ping_calls += 1
        if self.ping_calls <= self.fail_pings:
            raise ConnectionError("Connection refused")
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def hgetall(self, name: str) -> dict[str, str]:
        return dict(self.hashes.get(name, {}))

    async def scan_iter(self, match: str) -> AsyncIterator[str]:
        for key in list(self.values):
            if fnmatch.fnmatchcase(key, match):
                yield key

    async def aclose(self) -> None:
        self.closed = True
