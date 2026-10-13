# Changelog

All notable changes to BlazeCrawl Core. Format follows Keep a Changelog;
versioning follows SemVer.

## [Unreleased]

## [0.2.0] - 2026-10-13

### Added
- Validated, origin-scoped custom scrape `headers` and per-request `user_agent`.
  Headers apply only to the initial origin (scheme, hostname, effective port); static
  redirects permanently drop them on origin change and browser interception scopes
  redirects/subresources. Names/values, duplicates and transport-managed headers are
  checked; limits are 32 headers, 128 characters per name, 4096 per value and 8192
  wire bytes, with User-Agent capped at 200 characters. Cache keys isolate values.
- Optional `EGRESS_UPSTREAM_PROXY` for static and browser traffic using HTTP(S)
  CONNECT to locally validated target IP literals. Both endpoint and destination DNS
  sets are checked on every dial; original Host/SNI/TLS verification remain intact.
  Credentials are used only for CONNECT and redacted from errors. No SOCKS or remote
  target-DNS support; a proxy is still a trusted operator, not an isolation boundary.
- Shared crawl scheduling safeguards: per-host concurrency, token-bucket admissions and robots
  crawl-delay; bounded 429/5xx retries with Retry-After support and a finite manager-wide
  page-fetch-attempt budget. Retries preserve the checkpoint frontier; cancellation releases
  permits, and exhausted budgets fail jobs explicitly. Settings and restart scope are documented. (#27, #32)
- `formats: ["screenshot"]` on `POST /v1/scrape` returns a base64-encoded PNG in `data.screenshot`.
  Screenshots always render in the browser, so `render: "static"` is rejected with HTTP 422.
- `full_page` boolean on `POST /v1/scrape` (default `false`) captures the full scrollable page for screenshots.
- `SCREENSHOT_MAX_BYTES` setting (default 5 MB of raw PNG bytes). Captures over the cap are rejected with
  HTTP 413 (`error: "screenshot_too_large"`) and never return partial data. The cap also applies to cached
  screenshots. The setting must be a positive integer.
- `blazecrawl scrape --formats screenshot --output page.png` (with `--full-page` for the whole page).
- CLI Markdown `--output` and `doctor` readiness/key/Chromium diagnostics.
- Crawl jobs checkpoint their frontier, visited URLs, results, and counters after every page. With
  `REDIS_URL` set and reachable at startup, checkpoints go to Redis, one transaction each; otherwise they stay
  in memory. Unfinished crawls resume after a restart, so `GET /v1/crawl/{job_id}` keeps working and completed
  pages are not fetched again. Redis unreachable at startup logs a warning and falls back. (#9, #31)
- Opt-in disk checkpoints (`CRAWL_CHECKPOINTS_ENABLED=true`) under the state directory, written atomically.
  Used when Redis is not configured or is unreachable.
- Opt-in Prometheus `/metrics` endpoint via the `[metrics]` extra and
  `METRICS_ENABLED=true`, exposing actual browser-pool size, active contexts,
  lifecycle errors, and page-creation latency without URL/credential labels.
  Disabled by default with no mandatory dependency; enabling without the extra
  fails with actionable installation instructions.

### Changed
- All five distributions are versioned together at 0.2.0. Runtime, CLI, health and
  MCP initialization version reporting derive from installed package metadata.
- Canonical repository/package links now use `Danish-Sethi/blazecrawl`; the release
  image moves to `ghcr.io/danish-sethi/blazecrawl` (Linux amd64 only). PyPI/npm names
  are unchanged; the old personal GHCR image is not updated.
- Clean-installed wheel/npm-pack and container behavior checks gate every publisher.
  Read-only PR/manual candidates and post-publication public-registry checks reuse
  those checks. PyPI/npm publication stays tag-only and OIDC-only.
- First-run instructions use authenticated, loopback-only Docker with persistent
  state; native Windows requires an explicit random key. Five-person trial protocol
  is provided without participant results.
- A normal shutdown suspends running crawls without marking them cancelled; they resume on the next startup.
  Only an explicit cancel is terminal.
- `GET /v1/crawl/{job_id}` also returns `max_depth`, `created_at`, and `finished_at`.

### Fixed
- Browser-pool context/semaphore accounting on failed startup, restart,
  acquisition failure, and cancellation; page-creation failures are counted
  and release cleanup completes before a cancelled lease is returned.

### Compatibility and limits
- New request fields are additive to v0.1.2; older servers do not accept them.
  `render: "static"` with screenshot is rejected (422), not silently upgraded.
  PNGs exceeding the default 5 MB raw-byte cap fail (413), including cache hits.
- Crawl persistence is single-process restart recovery, **not** a distributed queue.
  Memory-only jobs disappear on restart. Redis/disk writes contain page data and
  must be protected. Unreachable Redis logs a warning and selects disk (if enabled)
  or memory; checkpoint write failures are logged, not a durability guarantee.
- The crawl governor is process-wide, not replica-wide or a network request quota.
  Direct scrape/map, robots fetches, redirects and subresources are outside its
  admission budget. Counters/delays reset on restart; budget exhaustion fails jobs.
- Auth, robots enforcement, SSRF/private-address blocking, redirect validation and
  pinned connections remain enabled. Browser headers fail closed without Chromium
  interception support. Metrics are opt-in and unauthenticated: restrict access.

## [0.1.2] — Distribution integrity repair

### Fixed
- Corrected BlazeCrawl Core runtime version reporting: `blazecrawl_core.__version__`,
  `blazecrawl --version`, and `/health` now derive from the installed distribution
  metadata and report the actual release version (the published `blazecrawl-core==0.1.1`
  wheel incorrectly reported `0.1.0` at runtime).
- Aligned the Node SDK and MCP `package-lock.json` versions with their package versions.
- Strengthened the release-version consistency gate to verify all manifests, both Node
  lockfiles (root and package-root), and the built core/SDK runtime versions, so this
  class of version drift cannot recur.
- Completed package distribution convergence across PyPI, npm, and GHCR.

### Changed
- npm publication is now OIDC Trusted Publishing only; the one-time v0.1.1 token
  bootstrap path was removed.

No intentional scrape/map/crawl API behavior change.

## [0.1.1] — Distribution preparation

### Changed
- Completed PyPI and npm registry metadata for the core package, SDKs, and MCP servers.
- Added a tag-only, OIDC-based release workflow for PyPI, npm provenance, and GHCR publishing.
- Added public-registry installation guidance. No intentional runtime API behavior change.

## [0.1.0] — RC1

### Added
- `/v1/scrape`, `/v1/map`, `/v1/crawl` (BFS, same-origin, robots-enforced).
- Clean Markdown extraction (readability → trafilatura → BeautifulSoup).
- Hybrid rendering: static fetch with browser fallback (Playwright).
- Security-first egress: SSRF validate + pin-at-connect, DNS-rebinding
  resistance, private/loopback/link-local/metadata blocking, redirect
  re-validation, https→http downgrade blocking, response size caps, browser
  request interception guard, WebRTC IP-leak mitigation.
- Local API-key auth (generated on first start; loopback-only disable option).
- Caching (memory default, optional Redis) and in-process crawl queue.
- Python SDK, Node SDK, CLI, Python MCP server, Node MCP server.
- Docker / docker-compose zero-config self-host.
