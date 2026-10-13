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
   Optional extras (`[redis]`, `[structured]`) add integrations.
3. **Fail closed.** Invalid URLs, blocked addresses, and resolution errors
   abort before any IO.
4. **Self-host first.** One `docker compose up` reaches a healthy, useful API.

## Hybrid rendering

`scraper.scrape(render="auto")` first attempts a cheap SSRF-pinned static HTTP
fetch. If the extracted main content is too thin (JS-heavy page), it retries
with the headless-browser path, which is also egress-guarded per request.

## Crawl queue and job persistence

Crawls run as in-process tasks on a single node. Job state is snapshotted to a job store at each
lifecycle transition (queued, running, finished, cancelled):

- `memory` (default): process-local. Jobs are lost on restart.
- `redis`: used when `REDIS_URL` is set and reachable at startup (requires the `redis` extra,
  `pip install blazecrawl-core[redis]`). Jobs are stored under `bc:crawl:job:<job_id>` with no expiry;
  delete those keys to purge them. If Redis is unreachable at startup, the API logs a warning and uses the
  memory store.

On startup, persisted jobs are loaded so `GET /v1/crawl/{id}` keeps working after a restart. Any job still
`queued` or `running` is marked `failed` with `interrupted by restart`. Crawls are not resumed, and
multi-process workers sharing one queue are not implemented.
