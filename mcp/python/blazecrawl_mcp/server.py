"""BlazeCrawl MCP server (Python).

Exposes scrape / map / crawl as MCP tools over stdio. Configure with:

    BLAZECRAWL_API_URL   (default http://127.0.0.1:8000)
    BLAZECRAWL_API_KEY
"""

from __future__ import annotations

import os
from importlib.metadata import version

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

API_KEY = os.environ.get("BLAZECRAWL_API_KEY")
API_BASE_URL = os.environ.get("BLAZECRAWL_API_URL", "http://127.0.0.1:8000").rstrip("/")

server = Server("blazecrawl", version=version("blazecrawl-mcp"))


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="scrape",
            description="Scrape a URL and return clean Markdown (LLM-ready).",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http/https URL to scrape"},
                    "render": {
                        "type": "string",
                        "description": "Rendering mode: auto-select, static HTTP, or browser rendering",
                        "enum": ["auto", "static", "browser"],
                        "default": "auto",
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="map",
            description="Discover the URL set of a site (sitemap + link graph).",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "http/https site URL whose discoverable links should be mapped",
                    }
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="crawl",
            description="Crawl a site (BFS, same-origin) and return page Markdown.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "http/https site URL to crawl",
                    },
                    "max_pages": {
                        "type": "integer",
                        "description": "Maximum number of pages to return from the crawl",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum same-origin link depth from the starting URL",
                    },
                },
                "required": ["url"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    async with httpx.AsyncClient(base_url=API_BASE_URL, headers=_headers(), timeout=180.0) as c:
        if name == "scrape":
            body = {"url": arguments["url"]}
            if "render" in arguments:
                body["render"] = arguments["render"]
            r = await c.post("/v1/scrape", json=body)
            data = r.json()
            md = (data.get("data") or {}).get("markdown") or str(data)
            return [TextContent(type="text", text=md)]

        if name == "map":
            r = await c.post("/v1/map", json={"url": arguments["url"]})
            data = r.json()
            urls = data.get("urls", [])
            return [TextContent(type="text", text="\n".join(urls) or str(data))]

        if name == "crawl":
            body = {"url": arguments["url"]}
            for k in ("max_pages", "max_depth"):
                if k in arguments:
                    body[k] = arguments[k]
            r = await c.post("/v1/crawl", json=body)
            job = r.json()
            jid = job.get("job_id")
            import asyncio

            for _ in range(600):
                g = await c.get(f"/v1/crawl/{jid}")
                st = g.json()
                if st.get("status") in ("completed", "failed", "cancelled"):
                    pages = st.get("pages", [])
                    out = "\n\n".join(
                        f"## {p.get('url')}\n\n{p.get('markdown', '')}" for p in pages
                    )
                    return [TextContent(type="text", text=out or str(st))]
                await asyncio.sleep(1)
            return [TextContent(type="text", text="crawl timed out")]

        return [TextContent(type="text", text=f"unknown tool: {name}")]


async def _run() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    import asyncio

    asyncio.run(_run())


if __name__ == "__main__":
    main()
