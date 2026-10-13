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

## Crawl queue

Single-node default uses an in-process job manager
(`CRAWL_QUEUE_BACKEND=memory`), which is correct for one process. Set
`REDIS_URL` + a Redis-backed backend for multi-process worker fleets; the job
contract (`/v1/crawl` 202 → poll `GET /v1/crawl/{id}`) is identical.

## Browser-pool metrics

The `[metrics]` extra plus `METRICS_ENABLED=true` enables `/metrics` with a dedicated
process-local Prometheus registry. It reports actual context capacity/utilization,
lifecycle errors, and page-creation latency; it never labels series with request
URLs or credentials. Disabled deployments do not import `prometheus-client` or
register the endpoint. See the [configuration and series reference](../README.md#optional-pool-metrics-source-checkout).
