# Contribution Backlog

A genuine, well-scoped backlog. Each item is independently mergeable and
testable. Claim an unfinished item by commenting on its existing issue; create
one from this spec only if it is not already tracked. Completed items are marked below.

Difficulty legend: 🟢 good-first-issue · 🟡 intermediate · 🔴 advanced.

---

## Good First Issues (15)

### GFI-1 🟢 Markdown table fidelity tests
- **Problem:** `markdown_converter` handles prose well but HTML tables can lose structure.
- **Files:** `blazecrawl_core/engine/markdown_converter.py`, `tests/`
- **Desired:** Fixture HTML with tables → assert GitHub-flavored Markdown tables.
- **Acceptance:** new tests pass; complex nested tables documented as a known limit.
- **Tests:** parametrized table fixtures.

### GFI-2 🟢 URL normalizer property tests
- **Problem:** `url_normalizer` needs adversarial coverage (default ports, fragments, tracking params, case).
- **Files:** `engine/url_normalizer.py`, `tests/test_engine.py`
- **Acceptance:** property/edge tests for idempotence + canonical form.

### GFI-3 🟢 `blazecrawl doctor` CLI command
- **Problem:** No single command to verify a local install (Playwright present, server reachable, key valid).
- **Files:** `blazecrawl_core/cli.py`
- **Acceptance:** `blazecrawl doctor` checks server `/ready` + Chromium presence, prints a clear report, exit code 0/1.

### GFI-4 🟢 Example: scrape → save Markdown to file
- **Files:** `examples/`
- **Acceptance:** runnable `examples/scrape_to_file.py` + README note.

### GFI-5 🟢 Example: crawl with progress to JSONL
- **Files:** `examples/`
- **Acceptance:** `examples/crawl_to_jsonl.py` streams crawl pages to a `.jsonl`.

### GFI-6 🟢 Compatibility matrix doc
- **Files:** `docs/`
- **Acceptance:** `docs/COMPATIBILITY.md` listing tested OS / Python / Node versions.

### GFI-7 🟢 Cache TTL edge-case tests
- **Files:** `engine/cache.py`, `tests/`
- **Acceptance:** tests for expiry, bust, and memory/redis parity (memory backend).

### GFI-8 🟢 robots.txt edge-case tests
- **Files:** `engine/robots.py`, `tests/`
- **Acceptance:** tests for `Disallow`, wildcard, crawl-delay presence, unreachable-robots fallback.

### GFI-9 🟢 Python SDK retry/backoff on 429/5xx
- **Files:** `sdks/python/blazecrawl/client.py`
- **Acceptance:** bounded exponential backoff with jitter; unit tests with stub transport.

### GFI-10 🟢 Node SDK README quickstart expansion
- **Files:** `sdks/node/README.md`
- **Acceptance:** scrape/map/crawl examples + error-handling section.

### GFI-11 🟢 MCP tool docstrings → schema descriptions
- **Files:** `mcp/python/blazecrawl_mcp/server.py`, `mcp/node/src/index.js`
- **Acceptance:** every tool/field has a precise `description`; snapshot test.

### GFI-12 🟢 CLI `--output` flag
- **Files:** `blazecrawl_core/cli.py`
- **Acceptance:** `blazecrawl scrape URL --output page.md` writes Markdown; test.

### GFI-13 🟢 Health endpoint content test
- **Files:** `tests/test_api.py`
- **Acceptance:** assert `/health` includes version + mode; `/ready` includes browser/cache/queue keys.

### GFI-14 🟢 Error-response contract test
- **Files:** `tests/test_api.py`
- **Acceptance:** all error paths return `{detail: {error, message}}` with correct status codes (400/401/404/422).

### GFI-15 🟢 Typos-to-dead-code sweep of `engine/`
- **Files:** `blazecrawl_core/engine/`
- **Acceptance:** remove genuinely dead branches; no behavior change; tests stay green. (Real cleanup, not filler.)

---

## Intermediate Issues (15)

### INT-1 🟡 Pluggable search-provider interface
- **Files:** new `engine/search/`
- **Acceptance:** `SearchProvider` protocol + registry; no provider bundled; interface documented.

### INT-2 🟡 Pluggable LLM-extraction provider interface
- **Files:** new `engine/extraction/`
- **Acceptance:** provider protocol for schema-guided extraction; reference no-op + docs; no cloud dep.

### INT-3 🟡 Redis-backed crawl queue backend
- **Files:** `engine/crawler.py`, new `engine/queue/redis_backend.py`
- **Status:** not implemented; durable single-process job storage in #65 does not provide distributed execution or worker leases.
- **Acceptance:** `CRAWL_QUEUE_BACKEND=redis` runs jobs across processes; parity tests vs memory.

