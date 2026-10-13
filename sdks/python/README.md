# blazecrawl (Python SDK)

Python API client for a running BlazeCrawl Core server.

## Install

```bash
pip install blazecrawl==0.2.0
```

These are v0.2.0 candidate instructions; check the [release status](../../README.md#quickstart)
before installing. The server is separate. Run `blazecrawl-core` locally or point the client at a
self-hosted instance with `base_url`.

## Authenticate and scrape

```python
from blazecrawl import BlazeCrawl, BlazeCrawlError

try:
    with BlazeCrawl(
        api_key="blz_local_...",
        base_url="http://127.0.0.1:8000",
    ) as client:
        document = client.scrape("https://example.com")
        print(document["markdown"])
except BlazeCrawlError as exc:
    print(f"BlazeCrawl request failed: {exc}")
```

`map()` discovers site URLs. `crawl()` polls to a terminal state by default;
pass `wait=False` to get a job reference and use `crawl_status(job_id)` yourself.
Check `status` and `pages_failed`, not only whether the call returned.


### Retry transient responses

The Python SDK retries HTTP `429` and `5xx` responses with bounded exponential
backoff and jitter. By default it retries twice after the initial request.

```python
client = BlazeCrawl(
    api_key="blz_local_...",
    max_retries=2,
    backoff_base=0.25,
    backoff_jitter=0.1,
)
```

Set `max_retries=0` to disable retries. Validation/authentication failures and
other non-transient `4xx` responses are never retried.
