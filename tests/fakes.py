"""In-memory stand-in for the ``redis.asyncio`` client surface used by ``RedisJobStore``."""

from __future__ import annotations

import fnmatch
from collections.abc import AsyncIterator


class FakeRedis:
    def __init__(self, fail_pings: int = 0) -> None:
        self.values: dict[str, str] = {}
        self.fail_pings = fail_pings
        self.ping_calls = 0
        self.closed = False

    async def ping(self) -> bool:
        self.ping_calls += 1
        if self.ping_calls <= self.fail_pings:
            raise ConnectionError("Connection refused")
        return True

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.values.get(key) for key in keys]

    async def scan_iter(self, match: str) -> AsyncIterator[str]:
        for key in list(self.values):
            if fnmatch.fnmatchcase(key, match):
                yield key

    async def aclose(self) -> None:
        self.closed = True
