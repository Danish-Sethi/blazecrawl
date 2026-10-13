"""Shared SSRF-safe egress layer (M2 / C-1).

Every fetch of a user-controlled URL MUST go through this module so the
DNS-rebinding / TOCTOU fix lives in exactly one place.

Two mechanisms are provided:

1. ``PinnedAsyncClient`` — an httpx-based async client that:
     * validates + pins the target IP via :func:`validate_and_pin` (resolve once),
     * connects the socket to the EXACT validated IP (never re-resolves), while
       preserving the original hostname for the Host header and TLS SNI/cert
       validation,
     * follows redirects MANUALLY, re-validating + re-pinning every hop and
       blocking cross-scheme downgrades and redirects to blocked hosts.

2. Helper accessors (``curl_resolve_entries`` / ``host_resolver_rules`` re-exported
   from :mod:`ssrf`) for the curl_cffi and headless-browser paths, which do their
   own DNS and therefore need their own pin.

Fail closed: if a target cannot be validated and pinned, we raise
:class:`SSRFValidationError` rather than fall back to an unpinned fetch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from blazecrawl_core.config import settings
from blazecrawl_core.logging import get_logger
from blazecrawl_core.network.browser_proxy import BrowserEgressProxy
from blazecrawl_core.network.headers import http_origin, validate_headers, validate_user_agent
from blazecrawl_core.network.ssrf import (
    PinnedTarget,
    SSRFValidationError,
    validate_and_pin,
)
from blazecrawl_core.network.upstream_proxy import UpstreamProxy, authority

logger = get_logger(__name__)

DEFAULT_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass
class EgressResponse:
    """Minimal response object returned by the pinned egress client."""

    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    redirects: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def _pinned_transport(target: PinnedTarget, *, verify: bool) -> httpx.AsyncHTTPTransport:
    """Build an httpx transport that connects ONLY to the validated IP.

    We pass ``sni_hostname`` so TLS SNI + certificate verification use the real
    hostname even though the socket connects to the pinned IP.
    """
    # Reuse a standard verified SSL context for https; ``verify`` lets callers
    # disable cert checks (never used for user URLs).
    return httpx.AsyncHTTPTransport(retries=0, verify=verify)


def _pin_url_to_ip(target: PinnedTarget) -> str:
    """Return a URL whose host is the validated IP (for the actual connection)."""
    # Bracket IPv6 literals.
    host = f"[{target.ip}]" if ":" in target.ip else target.ip
    return f"{target.scheme}://{host}:{target.port}{_path_q(target.url)}"


def _path_q(url: str) -> str:
    parts = urlsplit(url)
    pq = parts.path or "/"
    if parts.query:
        pq += f"?{parts.query}"
    return pq


class PinnedAsyncClient:
    """SSRF-safe async HTTP client with pin-at-connect and manual redirects."""

    def __init__(
        self,
        *,
        timeout_s: float = 30.0,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        verify: bool = True,
        user_agent: str | None = None,
        max_bytes: int | None = None,
        upstream_proxy_url: str | None = None,
    ) -> None:
        self._timeout = httpx.Timeout(timeout_s)
        self._max_redirects = max_redirects
        self._verify = verify
        self._user_agent = user_agent
        self._max_bytes = max_bytes
        self._upstream_proxy_url = upstream_proxy_url
        if upstream_proxy_url is not None:
            UpstreamProxy.parse(upstream_proxy_url)
            if not verify:
                raise SSRFValidationError("Upstream proxy requires TLS certificate verification")

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> EgressResponse:
        """Perform a request, validating + pinning every hop.

        Raises:
            SSRFValidationError: if the initial URL or any redirect target fails
                validation/pinning (fail closed).
        """
        if self._upstream_proxy_url is None:
            return await self._request(method, url, headers=headers, content=content)
        proxy = BrowserEgressProxy(
            upstream_proxy_url=self._upstream_proxy_url,
            connect_timeout=self._timeout.connect,
            read_timeout=self._timeout.read,
        )
        try:
            local_proxy_url = await proxy.start()
            response = await self._request(
                method, url, headers=headers, content=content, local_proxy_url=local_proxy_url
            )
            if any(event["event"] == "failed" for event in proxy.connection_events):
                raise SSRFValidationError("Upstream proxy connection refused or failed validation")
            return response
        finally:
            await proxy.close()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        content: bytes | None,
        local_proxy_url: str | None = None,
    ) -> EgressResponse:
        redirects: list[str] = []
        current_url = url
        base_headers = validate_headers(headers)
        if self._user_agent is not None:
            validate_user_agent(self._user_agent)

        for _hop in range(self._max_redirects + 1):
            # Validate + pin the CURRENT hop. Re-resolution happens here and only
            # here, and the connection below is bound to the returned IP.
            target = await validate_and_pin(current_url)

            # Only our controlled loopback proxy receives the hostname. It
            # validates again at the socket boundary and sends a numeric CONNECT
            # upstream; httpx retains the origin hostname for TLS verification.
            connect_url = current_url if local_proxy_url is not None else _pin_url_to_ip(target)
            req_headers = dict(base_headers)
            if self._user_agent:
                req_headers.setdefault("user-agent", self._user_agent)
            req_headers["Host"] = (
                f"[{target.hostname}]" if ":" in target.hostname else target.hostname
            )
            if target.port != (443 if target.scheme == "https" else 80):
                req_headers["Host"] = authority(target.hostname, target.port)

            transport = (
                httpx.AsyncHTTPTransport(proxy=local_proxy_url, verify=True, retries=0)
                if local_proxy_url is not None
                else _pinned_transport(target, verify=self._verify)
            )
            # extensions carry the SNI hostname so TLS verifies against the real
            # host even though we dialed the pinned IP.
            extensions: dict[str, Any] = {"sni_hostname": target.hostname}

            async with httpx.AsyncClient(
                transport=transport,
                timeout=self._timeout,
                follow_redirects=False,  # we follow manually, re-validating each hop
            ) as client:
                request = client.build_request(
                    method,
                    connect_url,
                    headers=req_headers,
                    content=content,
                    extensions=extensions,
                )
                if self._max_bytes is not None:
                    # Stream so we can enforce a hard size cap without buffering
                    # an unbounded body into memory.
                    response = await client.send(request, stream=True)
                    try:
                        try:
                            declared_length = int(response.headers.get("content-length", ""))
                        except ValueError:
                            declared_length = None
                        if declared_length is not None and declared_length > self._max_bytes:
                            raise SSRFValidationError(
                                f"Response exceeds max_bytes ({self._max_bytes})"
                            )
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > self._max_bytes:
                                raise SSRFValidationError(
                                    f"Response exceeds max_bytes ({self._max_bytes})"
                                )
                        body = bytes(chunks)
                    finally:
                        await response.aclose()
                else:
                    response = await client.send(request)
                    body = await response.aread()

            if response.status_code in _REDIRECT_STATUSES and "location" in response.headers:
                location = response.headers["location"]
                next_url = urljoin(current_url, location)
                next_scheme = urlsplit(next_url).scheme.lower()
                cur_scheme = urlsplit(current_url).scheme.lower()
                # Block https->http downgrade redirects.
                if cur_scheme == "https" and next_scheme == "http":
                    raise SSRFValidationError("Blocked redirect: https→http downgrade")
                if http_origin(current_url) != http_origin(next_url):
                    base_headers = {}
                redirects.append(next_url)
                current_url = next_url
                continue

            return EgressResponse(
                url=url,
                final_url=current_url,
                status_code=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                content=body,
                redirects=redirects,
            )

        raise SSRFValidationError(f"Too many redirects (>{self._max_redirects})")

    async def get(self, url: str, **kwargs: Any) -> EgressResponse:
        return await self.request("GET", url, **kwargs)


async def safe_fetch(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout_s: float = 30.0,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    user_agent: str | None = None,
    max_bytes: int | None = None,
) -> EgressResponse:
    """One-shot SSRF-safe fetch through the pinned egress client."""
    client = PinnedAsyncClient(
        timeout_s=timeout_s,
        max_redirects=max_redirects,
        user_agent=user_agent,
        max_bytes=max_bytes,
        upstream_proxy_url=settings.EGRESS_UPSTREAM_PROXY,
    )
    return await client.request(method, url, headers=headers)


async def install_browser_ssrf_guard(
    page,
    *,
    allow_missing_route: bool = False,
    initial_url: str | None = None,
    headers: dict[str, str] | None = None,
    user_agent: str | None = None,
) -> None:
    """Install a Playwright route guard that re-validates EVERY browser request.

    Chromium performs its own DNS, so HTTP-client IP pinning does not protect
    the browser path. This guard intercepts every request the page makes
    (top-level navigation, redirects, sub-resources) and aborts any whose URL
    fails SSRF validation — closing the rebinding hole for in-page navigations
    and sub-resource fetches.

    Shared by the hybrid renderer and the extract/interact workers so the
    browser-path defense lives in one place.

    Fails CLOSED by default: if ``page`` has no callable ``route`` (interception
    cannot be installed), this raises ``SSRFValidationError`` rather than
    silently letting the page navigate unguarded. Pass ``allow_missing_route=True``
    only from an explicit, narrow test double that intentionally exercises code
    without Playwright routing support — never from production call sites.
    """
    import inspect

    route = getattr(page, "route", None)
    if not callable(route):
        if allow_missing_route:
            return
        raise SSRFValidationError(
            "Cannot install browser SSRF guard: page has no request interception "
            "support. Refusing to navigate unguarded."
        )

    async def _guard(route_obj, request):  # noqa: ANN001
        req_url = getattr(request, "url", "") or ""
        try:
            await validate_and_pin(req_url)
        except SSRFValidationError:
            logger.warning("Browser SSRF guard blocked request", url=req_url)
            abort = getattr(route_obj, "abort", None)
            if callable(abort):
                res = abort()
                if inspect.isawaitable(res):
                    await res
            return
        # Preserve previously installed routes, notably the pool's resource guard.
        cont = getattr(route_obj, "fallback", None) or getattr(route_obj, "continue_", None)
        if callable(cont):
            res = cont()
            if inspect.isawaitable(res):
                await res

    maybe = route("**/*", _guard)
    if inspect.isawaitable(maybe):
        await maybe

    custom_headers = validate_headers(headers)
    if not custom_headers:
        return
    if initial_url is None or user_agent is None:
        raise ValueError("Origin-scoped browser headers require initial_url and user_agent")
    validate_user_agent(user_agent)
    origin = http_origin(initial_url)
    # Playwright route header overrides persist across redirects, and its route
    # handler only sees the first hop. CDP pauses every hop without replacing
    # the existing page routes (including SSRF and resource-blocking handlers).
    session = await page.context.new_cdp_session(page)

    async def _scope_headers(event: dict) -> None:
        request = event["request"]
        req_url = request["url"]
        try:
            await validate_and_pin(req_url)
        except SSRFValidationError:
            logger.warning("Browser header guard blocked request", url=req_url)
            await session.send(
                "Fetch.failRequest",
                {"requestId": event["requestId"], "errorReason": "BlockedByClient"},
            )
            return
        scoped = {name.lower(): value for name, value in request["headers"].items()}
        # Reconstruct each hop from Chromium's headers, never an earlier override.
        for name in custom_headers:
            scoped.pop(name, None)
        scoped["user-agent"] = user_agent
        if http_origin(req_url) == origin:
            scoped.update(custom_headers)
        await session.send(
            "Fetch.continueRequest",
            {
                "requestId": event["requestId"],
                "headers": [{"name": name, "value": value} for name, value in scoped.items()],
            },
        )

    session.on("Fetch.requestPaused", _scope_headers)
    await session.send(
        "Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
    )


def install_context_popup_ssrf_guard(context) -> None:
    """Guard every page a browser CONTEXT ever creates, including popups.

    Per-page guard installation at each call site protects the page the
    caller explicitly navigates. It does NOT protect a page a hostile page
    opens itself (``window.open()``, ``target="_blank"``) before our code
    ever touches it — that popup is a brand-new Playwright ``Page`` with no
    guard until something installs one.

    This listens for the context-level ``"page"`` event, which Playwright
    fires as soon as a new page/popup exists, and installs the same
    fail-closed SSRF guard on it immediately. It is defense-in-depth on top
    of (not a replacement for) the explicit per-page guard installed before
    intentional navigation.

    ponytail: the install is fire-and-forget (``asyncio.ensure_future``)
    because Playwright's ``"page"`` event handler is synchronous, so we
    cannot await guard installation before the popup's first navigation
    request is sent — there is an unavoidable small race for the very first
    request of a popup. ``allow_missing_route=True`` keeps that fire-and-forget
    task from raising into an unhandled-task-exception log line for the (real
    Playwright pages only) case where routing genuinely is not supported.
    Ceiling: a popup's first request could in theory race ahead of guard
    installation. Upgrade path: use ``context.expect_page()`` at each call
    site that intentionally triggers a popup so the guard installs before
    that popup's first navigation is dispatched, once a caller actually needs
    popups.
    """

    def _on_new_page(new_page) -> None:  # noqa: ANN001
        asyncio.ensure_future(install_browser_ssrf_guard(new_page, allow_missing_route=True))

    on = getattr(context, "on", None)
    if callable(on):
        on("page", _on_new_page)


async def validate_proxy_endpoint(proxy_url: str) -> None:
    """Validate a credential-free endpoint; actual dials always validate again."""
    proxy = UpstreamProxy.parse(proxy_url)
    try:
        await validate_and_pin(f"{proxy.scheme}://{authority(proxy.hostname, proxy.port)}/")
    except SSRFValidationError:
        raise SSRFValidationError("Upstream proxy endpoint failed validation") from None


class PinnedProxyClient(PinnedAsyncClient):
    """Pinned egress through HTTP(S) CONNECT, sharing the direct redirect loop."""

    def __init__(
        self,
        proxy_url: str,
        *,
        timeout_s: float = 30.0,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        verify: bool = True,
        user_agent: str | None = None,
        max_bytes: int | None = None,
    ) -> None:
        super().__init__(
            upstream_proxy_url=proxy_url,
            timeout_s=timeout_s,
            max_redirects=max_redirects,
            verify=verify,
            user_agent=user_agent,
            max_bytes=max_bytes,
        )
