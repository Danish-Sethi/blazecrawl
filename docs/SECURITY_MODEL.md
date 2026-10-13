# Security Model

BlazeCrawl Core treats **every user-supplied URL as hostile**. This document
describes the threats and the controls.

## Threats

* **SSRF** — a request to `http://169.254.169.254/...` or `http://10.x/...`
  reaching internal/cloud infrastructure.
* **DNS rebinding / TOCTOU** — DNS returns a public IP at validation time and a
  private IP at connection time.
* **Redirect pivot** — a public URL 302s to an internal address.
* **Protocol downgrade** — `https` → `http` redirect exposing content.
* **Browser-path bypass** — Chromium does its own DNS, bypassing HTTP pinning.
* **Bombs** — oversized or compressed responses exhausting memory.
* **Parser confusion** — userinfo URLs, legacy IP encodings, IPv4-mapped IPv6.

## Browser egress boundary (WP1B/WP2C/WP3-proven)

For browser rendering, Chromium is launched with an explicit loopback HTTP proxy
and an empty bypass list. Every absolute HTTP request and HTTPS CONNECT is
resolved by `network/browser_proxy.py`; all returned addresses are validated
before the proxy opens a socket to the selected IP. The original hostname remains
in the HTTP authority and CONNECT tunnel, so Chromium performs normal SNI and
certificate validation — BlazeCrawl never terminates or inspects TLS (no MITM).
Redirects and subresources therefore re-enter the same boundary. Proxy failures
are fail-closed.

```text
Chromium
→ BrowserEgressProxy (loopback, empty bypass list)
→ Resolver
→ validate complete IP set (all A/AAAA records)
→ numeric socket connection (connect to validated IP only)
```

> Invariant: for every browser-originated outbound request supported by
> BlazeCrawl, the destination socket may connect only to an IP address that has
> passed prohibited-address validation for the corresponding hostname at
> connection time.

### Browser primitives — v0.1 policy

| Primitive               | v0.1 Policy                                  |
| ----------------------- | -------------------------------------------- |
| HTTP navigation         | Supported / governed (egress boundary)        |
| HTTPS navigation        | Supported / governed (CONNECT, no MITM)       |
| iframe                  | Supported / governed                          |
| image                   | Supported / governed                          |
| script                  | Supported / governed                          |
| CSS/resources           | Supported / governed                          |
| fetch                   | Supported / governed                          |
| XHR                     | Supported / governed                          |
| EventSource             | Supported / governed                          |
| popup                   | Supported / governed                          |
| WebSocket               | Blocked                                       |
| Worker                  | Blocked                                       |
| SharedWorker            | Blocked                                       |
| ServiceWorker           | Blocked                                       |
| WebRTC unrestricted UDP | Prevented/constrained (`disable_non_proxied_udp`) |

"Governed" means the request traverses the browser egress proxy and is subject
to the same hostname/IP validation invariant as top-level navigation. "Blocked"
means the primitive is denied by request interception; these regressions are
covered by `tests/security/test_browser_subresource_isolation.py`.


| Threat | Control | Where |
|---|---|---|
| SSRF | Resolve once, validate EVERY A/AAAA record against the private/reserved denylist; fail closed | `network/ssrf.py` |
| DNS rebinding | Pin the connection to the single validated IP (`sni_hostname` preserves TLS); callers never re-resolve | `network/egress.py` |
| Redirect pivot | Manual redirect following; each hop re-validated + re-pinned | `network/egress.py` |
| Downgrade | `https→http` redirects blocked | `network/egress.py` |
| Browser bypass | Playwright route guard re-validates every request incl. sub-resources + popups | `network/egress.py`, `engine/browser_pool.py` |
| Bombs | `MAX_RESPONSE_BYTES` streaming cap, per-hop timeouts | `network/egress.py`, `config.py` |
| Parser confusion | Reject userinfo URLs, legacy/hex/decimal/octal IPs, IPv4-mapped IPv6, non-http(s) schemes | `network/ip_utils.py`, `network/ssrf.py` |
| robots abuse | robots.txt enforced; not configurable-off in OSS | `engine/robots.py` |
| WebRTC IP leak | Chromium `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` | `engine/browser_pool.py` |

## Authentication

The API requires a bearer key by default. If `BLAZECRAWL_API_KEY` is unset, a
cryptographically secure key is generated on first start, written to the state
directory (`$BLAZECRAWL_STATE_DIR`, default `~/.local/state/blazecrawl`; inside
Docker `/data/blazecrawl` on a volume) as `api_key` with mode `0600`, and shown
once in the first-run output. Restarts reuse the persisted key silently — the
raw key is not re-printed. Explicitly set `BLAZECRAWL_API_KEY` to use your own
key; it is never persisted or printed. Auth can be disabled only when bound to
loopback with `BLAZECRAWL_AUTH_DISABLED=true` — it is **refused on a routable
bind** so the engine cannot silently become an open proxy.

## Container hardening

The reference `docker-compose.yml` runs both services non-root with
`no-new-privileges`, `cap_drop: ALL`, a read-only root filesystem, and
`tmpfs` for `/tmp` and `/dev/shm`. Only the API-key state directory and the
Redis data directory are writable (volumes). The API publishes to
`127.0.0.1` only; Redis has no host-published port. Note: Chromium currently
runs with `--no-sandbox` because the intended container runtime does not
guarantee unprivileged user namespaces; BlazeCrawl itself is **not** a
security sandbox — isolate untrusted crawling workloads with containers/VMs
as appropriate for your threat model.

When `REDIS_URL` is set, crawl checkpoints (page URLs, titles, and Markdown) are written to Redis. When
`CRAWL_CHECKPOINTS_ENABLED=true`, they are written to `<state dir>/crawl-checkpoints`, with directories at
mode 0700 and files at mode 0600 on POSIX. Keep Redis on the internal network only, as the reference compose
file does, and protect the checkpoint directory like the API-key state directory.

## Reporting

See [../SECURITY.md](../SECURITY.md).
