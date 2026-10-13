# @blazecrawl/mcp

MCP server exposing a self-hosted BlazeCrawl Core instance's `scrape`, `map`,
and `crawl` tools over stdio. Requires Node.js 18+.

## Install

```bash
npm install @blazecrawl/mcp@0.2.0
```

These are v0.2.0 candidate instructions; check the [release status](../../README.md#quickstart).
Start the separate API server before connecting this stdio adapter.

## Generic MCP client configuration

```json
{
  "mcpServers": {
    "blazecrawl": {
      "command": "npx",
      "args": ["-y", "@blazecrawl/mcp@0.2.0"],
      "env": {
        "BLAZECRAWL_API_URL": "http://127.0.0.1:8000",
        "BLAZECRAWL_API_KEY": "blz_local_..."
      }
    }
  }
}
```

The server forwards tool calls to BlazeCrawl. The tools are `scrape`, `map`, and
`crawl`. `crawl` waits and returns page Markdown; there is no MCP crawl-status
tool. Use the HTTP API or SDK for job IDs and explicit polling.
