"""Real CONNECT/TLS traffic; only public DNS and numeric socket routing are mocked."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import ssl
import subprocess
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from blazecrawl_core.config import Settings, settings
from blazecrawl_core.engine import browser_pool
from blazecrawl_core.network import egress
from blazecrawl_core.network.browser_proxy import BrowserEgressProxy
from blazecrawl_core.network.egress import PinnedProxyClient
from blazecrawl_core.network.ssrf import SSRFValidationError
from blazecrawl_core.network.upstream_proxy import UpstreamProxy, close_writer

ORIGIN_IP = "9.9.9.9"
PROXY_IP = "8.8.8.8"
IPV6 = "2606:4700:4700::1111"


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    root = tmp_path_factory.mktemp("upstream-tls")
    cert, key = root / "cert.pem", root / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=origin.test",
            "-addext",
            f"subjectAltName=DNS:origin.test,DNS:proxy.test,IP:{IPV6}",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class Network:
    def __init__(self):
        self.answers = {
            "origin.test": [ORIGIN_IP],
            "other.test": [ORIGIN_IP],
            "wrong.test": [ORIGIN_IP],
            "proxy.test": [PROXY_IP],
        }
        self.resolutions = []
        self.dials = []
        self.connects = []
        self.origins = []
        self.bodies = []
        self.sni = []
        self.location = "http://127.0.0.1/private"
        self.proxy_response = b"HTTP/1.1 200 Connection Established\r\n\r\n"
        self.tasks = set()
        self.servers = []
        self.stall = False
        self.tunnel_ports = {}

    async def resolve(self, host, port):
        self.resolutions.append((host, port))
        if host in self.answers:
            return self.answers[host]
        return [str(ipaddress.ip_address(host))]

    @asynccontextmanager
    async def client(self, path):
        if path == "static":
            yield PinnedProxyClient(self.proxy_url, timeout_s=2)
        else:
            proxy = BrowserEgressProxy(upstream_proxy_url=self.proxy_url)
            url = await proxy.start()
            try:
                async with httpx.AsyncClient(proxy=url, follow_redirects=True, timeout=2) as client:
                    yield client
            finally:
                await proxy.close()


@pytest_asyncio.fixture
async def network(monkeypatch, certificate):
    net = Network()
    cert, key = certificate
    default_context = ssl.create_default_context

    def trusted_context(*args, **kwargs):
        ctx = default_context(*args, **kwargs)
        ctx.load_verify_locations(cafile=str(cert))
        return ctx

    net.trusted_context = trusted_context
    net.default_context = default_context
    monkeypatch.setattr(ssl, "create_default_context", trusted_context)
    monkeypatch.setattr("blazecrawl_core.network.ssrf._resolve", net.resolve)

    async def resolve(_self, host, port):
        return await net.resolve(host, port)

    monkeypatch.setattr("blazecrawl_core.network.resolver.SystemResolver.resolve", resolve)
    loop = asyncio.get_running_loop()
    original_connect = loop.sock_connect

    async def routed_connect(sock, address):
        host, port = address[:2]
        if host in {ORIGIN_IP, PROXY_IP}:
            net.dials.append((host, port))
            address = ("127.0.0.1", port)
        elif host == IPV6:
            net.dials.append((host, port))
            address = ("::1", port, 0, 0)
        await original_connect(sock, address)

    monkeypatch.setattr(loop, "sock_connect", routed_connect)

    async def origin(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            net.origins.append(header)
            headers = {}
            for line in header.split(b"\r\n")[1:-2]:
                name, _, value = line.partition(b":")
                headers[name.lower()] = value.strip()
            net.bodies.append(await reader.readexactly(int(headers.get(b"content-length", b"0"))))
            path = header.split(b" ", 2)[1]
            if path == b"/redirect":
                response = (
                    f"HTTP/1.1 302 Found\r\nLocation: {net.location}\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n"
                ).encode()
            elif path == b"/chunked":
                response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n8\r\n12345678\r\n0\r\n\r\n"
            else:
                response = (
                    b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\nupstream"
                )
            writer.write(response)
            await writer.drain()
        finally:
            await close_writer(writer)

    async def upstream(reader, writer):
        remote = None
        copies = []
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            net.connects.append(header)
            if net.stall:
                await reader.read()
                return
            if not header.startswith(b"CONNECT "):
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            target = header.split(b" ", 2)[1].decode()
            host, port = BrowserEgressProxy._authority(target)
            # The fixture refuses remote DNS; hostname CONNECT cannot pass.
            ipaddress.ip_address(host)
            writer.write(net.proxy_response)
            await writer.drain()
            if net.proxy_response != b"HTTP/1.1 200 Connection Established\r\n\r\n":
                return
            remote_reader, remote = await asyncio.open_connection(
                "127.0.0.1", net.tunnel_ports.get(port, port)
            )

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()
                destination.close()

            copies = [
                asyncio.create_task(copy(reader, remote)),
                asyncio.create_task(copy(remote_reader, writer)),
            ]
            await asyncio.gather(*copies)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            for task in copies:
                task.cancel()
            await asyncio.gather(*copies, return_exceptions=True)
            if remote is not None:
                await close_writer(remote)
            await close_writer(writer)

    async def start(handler, *, tls=False, host="127.0.0.1"):
        ctx = None
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(cert), str(key))
            ctx.sni_callback = lambda _sock, name, _ctx: net.sni.append(name)

        async def tracked(reader, writer):
            task = asyncio.current_task()
            net.tasks.add(task)
            try:
                await handler(reader, writer)
            finally:
                net.tasks.discard(task)

        server = await asyncio.start_server(tracked, host, 0, ssl=ctx)
        net.servers.append(server)
        return server.sockets[0].getsockname()[1]

    net.start_proxy = lambda **kwargs: start(upstream, **kwargs)
    net.http_port = await start(origin)
    net.https_port = await start(origin, tls=True)
    net.proxy_port = await start(upstream)
    net.proxy_url = f"http://proxy.test:{net.proxy_port}"
    try:
        yield net
    finally:
        for server in net.servers:
            server.close()
            await server.wait_closed()
        tasks = list(net.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("path", ["static", "browser"])
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_numeric_connect_preserves_host_sni_and_isolates_auth(network, path, scheme):
    net = network
    port = net.https_port if scheme == "https" else net.http_port
    net.proxy_url = f"http://operator:p%40ss@proxy.test:{net.proxy_port}"
    async with net.client(path) as client:
        response = await client.get(f"{scheme}://origin.test:{port}/")
    assert response.status_code == 200
    assert response.content == b"upstream"
    assert net.connects[0].startswith(f"CONNECT {ORIGIN_IP}:{port} HTTP/1.1\r\n".encode())
    assert b"Proxy-Authorization: Basic " + base64.b64encode(b"operator:p@ss") in net.connects[0]
    assert f"Host: origin.test:{port}\r\n".encode() in net.origins[0]
    assert b"Proxy-Authorization" not in net.origins[0]
    assert b"operator" not in net.origins[0] and b"p@ss" not in net.origins[0]
    assert ("proxy.test", net.proxy_port) in net.resolutions
    assert (PROXY_IP, net.proxy_port) in net.dials
    assert not any(host == ORIGIN_IP for host, _ in net.dials)
    if scheme == "https":
        assert net.sni == ["origin.test"]


@pytest.mark.parametrize("path", ["static", "browser"])
async def test_https_upstream_endpoint_is_pinned_and_verified(network, path):
    net = network
    port = await net.start_proxy(tls=True)
    net.proxy_url = f"https://proxy.test:{port}"
    async with net.client(path) as client:
        response = await client.get(f"https://origin.test:{net.https_port}/")
    assert response.status_code == 200
    assert (PROXY_IP, port) in net.dials
    assert net.sni == ["proxy.test", "origin.test"]


@pytest.mark.parametrize("path", ["static", "browser"])
@pytest.mark.parametrize("answers", [["127.0.0.1"], [ORIGIN_IP, "10.1.2.3"], []])
async def test_private_or_mixed_target_refused_before_upstream(network, path, answers):
    net = network
    net.answers["origin.test"] = answers
    async with net.client(path) as client:
        if path == "static":
            with pytest.raises(SSRFValidationError):
                await client.get(f"http://origin.test:{net.http_port}/")
        else:
            response = await client.get(f"http://origin.test:{net.http_port}/")
            assert response.status_code == 502
    assert not net.connects and not net.dials and not net.origins


@pytest.mark.parametrize("path", ["static", "browser"])
async def test_private_redirect_refused_before_second_upstream_connection(network, path):
    net = network
    async with net.client(path) as client:
        if path == "static":
            with pytest.raises(SSRFValidationError):
                await client.get(f"http://origin.test:{net.http_port}/redirect")
        else:
            response = await client.get(f"http://origin.test:{net.http_port}/redirect")
            assert response.status_code == 502
    assert len(net.connects) == 1 and len(net.origins) == 1


@pytest.mark.parametrize("path", ["static", "browser"])
async def test_endpoint_rebinding_is_rejected_on_every_dial(network, path):
    net = network
    async with net.client(path) as client:
        assert (await client.get(f"http://origin.test:{net.http_port}/")).status_code == 200
        net.answers["proxy.test"] = ["127.0.0.1"]
        if path == "static":
            with pytest.raises(SSRFValidationError, match="Upstream proxy"):
                await client.get(f"http://origin.test:{net.http_port}/")
        else:
            assert (await client.get(f"http://origin.test:{net.http_port}/")).status_code == 502
    assert len(net.connects) == 1 and len(net.origins) == 1
    assert sum(host == "proxy.test" for host, _ in net.resolutions) == 2


@pytest.mark.parametrize(
    "url",
    [
        "socks5://proxy.test:1080",
        "socks5h://proxy.test:1080",
        "ftp://proxy.test",
        "http://proxy.test/path",
        "http://proxy.test?query",
        "http://proxy.test#fragment",
        "http://proxy.test:0",
        "http://proxy.test:65536",
        "http://proxy.test:nope",
        "http://proxy.test:",
        "http://[",
        "http://",
        "",
        "http://user@proxy.test",
        "http://:password@proxy.test",
        "http://user:@proxy.test",
        "http://user:password@proxy.test@other.test",
        "http://us%3Aer:password@proxy.test",
        "http://user:p%0Assword@proxy.test",
        "http://user:pass%zz@proxy.test",
        "http://proxy.test\\evil",
        " http://proxy.test",
        "http://proxy.test\n",
    ],
)
def test_invalid_configuration_is_redacted_and_fails_closed(monkeypatch, url):
    monkeypatch.setenv("EGRESS_UPSTREAM_PROXY", url)
    with pytest.raises(SSRFValidationError) as exc:
        Settings()
    assert "password" not in str(exc.value)
    assert "password" not in repr(exc.value.__cause__)
    assert str(exc.value) == "Invalid upstream proxy configuration (HTTP/HTTPS required)"


def test_default_is_direct_and_credentials_are_not_in_repr(monkeypatch):
    monkeypatch.delenv("EGRESS_UPSTREAM_PROXY", raising=False)
    assert Settings().EGRESS_UPSTREAM_PROXY is None
    proxy = UpstreamProxy.parse("http://user:password@proxy.test")
    assert "password" not in repr(proxy) and "Basic" not in repr(proxy)
    with pytest.raises(SSRFValidationError):
        PinnedProxyClient("http://proxy.test", verify=False)


@pytest.mark.parametrize(
    "endpoint",
    [
        ["127.0.0.1"],
        [PROXY_IP, "10.0.0.1"],
        [],
    ],
)
async def test_private_mixed_or_empty_endpoint_refused_before_dial(network, endpoint):
    net = network
    net.answers["proxy.test"] = endpoint
    with pytest.raises(SSRFValidationError):
        await PinnedProxyClient(net.proxy_url).get(f"http://origin.test:{net.http_port}/")
    assert not net.connects and not net.dials and not net.origins


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_ipv6_target_authority_and_host_header(network, scheme):
    net = network
    port = net.http_port if scheme == "http" else net.https_port
    response = await PinnedProxyClient(net.proxy_url).get(f"{scheme}://[{IPV6}]:{port}/")
    assert response.status_code == 200
    assert net.connects[0].startswith(f"CONNECT [{IPV6}]:{port} ".encode())
    assert f"Host: [{IPV6}]:{port}\r\n".encode() in net.origins[0]


async def test_ipv6_endpoint_is_numerically_dialed(network):
    net = network
    port = await net.start_proxy(host="::1")
    net.answers["proxy.test"] = [IPV6]
    net.proxy_url = f"http://proxy.test:{port}"
    assert (
        await PinnedProxyClient(net.proxy_url).get(f"http://origin.test:{net.http_port}/")
    ).status_code == 200
    assert (IPV6, port) in net.dials


@pytest.mark.parametrize("path", ["/", "/chunked"])
async def test_streamed_body_cap_is_preserved(network, path):
    net = network
    with pytest.raises(SSRFValidationError, match="max_bytes"):
        await PinnedProxyClient(net.proxy_url, max_bytes=4).get(
            f"http://origin.test:{net.http_port}{path}"
        )


async def test_https_downgrade_is_refused(network):
    net = network
    net.location = f"http://origin.test:{net.http_port}/"
    with pytest.raises(SSRFValidationError, match="downgrade"):
        await PinnedProxyClient(net.proxy_url).get(f"https://origin.test:{net.https_port}/redirect")
    assert len(net.connects) == 1


@pytest.mark.parametrize("upstream", [False, True])
async def test_bad_destination_certificate_rejected(network, monkeypatch, upstream):
    net = network
    if not upstream:
        monkeypatch.setattr(ssl, "create_default_context", net.default_context)
    host = "wrong.test" if upstream else "origin.test"
    with pytest.raises(httpx.ConnectError):
        await PinnedProxyClient(net.proxy_url).get(f"https://{host}:{net.https_port}/")
    assert not net.origins


@pytest.mark.parametrize("trusted", [True, False])
async def test_bad_upstream_certificate_rejected_without_credentials_in_logs(
    network, capsys, monkeypatch, trusted
):
    net = network
    if not trusted:
        monkeypatch.setattr(ssl, "create_default_context", net.default_context)
    port = await net.start_proxy(tls=True)
    net.answers["wrong.test"] = [PROXY_IP]
    with pytest.raises(SSRFValidationError, match="Upstream proxy") as exc:
        await PinnedProxyClient(f"https://user:password@wrong.test:{port}").get(
            f"http://origin.test:{net.http_port}/"
        )
    assert "password" not in str(exc.value)
    captured = capsys.readouterr()
    assert "password" not in captured.out + captured.err
    assert not net.connects and not net.origins


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n",
        b"garbage\r\n\r\n",
        b"HTTP/1.1 200 OK\r\ninvalid header\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nX: " + b"x" * 65536 + b"\r\n\r\n",
    ],
    ids=["auth-required", "bad-status", "bad-header", "oversized-header"],
)
async def test_refused_malformed_or_oversized_connect_fails_closed(network, response):
    net = network
    net.proxy_response = response
    with pytest.raises(SSRFValidationError, match="Upstream proxy"):
        await PinnedProxyClient(net.proxy_url).get(f"http://origin.test:{net.http_port}/")
    assert not net.origins


@pytest.mark.parametrize("cancel", [False, True])
async def test_stalled_connect_times_out_or_cancels_and_closes_socket(network, cancel):
    net = network
    net.stall = True
    proxy = UpstreamProxy.parse(net.proxy_url)
    task = asyncio.create_task(
        proxy.connect(
            ORIGIN_IP,
            net.http_port,
            connect_timeout=1,
            read_timeout=0.1 if not cancel else 10,
        )
    )
    if cancel:
        for _ in range(100):
            if net.connects:
                break
            await asyncio.sleep(0.01)
        assert net.connects
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(SSRFValidationError):
            await task
    await asyncio.sleep(0.01)
    assert not net.tasks and not net.origins


async def test_safe_fetch_configured_and_unconfigured_paths(network, monkeypatch):
    net = network
    monkeypatch.setattr(settings, "EGRESS_UPSTREAM_PROXY", net.proxy_url)
    response = await egress.safe_fetch(f"http://origin.test:{net.http_port}/")
    assert response.status_code == 200 and len(net.connects) == 1
    monkeypatch.setattr(settings, "EGRESS_UPSTREAM_PROXY", None)
    calls = []

    async def respond(request):
        calls.append(request)
        return httpx.Response(200, content=b"direct")

    monkeypatch.setattr(egress, "_pinned_transport", lambda *a, **kw: httpx.MockTransport(respond))
    response = await egress.safe_fetch("https://origin.test/")
    assert response.content == b"direct" and len(net.connects) == 1
    assert calls[0].url.host == ORIGIN_IP
    assert calls[0].headers["host"] == "origin.test"
    assert calls[0].extensions["sni_hostname"] == "origin.test"


async def test_static_rebinding_at_final_socket_boundary_is_refused(network, monkeypatch):
    net = network
    resolve = net.resolve
    target_calls = 0

    async def rebind(host, port):
        nonlocal target_calls
        if host == "origin.test":
            target_calls += 1
            return [ORIGIN_IP] if target_calls == 1 else ["127.0.0.1"]
        return await resolve(host, port)

    monkeypatch.setattr("blazecrawl_core.network.ssrf._resolve", rebind)

    async def boundary_resolve(_self, host, port):
        return await rebind(host, port)

    monkeypatch.setattr("blazecrawl_core.network.resolver.SystemResolver.resolve", boundary_resolve)
    with pytest.raises(SSRFValidationError):
        await PinnedProxyClient(net.proxy_url).get(f"http://origin.test:{net.http_port}/")
    assert target_calls == 2 and not net.connects and not net.dials


async def test_endpoint_rebinding_during_one_redirect_chain_is_refused(network, monkeypatch):
    net = network
    resolve = net.resolve
    proxy_calls = 0
    net.location = f"http://origin.test:{net.http_port}/"

    async def rebind(host, port):
        nonlocal proxy_calls
        if host == "proxy.test":
            proxy_calls += 1
            return [PROXY_IP] if proxy_calls == 1 else ["127.0.0.1"]
        return await resolve(host, port)

    monkeypatch.setattr("blazecrawl_core.network.ssrf._resolve", rebind)
    with pytest.raises(SSRFValidationError):
        await PinnedProxyClient(net.proxy_url).get(f"http://origin.test:{net.http_port}/redirect")
    assert proxy_calls == 2 and len(net.connects) == 1 and len(net.origins) == 1


async def test_browser_proxy_unconfigured_path_remains_direct(network):
    net = network
    proxy = BrowserEgressProxy()
    url = await proxy.start()
    try:
        async with httpx.AsyncClient(proxy=url) as client:
            response = await client.get(f"http://origin.test:{net.http_port}/")
        assert response.status_code == 200
        assert (ORIGIN_IP, net.http_port) in net.dials
        assert not net.connects
    finally:
        await proxy.close()


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_static_post_body_is_forwarded_without_proxy_header_terminator_bytes(network, scheme):
    net = network
    port = net.http_port if scheme == "http" else net.https_port
    response = await PinnedProxyClient(net.proxy_url).request(
        "POST", f"{scheme}://origin.test:{port}/", content=b"payload"
    )
    assert response.status_code == 200 and net.bodies == [b"payload"]


@pytest.mark.parametrize(
    ("scheme", "port", "expected_host"),
    [
        ("http", 80, "origin.test"),
        ("http", 443, "origin.test:443"),
        ("https", 443, "origin.test"),
        ("https", 80, "origin.test:80"),
    ],
)
async def test_origin_host_port_is_scheme_aware_through_connect(
    network, scheme, port, expected_host
):
    net = network
    net.tunnel_ports[port] = net.https_port if scheme == "https" else net.http_port
    response = await PinnedProxyClient(net.proxy_url).get(f"{scheme}://origin.test:{port}/")
    assert response.status_code == 200
    assert f"Host: {expected_host}\r\n".encode() in net.origins[0]
    assert net.connects[0].startswith(f"CONNECT {ORIGIN_IP}:{port} ".encode())


@pytest.mark.parametrize("same_origin", [False, True])
async def test_real_upstream_static_redirect_preserves_custom_header_scope(network, same_origin):
    net = network
    host = "origin.test" if same_origin else "other.test"
    net.location = f"http://{host}:{net.http_port}/"
    response = await PinnedProxyClient(net.proxy_url, user_agent="Baseline/1.0").get(
        f"http://origin.test:{net.http_port}/redirect",
        headers={
            "Authorization": "Bearer fixture-only",
            "Cookie": "fixture=only",
            "X-Custom-Token": "fixture-token",
            "User-Agent": "Custom/1.0",
        },
    )
    assert response.status_code == 200 and len(net.origins) == 2
    for name, value in (
        (b"authorization", b"Bearer fixture-only"),
        (b"cookie", b"fixture=only"),
        (b"x-custom-token", b"fixture-token"),
    ):
        assert name + b": " + value in net.origins[0]
        assert (name + b": " + value in net.origins[1]) == same_origin
    expected_agent = b"Custom/1.0" if same_origin else b"Baseline/1.0"
    assert b"user-agent: " + expected_agent in net.origins[1]


@pytest.mark.parametrize("redirect", [False, True])
async def test_real_browser_upstream_cdp_header_scope_and_screenshot(network, redirect):
    from playwright.async_api import async_playwright

    net = network
    net.location = f"http://other.test:{net.http_port}/"
    proxy = BrowserEgressProxy(upstream_proxy_url=net.proxy_url)
    proxy_url = await proxy.start()
    url = f"http://origin.test:{net.http_port}" + ("/redirect" if redirect else "/")
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                proxy={"server": proxy_url, "bypass": ""},
            )
            try:
                page = await browser.new_page()
                await page.route("**/favicon.ico", lambda route: route.abort())
                await page.set_extra_http_headers({"User-Agent": "Baseline/1.0"})
                await egress.install_browser_ssrf_guard(
                    page,
                    initial_url=url,
                    user_agent="Baseline/1.0",
                    headers={
                        "Authorization": "Bearer browser-fixture",
                        "X-Custom-Token": "fixture-token",
                        "User-Agent": "Custom/1.0",
                    },
                )
                response = await page.goto(url)
                assert response is not None and response.status == 200
                png = await browser_pool.take_screenshot(page, full_page=True, max_bytes=512 * 1024)
                assert png.startswith(b"\x89PNG\r\n\x1a\n")
            finally:
                await browser.close()
        first = net.origins[0].lower()
        assert b"authorization: bearer browser-fixture\r\n" in first
        assert b"x-custom-token: fixture-token\r\n" in first
        if redirect:
            assert len(net.origins) == 2
            second = net.origins[1].lower()
            assert b"authorization:" not in second
            assert b"x-custom-token:" not in second
            assert b"user-agent: baseline/1.0\r\n" in second
        assert all(
            h.startswith(f"CONNECT {ORIGIN_IP}:{net.http_port} ".encode()) for h in net.connects
        )
    finally:
        await proxy.close()


async def test_browser_pool_keeps_local_proxy_with_configured_upstream(network, monkeypatch):
    net = network
    monkeypatch.setattr(settings, "EGRESS_UPSTREAM_PROXY", net.proxy_url)
    monkeypatch.setattr(browser_pool.BrowserPool, "_instance", None)
    monkeypatch.setattr(browser_pool.BrowserPool, "_lock", None)
    pool = browser_pool.BrowserPool(pool_size=1)
    browser = AsyncMock()
    playwright = AsyncMock()
    playwright.chromium.launch.return_value = browser
    manager = AsyncMock()
    manager.start.return_value = playwright
    monkeypatch.setattr(browser_pool, "async_playwright", lambda: manager)
    monkeypatch.setattr(pool, "_create_context", AsyncMock())
    await pool.startup()
    try:
        launch = playwright.chromium.launch.call_args.kwargs
        assert launch["proxy"]["bypass"] == ""
        assert launch["proxy"]["server"].startswith("http://127.0.0.1:")
        assert net.proxy_url not in str(launch)
        async with httpx.AsyncClient(proxy=launch["proxy"]["server"]) as client:
            assert (await client.get(f"http://origin.test:{net.http_port}/")).status_code == 200
        assert net.connects[0].startswith(f"CONNECT {ORIGIN_IP}:{net.http_port} ".encode())
    finally:
        await pool.shutdown()


async def test_real_chromium_uses_local_validating_upstream_boundary(network):
    from playwright.async_api import async_playwright

    net = network
    proxy = BrowserEgressProxy(upstream_proxy_url=net.proxy_url)
    url = await proxy.start()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                proxy={"server": url, "bypass": ""},
            )
            try:
                page = await browser.new_page()
                await page.route("**/favicon.ico", lambda route: route.abort())
                response = await page.goto(f"http://origin.test:{net.http_port}/")
                assert response is not None and response.status == 200
                assert await page.text_content("body") == "upstream"
                net.answers["origin.test"] = [ORIGIN_IP, "127.0.0.1"]
                count = len(net.connects)
                blocked = await page.goto(f"http://origin.test:{net.http_port}/blocked")
                assert blocked is not None and blocked.status == 502
                assert len(net.connects) == count
            finally:
                await browser.close()
        assert f"CONNECT {ORIGIN_IP}:{net.http_port} ".encode() in net.connects[0]
        assert f"Host: origin.test:{net.http_port}\r\n".encode() in net.origins[0]
    finally:
        await proxy.close()
