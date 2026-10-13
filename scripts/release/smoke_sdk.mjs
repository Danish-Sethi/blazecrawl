// Copied into the clean npm install directory so imports resolve installed packs.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { BlazeCrawl, BlazeCrawlError } from "@blazecrawl/sdk";

const version = process.argv[2];
const metadata = JSON.parse(readFileSync(new URL("node_modules/@blazecrawl/sdk/package.json", import.meta.url)));
assert.equal(metadata.version, version);
const client = new BlazeCrawl({ timeout: 60000 });
assert.equal((await client.health()).version, version);
const document = await client.scrape("https://example.com", {
  render: "static", use_cache: false, only_main_content: false,
});
assert.match(document.markdown, /Example Domain/);
await assert.rejects(
  new BlazeCrawl({ apiKey: "invalid-artifact-smoke-key" }).scrape("https://example.com"),
  (error) => error instanceof BlazeCrawlError && error.statusCode === 401,
);
const job = await client.crawl("https://example.com", { wait: false, max_pages: 1, max_depth: 1 });
const deadline = Date.now() + 90000;
let status;
do {
  status = await client.crawlStatus(job.job_id);
  assert.equal(status.pages_failed, 0);
  assert(!["failed", "cancelled"].includes(status.status), JSON.stringify(status));
  if (status.status === "completed") break;
  assert(Date.now() < deadline, "Node SDK crawl polling exceeded 90s");
  await new Promise((resolve) => setTimeout(resolve, 200));
} while (true);
assert.equal(status.pages_crawled, 1);
assert.match(status.pages[0].markdown, /domain/i);
console.log("PASS installed Node SDK version, Markdown unwrap, auth error, bounded crawl polling");
