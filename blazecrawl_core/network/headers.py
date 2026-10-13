"""Shared validation and origin policy for caller-supplied HTTP headers."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

MAX_HEADER_COUNT = 32
MAX_HEADER_BYTES = 8192
MAX_HEADER_NAME = 128
MAX_HEADER_VALUE = 4096
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_MANAGED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
        "http2-settings",
        "expect",
    }
)


def _printable_ascii(value: str) -> bool:
    return all(32 <= ord(char) <= 126 for char in value)


def validate_user_agent(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or not _printable_ascii(value):
        raise ValueError("User-Agent must contain 1-200 printable ASCII characters")
    return value


def validate_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Validate bounded headers and canonicalize names without changing values."""
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise ValueError("headers must be an object of string names and values")
    if len(headers) > MAX_HEADER_COUNT:
        raise ValueError(f"At most {MAX_HEADER_COUNT} custom headers are allowed")
    normalized: dict[str, str] = {}
    size = 0
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= MAX_HEADER_NAME
            or not _TOKEN.fullmatch(name)
        ):
            raise ValueError("Header names must be HTTP tokens of at most 128 characters")
        key = name.lower()
        if key in _MANAGED_HEADERS:
            raise ValueError(f"Header {name!r} is managed by the HTTP transport")
        if key in normalized:
            raise ValueError(f"Duplicate case-insensitive header name: {name!r}")
        if (
            not isinstance(value, str)
            or len(value) > MAX_HEADER_VALUE
            or not _printable_ascii(value)
        ):
            raise ValueError("Header values must be at most 4096 printable ASCII characters")
        if key == "user-agent":
            validate_user_agent(value)
        size += len(name) + len(value) + 4  # ": " and CRLF on the wire.
        if size > MAX_HEADER_BYTES:
            raise ValueError(f"Custom headers exceed {MAX_HEADER_BYTES} bytes")
        normalized[key] = value
    return normalized


def http_origin(url: str) -> tuple[str, str, int]:
    """Compare scheme, IDNA hostname and effective port (not URL paths)."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").encode("idna").decode("ascii").lower()
    return (
        scheme,
        host,
        parts.port if parts.port is not None else (443 if scheme == "https" else 80),
    )
