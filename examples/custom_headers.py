"""Scrape with custom HTTP headers.

Run a local server (see README Quickstart), install the Python SDK, then:

    export BLAZECRAWL_API_KEY=blz_local_...
    python examples/custom_headers.py

The default echo service is external (httpbin.org). All outbound credentials
below are FAKE: never send real secrets to a public echo service. Set
BLAZECRAWL_HEADER_DEMO_URL to your own echo endpoint if needed. The SDK's
BLAZECRAWL_API_KEY authenticates to your local API, not to the target website.
"""

import os

from blazecrawl import BlazeCrawl

URL = os.environ.get("BLAZECRAWL_HEADER_DEMO_URL", "https://httpbin.org/headers")

with BlazeCrawl(api_key=os.environ.get("BLAZECRAWL_API_KEY")) as bc:
    examples = [
        # User-Agent overrides user_agent on the initial origin, ignoring name case.
        ("1. Custom User-Agent", {"user-agent": "MyBot/1.0 (contact@example.com)"}),
        # Accept-Language requests localized content from sites that support it.
        ("2. Accept-Language: French", {"Accept-Language": "fr-FR,fr;q=0.9"}),
        # These are fake website credentials, NOT your local BlazeCrawl API key.
        (
            "3. Custom authentication headers (FAKE)",
            {
                "Authorization": "Bearer fake-example-token",
                "X-Custom-Api-Key": "fake-example-api-key",
            },
        ),
        # Multiple headers can be combined; the echo response shows their values.
        (
            "4. Combined headers",
            {
                "User-Agent": "MyBot/1.0 (contact@example.com)",
                "Accept-Language": "de-DE,de;q=0.9",
                "Accept": "application/json",
                "X-Request-ID": "req-12345",
            },
        ),
        # No custom headers: the server's default User-Agent applies.
        ("5. Default (no custom headers)", {}),
    ]
    for title, headers in examples:
        print(f"\n{title}")
        doc = bc.scrape(
            URL,
            headers=headers,
            render="static",
            formats=["text"],
            only_main_content=False,
        )
        # Both SDKs unwrap the API's data object; text is returned as plaintext.
        print(doc["plaintext"])
