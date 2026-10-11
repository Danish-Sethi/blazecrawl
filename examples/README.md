# Examples

Small, runnable examples. Each assumes a BlazeCrawl Core server is running and
reachable (see the [README Quickstart](../README.md#quickstart)) and that a local
API key is available.

Set these first:

```bash
export BLAZECRAWL_API_URL=http://127.0.0.1:8000   # default; change if needed
export BLAZECRAWL_API_KEY=blz_local_...           # from the server logs
```

## Python

| Example | What it shows |
|---|---|
| [quickstart.py](quickstart.py) | Minimal scrape → Markdown, then a bounded crawl. |
| [scrape_browser_rendered.py](scrape_browser_rendered.py) | Force the Playwright browser path (`render="browser"`) for a JS-injected page. |
| [map_then_crawl.py](map_then_crawl.py) | `map` a site's URLs, then `crawl` a bounded subset. |
| [custom_headers.py](custom_headers.py) | Pass custom HTTP headers (User-Agent, Accept-Language, auth) with a scrape request. |

Run with the Python SDK installed (`pip install blazecrawl`):

```bash
python examples/quickstart.py
```

## Node

| Example | What it shows |
|---|---|
| [node_sdk.mjs](node_sdk.mjs) | Scrape, map, and crawl with `@blazecrawl/sdk`. |
| [custom_headers.mjs](custom_headers.mjs) | Pass custom HTTP headers (User-Agent, Accept-Language, auth) with a scrape request. |

Run with the Node SDK installed (`npm install @blazecrawl/sdk`):

```bash
node examples/node_sdk.mjs
```

## MCP

[mcp-config.json](mcp-config.json) is a generic MCP client configuration that
exposes BlazeCrawl's `scrape` / `map` / `crawl` tools to an MCP-compatible AI
client while the crawler stays self-hosted. See
[mcp/python](../mcp/python/README.md) and [mcp/node](../mcp/node/README.md) for
the two server implementations and client-specific setup.

> **Custom headers:** The current source checkout supports a `headers` field on
> `POST /v1/scrape` for passing custom outbound HTTP headers (User-Agent,
> Accept-Language, Authorization, etc.). See [custom_headers.py](custom_headers.py)
> and [custom_headers.mjs](custom_headers.mjs) for runnable examples.
> That field is **not in the published `v0.1.2` schema**.
