"""BlazeCrawl Core CLI.

Talks to a running BlazeCrawl Core server. Configure with:

    BLAZECRAWL_API_URL   (default http://127.0.0.1:8000)
    BLAZECRAWL_API_KEY   (required unless the server runs loopback auth-disabled)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import httpx

from blazecrawl_core import __version__


def _client() -> httpx.Client:
    base = os.environ.get("BLAZECRAWL_API_URL", "http://127.0.0.1:8000").rstrip("/")
    key = os.environ.get("BLAZECRAWL_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.Client(base_url=base, headers=headers, timeout=120.0)


def _print(resp: httpx.Response, as_json: bool, output: Path | None = None) -> int:
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = {"error": resp.text}
        print(json.dumps(detail, indent=2), file=sys.stderr)
        return 1
    data = resp.json()
    if as_json:
        text = json.dumps(data, indent=2)
    else:
        # Human-friendly: prefer markdown.
        md = (data.get("data") or {}).get("markdown") or data.get("markdown")
        text = md or json.dumps(data, indent=2)
    if output is not None:
        try:
            output.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Could not write output to {output}: {exc}", file=sys.stderr)
            return 1
    else:
        print(text)
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    wants_png = "screenshot" in (args.formats or [])
    if wants_png and not args.json and args.output is None:
        print(
            "--formats screenshot writes a PNG file: pass --output PATH (or --json).",
            file=sys.stderr,
        )
        return 2
    body: dict = {"url": args.url, "render": args.render}
    if args.formats:
        body["formats"] = args.formats
    if args.full_page:
        body["full_page"] = True
    with _client() as c:
        resp = c.post("/v1/scrape", json=body)
    if wants_png and not args.json:
        return _write_screenshot(resp, args.output)
    return _print(resp, args.json, args.output)


def _write_screenshot(resp: httpx.Response, output: Path) -> int:
    if resp.status_code >= 400:
        return _print(resp, False)
    encoded = (resp.json().get("data") or {}).get("screenshot")
    if not encoded:
        print("Server response did not include a screenshot.", file=sys.stderr)
        return 1
    try:
        output.write_bytes(base64.b64decode(encoded))
    except OSError as exc:
        print(f"Could not write output to {output}: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    with _client() as c:
        return _print(c.post("/v1/map", json={"url": args.url}), args.json or True)


def cmd_crawl(args: argparse.Namespace) -> int:
    body: dict = {"url": args.url}
    if args.max_pages:
        body["max_pages"] = args.max_pages
    if args.max_depth is not None:
        body["max_depth"] = args.max_depth
    with _client() as c:
        r = c.post("/v1/crawl", json=body)
        if r.status_code >= 400:
            return _print(r, True)
        job = r.json()
        jid = job["job_id"]
        if not args.wait:
            print(json.dumps(job, indent=2))
            return 0
        import time

        for _ in range(600):
            g = c.get(f"/v1/crawl/{jid}")
            st = g.json()
            print(f"status={st.get('status')} crawled={st.get('pages_crawled')}", file=sys.stderr)
            if st.get("status") in ("completed", "failed", "cancelled"):
                print(json.dumps(st, indent=2))
                return 0 if st.get("status") == "completed" else 1
            time.sleep(1)
        print("Timed out waiting for crawl", file=sys.stderr)
        return 1


def _doctor_playwright() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return False, f"Playwright import failed: {exc}"

    try:
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if executable.exists():
                return True, f"Chromium available at {executable}"
            return False, f"Chromium executable is missing: {executable}"
    except Exception as exc:
        return False, f"Playwright check failed: {exc}"


def _doctor_server() -> tuple[bool, str]:
    configured_url = os.environ.get("BLAZECRAWL_API_URL")
    if not configured_url:
        return True, "Server check skipped (BLAZECRAWL_API_URL is not set)"

    try:
        with _client() as client:
            response = client.get("/ready")
    except httpx.HTTPError as exc:
        return False, f"Server /ready is unreachable: {exc}"

    if response.status_code >= 400:
        return False, f"Server /ready returned HTTP {response.status_code}"
    return True, "Server /ready is reachable"


def _doctor_api_key() -> tuple[bool, str]:
    key = os.environ.get("BLAZECRAWL_API_KEY")
    configured_url = os.environ.get("BLAZECRAWL_API_URL")
    if not key:
        return True, "API key check skipped (BLAZECRAWL_API_KEY is not set)"
    if not configured_url:
        return True, "API key check skipped (BLAZECRAWL_API_URL is not set)"

    try:
        with _client() as client:
            response = client.get("/v1/crawl/__doctor__")
    except httpx.HTTPError as exc:
        return False, f"API key check could not reach the server: {exc}"

    if response.status_code == 401:
        return False, "API key was rejected by the server"
    if response.status_code == 404:
        return True, "API key accepted by the server"
    if response.status_code < 400:
        return True, "API key accepted by the server"
    return False, f"API key check returned unexpected HTTP {response.status_code}"


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = [
        ("Playwright/Chromium", _doctor_playwright()),
        ("Server readiness", _doctor_server()),
        ("API key", _doctor_api_key()),
    ]

    failed = False
    print("BlazeCrawl doctor")
    for name, (ok, detail) in checks:
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {name}: {detail}")
        failed = failed or not ok
    return 1 if failed else 0


def cmd_health(args: argparse.Namespace) -> int:
    with _client() as c:
        return _print(c.get("/health"), True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="blazecrawl", description="BlazeCrawl Core CLI")
    p.add_argument("--version", action="version", version=f"blazecrawl-core {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scrape", help="Scrape a URL to Markdown")
    s.add_argument("url")
    s.add_argument(
        "--formats",
        nargs="+",
        choices=["markdown", "html", "text", "links", "images", "screenshot"],
    )
    s.add_argument("--render", choices=["auto", "static", "browser"], default="auto")
    s.add_argument(
        "--full-page",
        action="store_true",
        help="With --formats screenshot, capture the full scrollable page instead of the viewport",
    )
    s.add_argument("--json", action="store_true")
    s.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help=(
            "Write output to a UTF-8 file instead of stdout (overwrites existing files). "
            "With --formats screenshot, writes the PNG bytes instead."
        ),
    )
    s.set_defaults(func=cmd_scrape)

    m = sub.add_parser("map", help="Discover the URL set of a site")
    m.add_argument("url")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_map)

    cr = sub.add_parser("crawl", help="Crawl a site (BFS, same-origin)")
    cr.add_argument("url")
    cr.add_argument("--max-pages", type=int)
    cr.add_argument("--max-depth", type=int)
    cr.add_argument("--wait", action="store_true", help="Block until the crawl completes")
    cr.set_defaults(func=cmd_crawl)

    h = sub.add_parser("health", help="Check server health")
    h.set_defaults(func=cmd_health)
    d = sub.add_parser("doctor", help="Check local BlazeCrawl installation and server access")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