### INT-4 🟡 Screenshot output format
- **Files:** `engine/scraper.py`, `engine/browser_pool.py`, `api/schemas.py`
- **Status:** implemented in [#64](https://github.com/Danish-Sethi/blazecrawl/pull/64), closing #26; API and CLI support viewport/full-page PNG capture with a size cap.
- **Acceptance:** `formats:["screenshot"]` returns base64 PNG (full_page option); size-capped; test.

### INT-5 🟡 PDF parsing with guards
- **Files:** new `engine/documents/pdf.py`
- **Acceptance:** parse text from PDF responses; hard caps on size/pages; decompression-bomb test.

### INT-6 🟡 Local filesystem export (JSONL/Markdown)
- **Files:** new `engine/export/`
- **Acceptance:** crawl results exportable to a directory; test round-trip.

### INT-7 🟡 Browser-pool Prometheus metrics
- **Files:** `engine/browser_pool.py`, `api/main.py`
- **Status:** implemented in [#69](https://github.com/Danish-Sethi/blazecrawl/pull/69), closing #29; optional extra and explicit enablement, off by default.
- **Acceptance:** `/metrics` exposes pool size, active, errors, page-creation latency; optional `[metrics]` extra.

### INT-8 🟡 Crawl checkpoint/resume
- **Files:** `engine/crawler.py`
- **Status:** implemented in [#65](https://github.com/Danish-Sethi/blazecrawl/pull/65), closing #9 and #31; per-page Redis/disk checkpoints resume without re-fetching completed pages.
- **Acceptance:** interrupted crawl can resume from persisted frontier (memory→disk snapshot); test.

### INT-9 🟡 Per-host crawl-delay + concurrency limiting
- **Files:** `engine/crawler.py`, `engine/robots.py`
- **Status:** implemented in [#71](https://github.com/Danish-Sethi/blazecrawl/pull/71), closing #27; host scheduling is shared across concurrent crawl jobs.
- **Acceptance:** honor robots `crawl-delay`; per-host semaphore; test.

### INT-10 🟡 Static-vs-browser routing heuristic tests
- **Files:** `engine/scraper.py`, `tests/`
- **Acceptance:** fixtures that force each path; assert the chosen `render` and content quality.

### INT-11 🟡 Python SDK async crawl helper
- **Files:** `sdks/python/blazecrawl/client.py`
- **Acceptance:** `acrawl(..., wait=True)` with async polling; tests.

### INT-12 🟡 Node SDK typed errors + retry
- **Files:** `sdks/node/src/index.js`
- **Acceptance:** distinct error classes; bounded retry on 429/5xx; node:test coverage.

### INT-13 🟢🟡 MCP resource caching layer
- **Files:** `mcp/python`, `mcp/node`
- **Acceptance:** repeated `scrape` of same URL within TTL returns cached result; test.

### INT-14 🟡 Webhook delivery on crawl completion (signed)
- **Files:** `api/main.py`, new `engine/webhooks.py`
- **Acceptance:** optional `webhook_url` on crawl; HMAC-signed POST; SSRF-validated target; test.

### INT-15 🟡 Extraction schema validation (JSON-Schema)
- **Files:** `engine/content_extractor.py`
- **Acceptance:** optional structured extraction validated against a caller JSON-Schema; clear errors.

---

## Advanced Issues (10)

### ADV-1 🔴 Egress proxy support (SSRF-preserving)
- **Files:** `network/egress.py`, `engine/browser_pool.py`
- **Status:** implemented in [#70](https://github.com/Danish-Sethi/blazecrawl/pull/70), closing #30; static/browser CONNECT targets and proxy sockets are pinned to locally validated IPs.
- **Acceptance:** optional user-supplied upstream proxy; proxy endpoint itself validated; target validation unconditional; tests prove no SSRF pivot.

### ADV-2 🔴 Plugin/extractor registry
- **Files:** new `engine/plugins/`
- **Acceptance:** third-party content extractors register via entry-points; sandboxed; security review doc.

### ADV-3 🔴 Hybrid-rendering decision model
- **Files:** `engine/scraper.py`
- **Acceptance:** learnable/heuristic routing (content-thin, JS-framework markers) with benchmark methodology; no regression on static path.

### ADV-4 🔴 Browser sandbox hardening profile
- **Files:** `engine/browser_pool.py`, `deploy/docker/`
- **Acceptance:** document + enforce seccomp/no-sandbox tradeoffs; container runs least-privilege; security tests.

### ADV-5 🔴 Change detection / diffing
- **Files:** new `engine/diff/`
- **Acceptance:** content-hash + semantic diff between scrapes; API surface; tests.

### ADV-6 🔴 Distributed crawl coordination
- **Files:** `engine/queue/`, `engine/crawler.py`
- **Acceptance:** multi-worker lease/fencing so a page is crawled once; failure recovery; integration test.

### ADV-7 🔴 Adaptive politeness / rate governance
- **Files:** `engine/crawler.py`
- **Status:** implemented in [#71](https://github.com/Danish-Sethi/blazecrawl/pull/71), closing #32; bounded retries, Retry-After, and fetch-attempt accounting include browser fallback.
- **Acceptance:** token-bucket per host, backoff on 429/5xx, global budget; tests.

### ADV-8 🔴 Structured-data (JSON-LD/microdata) hardening
- **Files:** `engine/content_extractor.py` (`[structured]` extra)
- **Acceptance:** robust extruct integration, malformed-input fuzz tests, size caps.

### ADV-9 🔴 Screenshot/PDF rendering pipeline isolation
- **Files:** `engine/browser_pool.py`
- **Acceptance:** dedicated context lifecycle for binary capture; resource limits; tests.

### ADV-10 🔴 Reproducible performance harness
- **Files:** `tests/perf/`, `docs/BENCHMARKS.md`
- **Acceptance:** scripted, CI-runnable harness producing the published baseline; methodology doc enforced.
