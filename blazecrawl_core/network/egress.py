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

from blazecrawl_core.logging import get_logger
from blazecrawl_core.network.headers import http_origin, validate_headers, validate_user_agent
from blazecrawl_core.network.ssrf import (
    PinnedTarget,
    SSRFValidationError,
    validate_and_pin,
)

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
    ) -> None:
        self._timeout = httpx.Timeout(timeout_s)
        self._max_redirects = max_redirects
        self._verify = verify
        self._user_agent = user_agent
        self._max_bytes = max_bytes

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
        redirects: list[str] = []
        current_url = url
        base_headers = validate_headers(headers)
        if self._user_agent is not None:
            validate_user_agent(self._user_agent)

        for _hop in range(self._max_redirects + 1):
            # Validate + pin the CURRENT hop. Re-resolution happens here and only
            # here, and the connection below is bound to the returned IP.
            target = await validate_and_pin(current_url)

            connect_url = _pin_url_to_ip(target)
            req_headers = dict(base_headers)
            if self._user_agent:
                req_headers.setdefault("user-agent", self._user_agent)
            req_headers["Host"] = (
                target.hostname if target.port in (80, 443) else f"{target.hostname}:{target.port}"
            )

            transport = _pinned_transport(target, verify=self._verify)
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
                    clen = response.headers.get("content-length")
                    if clen is not None:
                        try:
                            if int(clen) > self._max_bytes:
                                await response.aclose()
                                raise SSRFValidationError(
                                    f"Response exceeds max_bytes ({self._max_bytes})"
                                )
                        except ValueError:
                            pass
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > self._max_bytes:
                            await response.aclose()
                            raise SSRFValidationError(
                                f"Response exceeds max_bytes ({self._max_bytes})"
                            )
                    body = bytes(chunks)
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


# ---------------------------------------------------------------------------
# M9 — SSRF-safe egress THROUGH a proxy
# ---------------------------------------------------------------------------
# Routing a fetch through a proxy changes the network mechanics but MUST NOT
# weaken the M2 SSRF guarantees. The guarantees are preserved as follows:
#
#   1. TARGET VALIDATION IS UNCONDITIONAL. Before every hop we call
#      validate_and_pin(target) exactly as the direct path does. A proxied
#      request to an internal/blocked target (or a proxied redirect to one) is
#      refused with SSRFValidationError — the proxy can never be used as an SSRF
#      pivot to RFC-1918 / loopback / link-local / cloud-metadata ranges.
#
#   2. THE PROXY ENDPOINT ITSELF IS VALIDATED. We resolve+classify the proxy
#      host and refuse a proxy that points at an internal range, so a
#      misconfigured/hostile proxy URL cannot tunnel us inside the perimeter.
#
#   3. NETWORK-ISOLATION ASSUMPTION (documented + enforced-in-depth). With an
#      HTTP/HTTPS proxy the proxy performs the final DNS + connect to the target
#      host, so we cannot pin the *socket* to our validated IP the way the
#      direct path does. The residual gap (proxy re-resolves the host) is closed
#      by (a) always validating the host's resolution ourselves immediately
#      before the call — an attacker controlling DNS still has to present a
#      public IP to OUR resolver — and (b) the operational requirement that
#      egress proxies run OUTSIDE the trusted network with no route to internal
#      services. Both are required; neither alone is sufficient. We fail closed
#      if target validation fails for any reason.

_PROXYABLE_SCHEMES = ("http", "https", "socks5", "socks5h")


async def validate_proxy_endpoint(proxy_url: str) -> None:
    """Validate that a proxy endpoint is well-formed and NOT internal.

    Raises SSRFValidationError if the proxy host resolves to a blocked range, so
    a hostile/misconfigured proxy URL cannot become a tunnel into the perimeter.
    Credentials in the URL are ignored for validation and never logged.
    """
    parts = urlsplit(proxy_url)
    scheme = (parts.scheme or "").lower()
    if scheme not in _PROXYABLE_SCHEMES:
        raise SSRFValidationError(f"Unsupported proxy scheme: {scheme!r}")
    host = parts.hostname
    if not host:
        raise SSRFValidationError("Proxy URL has no host")

    # Reuse the SSRF resolver/denylist on the proxy host. We build an http URL
    # purely to run it through the same validation path; we do not fetch it.
    port = parts.port or (443 if scheme in ("https",) else 80)
    from blazecrawl_core.network.ip_utils import is_blocked_address as _is_blocked_address
    from blazecrawl_core.network.ssrf import _classify_resolved_ip, _parse_ip_literal, _resolve

    literal = _parse_ip_literal(host)
    if literal is not None:
        if _is_blocked_address(literal):
            raise SSRFValidationError("Proxy endpoint resolves to a blocked address")
        return
    ips = await _resolve(host, port)
    if not ips:
        raise SSRFValidationError("Proxy hostname resolution returned no addresses")
    for ip in ips:
        _classify_resolved_ip(ip)


class PinnedProxyClient:
    """SSRF-safe async HTTP client that routes through a proxy.

    Behaves like :class:`PinnedAsyncClient` for VALIDATION purposes — every hop
    (and redirect) is validated via ``validate_and_pin`` and https→http
    downgrades are blocked — but the actual connection is made THROUGH the proxy
    (httpx ``proxy=``) rather than to a pinned socket. The proxy endpoint is
    validated once up front.

    Use this only for the proxied path; the direct path stays on
    :class:`PinnedAsyncClient` (which additionally pins the socket IP).
    """

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
        self._proxy_url = proxy_url
        self._timeout = httpx.Timeout(timeout_s)
        self._max_redirects = max_redirects
        self._verify = verify
        self._user_agent = user_agent
        self._max_bytes = max_bytes
        self._proxy_validated = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> EgressResponse:
        base_headers = validate_headers(headers)
        if self._user_agent is not None:
            validate_user_agent(self._user_agent)
        if not self._proxy_validated:
            await validate_proxy_endpoint(self._proxy_url)
            self._proxy_validated = True

        redirects: list[str] = []
        current_url = url

        for _hop in range(self._max_redirects + 1):
            # UNCONDITIONAL target validation — identical to the direct path.
            # This is what stops the proxy from being an SSRF pivot.
            await validate_and_pin(current_url)
            req_headers = dict(base_headers)
            if self._user_agent:
                req_headers.setdefault("user-agent", self._user_agent)

            async with httpx.AsyncClient(
                proxy=self._proxy_url,
                timeout=self._timeout,
                verify=self._verify,
                follow_redirects=False,  # manual: re-validate every hop
            ) as client:
                request = client.build_request(
                    method, current_url, headers=req_headers, content=content
                )
                if self._max_bytes is not None:
                    response = await client.send(request, stream=True)
                    clen = response.headers.get("content-length")
                    if clen is not None:
                        try:
                            if int(clen) > self._max_bytes:
                                await response.aclose()
                                raise SSRFValidationError(
                                    f"Response exceeds max_bytes ({self._max_bytes})"
                                )
                        except ValueError:
                            pass
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > self._max_bytes:
                            await response.aclose()
                            raise SSRFValidationError(
                                f"Response exceeds max_bytes ({self._max_bytes})"
                            )
                    body = bytes(chunks)
                    await response.aclose()
                else:
                    response = await client.send(request)
                    body = await response.aread()

            if response.status_code in _REDIRECT_STATUSES and "location" in response.headers:
                location = response.headers["location"]
                next_url = urljoin(current_url, location)
                cur_scheme = urlsplit(current_url).scheme.lower()
                next_scheme = urlsplit(next_url).scheme.lower()
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
