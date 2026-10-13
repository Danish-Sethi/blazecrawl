"""Controlled browser egress proxy.

Chromium is configured with this proxy and an empty bypass list.  Every HTTP
origin connection and HTTPS CONNECT is resolved by this process, checked against
the SSRF policy, and opened to the selected validated IP.  The target hostname
remains in the HTTP authority / CONNECT request, so Chromium retains TLS SNI and
certificate verification semantics.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from blazecrawl_core.logging import get_logger
from blazecrawl_core.network.ip_utils import is_blocked_address, parse_ip_literal
from blazecrawl_core.network.resolver import Resolver, SystemResolver
from blazecrawl_core.network.ssrf import SSRFValidationError
from blazecrawl_core.network.upstream_proxy import (
    UpstreamProxy,
    close_writer,
    open_pinned_connection,
)

logger = get_logger(__name__)

_MAX_HEADER = 64 * 1024
_MAX_LINE = 8192
_CONNECT_TIMEOUT = 10.0
_IDLE_TIMEOUT = 60.0


class BrowserEgressProxy:
    def __init__(
        self,
        *,
        max_connections: int = 128,
        resolver: Resolver | None = None,
        upstream_proxy_url: str | None = None,
        connect_timeout: float = _CONNECT_TIMEOUT,
        read_timeout: float = _IDLE_TIMEOUT,
    ) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._slots = asyncio.Semaphore(max_connections)
        self._resolver = resolver or SystemResolver()
        self._upstream = (
            UpstreamProxy.parse(upstream_proxy_url) if upstream_proxy_url is not None else None
        )
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self.port = 0
        self.connection_events: list[dict[str, Any]] = []

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        assert self._server.sockets
        self.port = int(self._server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{self.port}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _authority(value: str, *, default_port: int | None = None) -> tuple[str, int]:
        if any(ch in value for ch in ("@", "\\", "\r", "\n")):
            raise SSRFValidationError("Invalid proxy authority")
        # httpcore emits unbracketed IPv6 proxy authorities. Normalize only a
        # numeric IPv6 host with an explicit port; the usual SSRF checks still apply.
        if value.count(":") > 1 and not value.startswith("["):
            host, _, port_text = value.rpartition(":")
            literal = parse_ip_literal(host)
            if literal is None or literal.version != 6 or not port_text.isdecimal():
                raise SSRFValidationError("Invalid proxy authority")
            value = f"[{host}]:{port_text}"
        parsed = urlsplit(f"//{value}")
        if (
            parsed.username is not None
            or parsed.password is not None
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise SSRFValidationError("Invalid proxy authority")
        try:
            port = parsed.port if parsed.port is not None else default_port
        except ValueError as exc:
            raise SSRFValidationError("Invalid proxy port") from exc
        if port is None or not 1 <= port <= 65535:
            raise SSRFValidationError("Invalid proxy port")
        return parsed.hostname, port

    async def _validated_socket(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        literal = parse_ip_literal(host)
        raw_ips = (
            [str(literal)]
            if literal
            else await asyncio.wait_for(self._resolver.resolve(host, port), self._connect_timeout)
        )
        ips = [parse_ip_literal(s) for s in raw_ips]
        if not ips or any(ip is None or is_blocked_address(ip) for ip in ips):
            self.connection_events.append(
                {
                    "event": "rejected",
                    "host": host,
                    "port": port,
                    "resolved": raw_ips,
                }
            )
            raise SSRFValidationError("Browser proxy rejected prohibited address")
        last: Exception | None = None
        for ip in ips:
            assert ip is not None
            try:
                if self._upstream is None:
                    connection = await open_pinned_connection(
                        str(ip), port, timeout=self._connect_timeout
                    )
                else:
                    connection = await self._upstream.connect(
                        str(ip),
                        port,
                        connect_timeout=self._connect_timeout,
                        read_timeout=self._read_timeout,
                    )
                self.connection_events.append(
                    {"event": "connected", "host": host, "port": port, "ip": str(ip)}
                )
                return connection
            except (TimeoutError, OSError) as exc:
                last = exc
        raise OSError("validated browser destination could not be reached") from last

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        try:
            async with self._slots:
                header = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), self._connect_timeout
                )
                if len(header) > _MAX_HEADER:
                    raise SSRFValidationError("Proxy headers too large")
                lines = header.decode("latin-1").split("\r\n")
                if not lines or len(lines[0]) > _MAX_LINE:
                    raise SSRFValidationError("Proxy request line too large")
                parts = lines[0].split(" ")
                if len(parts) != 3 or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise SSRFValidationError("Malformed proxy request")
                method, target, _version = parts
                method = method.upper()
                if method == "CONNECT":
                    host, port = self._authority(target)
                elif method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
                    parsed = urlsplit(target)
                    if parsed.scheme.lower() != "http" or not parsed.netloc:
                        raise SSRFValidationError("Proxy requires an absolute HTTP URL")
                    host, port = self._authority(parsed.netloc, default_port=80)
                else:
                    raise SSRFValidationError("Unsupported proxy method")
                upstream = await self._validated_socket(host, port)
                upstream_reader, upstream_writer = upstream
                if method == "CONNECT":
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                else:
                    parsed = urlsplit(target)
                    origin = parsed.path or "/"
                    if parsed.query:
                        origin += "?" + parsed.query
                    rewritten = (
                        f"{method} {origin} HTTP/1.1\r\n"
                        + "\r\n".join(
                            line
                            for line in lines[1:-2]
                            if not line.lower().startswith(
                                ("proxy-authorization:", "proxy-connection:")
                            )
                        )
                        + "\r\n\r\n"
                    )
                    upstream_writer.write(rewritten.encode("latin-1"))
                    await upstream_writer.drain()
                await self._tunnel(reader, writer, upstream_reader, upstream_writer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.connection_events.append({"event": "failed", "reason": type(exc).__name__})
            logger.warning("Egress proxy request failed", reason=type(exc).__name__)
            if not writer.is_closing():
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await writer.drain()
        finally:
            if upstream is not None:
                await close_writer(upstream[1])
            await close_writer(writer)

    async def _tunnel(self, client_reader, client_writer, upstream_reader, upstream_writer) -> None:
        async def copy(source, destination):
            try:
                while True:
                    data = await asyncio.wait_for(source.read(65536), self._read_timeout)
                    if not data:
                        break
                    destination.write(data)
                    await destination.drain()
            finally:
                if not destination.is_closing():
                    destination.close()

        tasks = [
            asyncio.create_task(copy(client_reader, upstream_writer)),
            asyncio.create_task(copy(upstream_reader, client_writer)),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
