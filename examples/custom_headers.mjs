/**
 * Scrape with custom HTTP headers.
 *
 * Run a local server (see README Quickstart), then:
 *   export BLAZECRAWL_API_KEY=blz_local_...
 *   node examples/custom_headers.mjs
 *
 * The default echo service is external (httpbin.org). All outbound credentials
 * below are FAKE: never send real secrets to a public echo service. Set
 * BLAZECRAWL_HEADER_DEMO_URL to your own echo endpoint if needed. The SDK's
 * BLAZECRAWL_API_KEY authenticates to your local API, not to the target website.
 */

import { BlazeCrawl } from "../sdks/node/src/index.js";

const bc = new BlazeCrawl({ apiKey: process.env.BLAZECRAWL_API_KEY });
const URL = process.env.BLAZECRAWL_HEADER_DEMO_URL || "https://httpbin.org/headers";
const examples = [
  // User-Agent overrides user_agent on the initial origin, ignoring name case.
  ["1. Custom User-Agent", { "user-agent": "MyBot/1.0 (contact@example.com)" }],
  // Accept-Language requests localized content from sites that support it.
  ["2. Accept-Language: French", { "Accept-Language": "fr-FR,fr;q=0.9" }],
  // These are fake website credentials, NOT your local BlazeCrawl API key.
  ["3. Custom authentication headers (FAKE)", {
    Authorization: "Bearer fake-example-token",
    "X-Custom-Api-Key": "fake-example-api-key",
  }],
  // Multiple headers can be combined; the echo response shows their values.
  ["4. Combined headers", {
    "User-Agent": "MyBot/1.0 (contact@example.com)",
    "Accept-Language": "de-DE,de;q=0.9",
    Accept: "application/json",
    "X-Request-ID": "req-12345",
  }],
  // No custom headers: the server's default User-Agent applies.
  ["5. Default (no custom headers)", {}],
];

for (const [title, headers] of examples) {
  console.log(`\n${title}`);
  const doc = await bc.scrape(URL, {
    headers,
    render: "static",
    formats: ["text"],
    only_main_content: false,
  });
  // Both SDKs unwrap the API's data object; text is returned as plaintext.
  console.log(doc.plaintext);
}
