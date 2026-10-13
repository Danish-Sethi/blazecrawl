# Changelog

All notable changes to BlazeCrawl Core. Format follows Keep a Changelog;
versioning follows SemVer.

## [Unreleased]

### Added
- Crawl job persistence. When `REDIS_URL` is set and reachable at startup, crawl job state is stored in
  Redis, so `GET /v1/crawl/{job_id}` keeps returning jobs after an API restart. If Redis is unreachable,
  the API logs a warning and falls back to the in-memory store.

### Changed
- Crawl jobs that were `queued` or `running` when the API stopped are marked `failed` with
  `error: "interrupted by restart"` on the next startup. Crawls are not resumed.
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
