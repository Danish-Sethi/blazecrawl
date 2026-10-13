# Roadmap

BlazeCrawl Core is `v0.1.2`. This is a directional roadmap, not a commitment;
priorities follow real usage and contributor interest.

## Now (0.1.x)
* scrape / crawl / map, Markdown, browser rendering, SSRF hardening
* Python + Node SDK, CLI, MCP servers, Docker self-host
* hardening, tests, docs, contributor onboarding

## Implemented on main (unreleased)
* validated, origin-scoped custom scrape headers and Python/Node examples
* screenshot API and CLI output, full-page capture, and a configurable PNG cap
* optional browser-pool Prometheus metrics, disabled by default
* Redis-backed job persistence and opt-in disk checkpoints, including restart resume
* shared per-host crawl-delay/concurrency and token buckets, bounded HTTP backoff, and a process-wide fetch-attempt budget
* optional operator-supplied upstream proxies with locally validated-IP CONNECT for static and browser fetches

## Next (0.2)
* pluggable search-provider interface (community adapters)
* pluggable LLM-extraction provider interface
* Redis-backed distributed crawl queue and multi-worker coordination (distinct from single-process job persistence)
* local filesystem export (JSONL/Markdown)

## Later (0.3+)
* plugin/extractor registry
* change detection / diffing
* scheduled crawls
* additional document parsers (PDF) with size/sandbox guards

## Explicitly out of scope for OSS Core
Managed proxies, managed anti-bot, managed LLM execution, SSO/SCIM, billing,
multi-tenant metering — see [OSS_VS_CLOUD.md](OSS_VS_CLOUD.md).

Vote/discuss on the GitHub issue tracker.
