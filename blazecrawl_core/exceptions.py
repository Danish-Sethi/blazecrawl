"""Exception hierarchy for BlazeCrawl Core (self-contained subset)."""

from __future__ import annotations

from typing import Any


class BlazeCrawlError(Exception):
    """Base exception for all BlazeCrawl Core errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class ScrapeTimeoutError(BlazeCrawlError):
    """Raised when a scrape operation times out."""


class BrowserPoolExhaustedError(BlazeCrawlError):
    """Raised when the browser pool has no available contexts."""


class CacheError(BlazeCrawlError):
    """Raised when a cache operation fails critically."""


class ScreenshotTooLargeError(BlazeCrawlError):
    """Raised when a captured screenshot exceeds the configured byte cap."""


class AuthenticationError(BlazeCrawlError):
    """Raised when authentication fails."""


class RateLimitExceededError(BlazeCrawlError):
    """Raised when the rate limit is exceeded."""


class ServiceError(BlazeCrawlError):
    """Raised when a service operation fails with a specific HTTP status."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.details: dict | None = None


EXCEPTION_STATUS_CODES: dict[type[BlazeCrawlError], int] = {
    ScrapeTimeoutError: 408,
    BrowserPoolExhaustedError: 503,
    CacheError: 500,
    ScreenshotTooLargeError: 413,
    AuthenticationError: 401,
    RateLimitExceededError: 429,
    ServiceError: 500,
}


def get_status_code(exception: BlazeCrawlError) -> int:
    """Return the HTTP status code for a given exception.

    An instance-level ``status_code`` (e.g. on ``ServiceError``) wins over the
    static mapping table.
    """
    instance_status_code = getattr(exception, "status_code", None)
    if isinstance(instance_status_code, int):
        return instance_status_code
    for exc_type, status_code in EXCEPTION_STATUS_CODES.items():
        if isinstance(exception, exc_type):
            return status_code
    return 500
