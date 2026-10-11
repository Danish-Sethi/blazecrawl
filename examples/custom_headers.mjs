/**
 * Scrape with custom HTTP headers.
 *
 * Many websites serve different content based on headers like User-Agent,
 * Accept-Language, or custom authentication tokens. This example shows how
 * to pass custom headers to BlazeCrawl's scrape endpoint.
 *
 * Run a server first (see README Quickstart), then:
 *
 *   export BLAZECRAWL_API_KEY=blz_local_...
 *   node examples/custom_headers.mjs
 */

import { BlazeCrawl } from "../sdks/node/src/index.js";

const bc = new BlazeCrawl({ apiKey: process.env.BLAZECRAWL_API_KEY });

// httpbin.org/headers echoes back the request headers it received,
// making it easy to verify that custom headers are being sent.
const URL = "https://httpbin.org/headers";

// --- 1. Custom User-Agent ---
// Some sites block default bot UAs or serve different content to
// different browsers. Override the User-Agent to mimic a real browser.
console.log("=".repeat(60));
console.log("1. Custom User-Agent");
console.log("=".repeat(60));
const doc1 = await bc.scrape(URL, {
  headers: {
    "User-Agent":
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
      "AppleWebKit/537.36 (KHTML, like Gecko) " +
      "Chrome/120.0.0.0 Safari/537.36",
  },
});
console.log((doc1.markdown || "").slice(0, 500));

// --- 2. Accept-Language ---
// Serve localized content by requesting a specific language.
console.log("\n" + "=".repeat(60));
console.log("2. Accept-Language: French");
console.log("=".repeat(60));
const doc2 = await bc.scrape(URL, {
  headers: {
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
  },
});
console.log((doc2.markdown || "").slice(0, 500));

// --- 3. Custom authentication headers ---
// Some APIs or gated content require a Bearer token or API key
// in the request headers.
console.log("\n" + "=".repeat(60));
console.log("3. Custom Authorization header");
console.log("=".repeat(60));
const doc3 = await bc.scrape(URL, {
  headers: {
    Authorization: "Bearer my-secret-token-12345",
    "X-Custom-Api-Key": "my-api-key-abcdef",
  },
});
console.log((doc3.markdown || "").slice(0, 500));

// --- 4. Multiple headers combined ---
// Combine several headers in a single request.
console.log("\n" + "=".repeat(60));
console.log("4. Combined headers");
console.log("=".repeat(60));
const doc4 = await bc.scrape(URL, {
  headers: {
    "User-Agent": "MyBot/1.0 (contact@example.com)",
    "Accept-Language": "de-DE,de;q=0.9",
    Accept: "application/json",
    "X-Request-ID": "req-12345",
  },
});
console.log((doc4.markdown || "").slice(0, 500));

// --- 5. No custom headers (default behavior) ---
// Without a headers parameter, BlazeCrawl uses its default User-Agent.
console.log("\n" + "=".repeat(60));
console.log("5. Default (no custom headers)");
console.log("=".repeat(60));
const doc5 = await bc.scrape(URL);
console.log((doc5.markdown || "").slice(0, 500));