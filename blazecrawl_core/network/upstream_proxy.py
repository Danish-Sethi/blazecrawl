"""Validated, IP-pinned HTTP(S) upstream CONNECT sockets (never remote DNS)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import socket
import ssl
from dataclasses import dataclass, field
from urllib.parse import unquote_to_bytes, urlsplit

from blazecrawl_core.network.ip_utils import is_blocked_address, parse_ip_literal
from blazecrawl_core.network.ssrf import SSRFValidationError, validate_and_pin

_MAX_HEADER = 64 * 1024


def authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(frozen=True)
class UpstreamProxy:
    scheme: str
    hostname: str
    port: int
    authorization: bytes | None = field(default=None, repr=False)

    @classmethod
    def parse(cls, url: str) -> UpstreamProxy:
        """Reject ambiguous configuration without echoing credentials."""
        try:
            if not url or len(url) > _MAX_HEADER or any(ord(c) <= 32 or ord(c) == 127 for c in url):
                raise ValueError
            if "\\" in url or "?" in url or "#" in url:
                raise ValueError
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or parts.path not in {"", "/"}:
                raise ValueError
            host = parts.hostname
            if not host or "%" in host:
                raise ValueError
            host = host.encode("idna").decode("ascii")
            port = parts.port
            if parts.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535):
                raise ValueError
            authorization = None
            if parts.username is not None:
                if parts.netloc.count("@") != 1 or not parts.username or not parts.password:
                    raise ValueError
                credentials = (parts.username, parts.password)
                if any(re.search(r"%(?![0-9a-fA-F]{2})", value) for value in credentials):
                    raise ValueError
                username, password = (unquote_to_bytes(value) for value in credentials)
                if b":" in username or any(c < 32 or c == 127 for c in username + password):
                    raise ValueError
                authorization = b"Basic " + base64.b64encode(username + b":" + password)
            return cls(
                parts.scheme, host, port or (443 if parts.scheme == "https" else 80), authorization
            )
        except (ValueError, UnicodeError):
            raise SSRFValidationError(
                "Invalid upstream proxy configuration (HTTP/HTTPS required)"
            ) from None

    async def connect(
        self, ip: str, port: int, *, connect_timeout: float, read_timeout: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        writer = None
        try:
            literal = parse_ip_literal(ip)
            if literal is None or is_blocked_address(literal) or not 1 <= port <= 65535:
                raise SSRFValidationError("Upstream CONNECT destination failed validation")
            ip = str(literal)
            # The credential-free endpoint is freshly resolved at the socket boundary.
            endpoint = await asyncio.wait_for(
                validate_and_pin(f"{self.scheme}://{authority(self.hostname, self.port)}/"),
                connect_timeout,
            )
            reader, writer = await open_pinned_connection(
                endpoint.ip,
                endpoint.port,
                timeout=connect_timeout,
                ssl_context=ssl.create_default_context() if self.scheme == "https" else None,
                server_hostname=self.hostname if self.scheme == "https" else None,
            )
            target = authority(ip, port).encode("ascii")
            request = b"CONNECT " + target + b" HTTP/1.1\r\nHost: " + target + b"\r\n"
            if self.authorization is not None:
                request += b"Proxy-Authorization: " + self.authorization + b"\r\n"
            writer.write(request + b"\r\n")
            await asyncio.wait_for(writer.drain(), connect_timeout)
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), read_timeout)
            lines = header.split(b"\r\n")
            status = lines[0].split(b" ", 2)
            if (
                len(header) > _MAX_HEADER
                or len(status) < 2
                or status[0] not in {b"HTTP/1.0", b"HTTP/1.1"}
                or len(status[1]) != 3
                or not status[1].isdigit()
                or not 200 <= int(status[1]) < 300
                or any(not line or b":" not in line or line[:1] in b" \t" for line in lines[1:-2])
            ):
                raise SSRFValidationError("Upstream proxy CONNECT refused or malformed")
            return reader, writer
        except asyncio.CancelledError:
            if writer is not None:
                await close_writer(writer)
            raise
        except (
            SSRFValidationError,
            OSError,
            ValueError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            if writer is not None:
                await close_writer(writer)
            raise SSRFValidationError(
                "Upstream proxy connection refused or failed validation"
            ) from None


async def open_pinned_connection(
    ip: str,
    port: int,
    *,
    timeout: float,
    ssl_context: ssl.SSLContext | None = None,
    server_hostname: str | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Dial a numeric address; never hand a hostname to the socket resolver."""
    sock = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    address = (ip, port, 0, 0) if ":" in ip else (ip, port)
    try:
        await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, address), timeout)
        return await asyncio.wait_for(
            asyncio.open_connection(
                sock=sock, ssl=ssl_context, server_hostname=server_hostname, limit=_MAX_HEADER
            ),
            timeout,
        )
    except BaseException:
        sock.close()
        raise


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), 5.0)
