# Changelog

All notable changes to BlazeCrawl Core. Format follows Keep a Changelog;
versioning follows SemVer.

## [Unreleased]

### Added
- `formats: ["screenshot"]` on `POST /v1/scrape` returns a base64-encoded PNG in `data.screenshot`.
  Screenshots always render in the browser, so `render: "static"` is rejected with HTTP 422.
- `full_page` boolean on `POST /v1/scrape` (default `false`) captures the full scrollable page for screenshots.
- `SCREENSHOT_MAX_BYTES` setting (default 5 MB of raw PNG bytes). Captures over the cap are rejected with
  HTTP 413 (`error: "screenshot_too_large"`) and never return partial data. The cap also applies to cached
  screenshots. The setting must be a positive integer.
- `blazecrawl scrape --formats screenshot --output page.png` (with `--full-page` for the whole page) in the
  source-checkout CLI.
- Crawl jobs checkpoint their frontier, visited URLs, results, and counters after every page. With
  `REDIS_URL` set and reachable at startup, checkpoints go to Redis, one transaction each; otherwise they stay
  in memory. Unfinished crawls resume after a restart, so `GET /v1/crawl/{job_id}` keeps working and completed
  pages are not fetched again. Redis unreachable at startup logs a warning and falls back. (#9, #31)
- Opt-in disk checkpoints (`CRAWL_CHECKPOINTS_ENABLED=true`) under the state directory, written atomically.
  Used when Redis is not configured or is unreachable.

### Changed
- A normal shutdown suspends running crawls without marking them cancelled; they resume on the next startup.
  Only an explicit cancel is terminal.
- `GET /v1/crawl/{job_id}` also returns `max_depth`, `created_at`, and `finished_at`.

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
