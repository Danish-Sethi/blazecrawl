# Architecture

BlazeCrawl Core is a single-node, self-hostable web-data engine.

```
                          ┌──────────────────────────────────────┐
   SDK (py/node) ──┐      │  api/  FastAPI                       │
   CLI ────────────┼─────▶│   GET  /health  /ready               │
   MCP (py/node) ──┘      │   POST /v1/scrape /v1/map /v1/crawl  │
                          │   auth: local API key (api/auth.py)  │
                          └───────┬──────────────────────────────┘
                                  │
                ┌─────────────────▼─────────────────┐
                │  network/ (the security boundary) │
                │  ssrf.py    validate_and_pin()    │
                │  egress.py  PinnedAsyncClient +   │
                │             browser request guard │
                │  ip_utils   private/reserved list │
                └─────────────────▲─────────────────┘
                                  │ every outbound fetch
        ┌─────────────────────────┼──────────────────────────┐
        │                         │                          │
┌───────▼───────┐        ┌────────▼────────┐        ┌────────▼────────┐
│ engine/       │        │ engine/         │        │ engine/         │
│ scraper.py    │        │ crawler.py      │        │ mapper.py       │
│ static|browser│        │ BFS same-origin │        │ sitemap +       │
│ hybrid        │        │ robots-enforced │        │ link graph      │
└───────┬───────┘        └────────┬────────┘        └────────┬────────┘
        │                         │                          │
        └─────────────┬───────────┴──────────────────────────┘
                      │
        ┌─────────────▼──────────────┐
        │ engine/content_extractor   │  readability → trafilatura → bs4
        │ engine/markdown_converter  │  HTML → Markdown
        │ engine/browser_pool        │  Playwright contexts (hardened)
        │ engine/cache               │  memory (default) | redis
        └────────────────────────────┘
```

## Design principles

1. **Single security boundary.** All network egress flows through
   `network/egress.py`. Nothing else may open a socket to a user-supplied URL.
2. **Zero mandatory cloud.** The base install needs no GCP/Stripe/Vertex/etc.
   Optional extras (`[redis]`, `[structured]`, `[metrics]`) add integrations.
3. **Fail closed.** Invalid URLs, blocked addresses, and resolution errors
   abort before any IO.
4. **Self-host first.** One `docker compose up` reaches a healthy, useful API.

## Hybrid rendering

`scraper.scrape(render="auto")` first attempts a cheap SSRF-pinned static HTTP
fetch. If the extracted main content is too thin (JS-heavy page), it retries
with the headless-browser path, which is also egress-guarded per request.

## Crawl queue and job persistence

Crawls run as in-process tasks on a single node. After every fetched page, a job checkpoints its state
(status, counters, settings, frontier, visited URLs) and the new page records to a job store. The state
write is the commit point.

- `memory` (default): process-local. Nothing survives a process exit.
- `redis`: used when `REDIS_URL` is set and reachable at startup (requires the `redis` extra,
  `pip install blazecrawl-core[redis]`). Each checkpoint is one Redis transaction. Keys are
  `bc:crawl:job:<job_id>` (state) and `bc:crawl:pages:<job_id>` (page records), with no expiry; delete them
  to purge.
- `disk`: opt-in with `CRAWL_CHECKPOINTS_ENABLED=true`, stored under `<state dir>/crawl-checkpoints/<job_id>/`.
  Files are written atomically (temp file, fsync, rename). On POSIX, directories are created with mode 0700
  and files with mode 0600.

If Redis is unreachable at startup (three attempts, 2 s timeout each), the API logs a warning and uses the disk
store when enabled, otherwise the memory store. A failed checkpoint is logged and the crawl continues; the
pages it could not write are retried at the next checkpoint.

On startup, queued and running jobs are reloaded and resumed from their last checkpoint. Pages fetched after
that checkpoint are fetched again; completed pages are not. A normal shutdown stops running jobs without marking
them cancelled, so they resume on the next startup. Only an explicit `DELETE /v1/crawl/{job_id}` makes a job
`cancelled`. Cancelled, completed, and failed jobs are not resumed. Records that fail validation are logged and
skipped. Multi-process workers sharing one queue are not implemented.

## Browser-pool metrics

The `[metrics]` extra plus `METRICS_ENABLED=true` enables `/metrics` with a dedicated
process-local Prometheus registry. It reports actual context capacity/utilization,
lifecycle errors, and page-creation latency; it never labels series with request
URLs or credentials. Disabled deployments do not import `prometheus-client` or
register the endpoint. See the [configuration and series reference](../README.md#optional-pool-metrics-source-checkout).
