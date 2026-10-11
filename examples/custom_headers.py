"""Scrape with custom HTTP headers.

Many websites serve different content based on headers like User-Agent,
Accept-Language, or custom authentication tokens. This example shows how
to pass custom headers to BlazeCrawl's scrape endpoint.

Run a server first (see README Quickstart), then:

    export BLAZECRAWL_API_KEY=blz_local_...
    python examples/custom_headers.py
"""

import os

from blazecrawl import BlazeCrawl

# httpbin.org/headers echoes back the request headers it received,
# making it easy to verify that custom headers are being sent.
URL = "https://httpbin.org/headers"

with BlazeCrawl(api_key=os.environ.get("BLAZECRAWL_API_KEY")) as bc:
    # --- 1. Custom User-Agent ---
    # Some sites block default bot UAs or serve different content to
    # different browsers. Override the User-Agent to mimic a real browser.
    print("=" * 60)
    print("1. Custom User-Agent")
    print("=" * 60)
    doc = bc.scrape(
        URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
    )
    print(doc["markdown"][:500])

    # --- 2. Accept-Language ---
    # Serve localized content by requesting a specific language.
    print("\n" + "=" * 60)
    print("2. Accept-Language: French")
    print("=" * 60)
    doc = bc.scrape(
        URL,
        headers={
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    print(doc["markdown"][:500])

    # --- 3. Custom authentication headers ---
    # Some APIs or gated content require a Bearer token or API key
    # in the request headers.
    print("\n" + "=" * 60)
    print("3. Custom Authorization header")
    print("=" * 60)
    doc = bc.scrape(
        URL,
        headers={
            "Authorization": "Bearer my-secret-token-12345",
            "X-Custom-Api-Key": "my-api-key-abcdef",
        },
    )
    print(doc["markdown"][:500])

    # --- 4. Multiple headers combined ---
    # Combine several headers in a single request.
    print("\n" + "=" * 60)
    print("4. Combined headers")
    print("=" * 60)
    doc = bc.scrape(
        URL,
        headers={
            "User-Agent": "MyBot/1.0 (contact@example.com)",
            "Accept-Language": "de-DE,de;q=0.9",
            "Accept": "application/json",
            "X-Request-ID": "req-12345",
        },
    )
    print(doc["markdown"][:500])

    # --- 5. No custom headers (default behavior) ---
    # Without a headers parameter, BlazeCrawl uses its default User-Agent.
    print("\n" + "=" * 60)
    print("5. Default (no custom headers)")
    print("=" * 60)
    doc = bc.scrape(URL)
    print(doc["markdown"][:500])