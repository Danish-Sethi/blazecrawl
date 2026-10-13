# @blazecrawl/sdk

Node.js API client for a running BlazeCrawl Core server. Requires Node.js 18+.

## Install

```bash
npm install @blazecrawl/sdk@0.2.0
```

These are v0.2.0 candidate instructions; check the [release status](../../README.md#quickstart).
The SDK does not start an API server.

## Authenticate and scrape

```js
import { BlazeCrawl } from "@blazecrawl/sdk";

const client = new BlazeCrawl({
  apiKey: "blz_local_...",
  baseUrl: "http://127.0.0.1:8000",
});

try {
  const document = await client.scrape("https://example.com");
  console.log(document.markdown);
} catch (error) {
  console.error("BlazeCrawl request failed:", error);
}
```

Use `map()` to discover URLs. `crawl()` polls to a terminal state by default;
pass `{ wait: false }` for a job reference and use `crawlStatus(jobId)` yourself.
Check `status` and `pages_failed`, not only whether the call returned.
