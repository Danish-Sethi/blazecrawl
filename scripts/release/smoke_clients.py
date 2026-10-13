"""Real SDK HTTP and Python/Node MCP stdio calls against the live artifact API."""

import argparse
import asyncio
import json
import os
import shutil
import sys
from importlib.metadata import version
from pathlib import Path

import blazecrawl
from blazecrawl import AuthError, BlazeCrawl
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from mcp import ClientSession, StdioServerParameters


async def mcp_smoke(command: str, arguments: list[str], expected: str):
    server = StdioServerParameters(command=command, args=arguments, env=dict(os.environ))
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        assert initialized.serverInfo.version == expected, initialized.serverInfo
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {"scrape", "map", "crawl"}
        result = await session.call_tool(
            "scrape", {"url": "https://example.com", "render": "static"}
        )
        assert not result.isError and result.content
        assert isinstance(result.content[0], TextContent)
        assert "Example Domain" in result.content[0].text, result
    print(f"PASS installed MCP initialize/list_tools/call_tool: {Path(command).name}")


async def main(args):
    assert blazecrawl.__version__ == version("blazecrawl") == args.version
    assert version("blazecrawl-mcp") == args.version
    with BlazeCrawl(max_retries=0, timeout=60) as client:
        assert client.health()["version"] == args.version
        document = client.scrape("https://example.com", render="static", use_cache=False)
        assert "Example Domain" in document["markdown"], document
        # Exercise asynchronous polling with an outer deadline, not an unbounded SDK loop.
        job = await asyncio.wait_for(
            client.acrawl("https://example.com", max_pages=1, max_depth=1, poll_interval=0.2),
            timeout=90,
        )
        assert job["status"] == "completed" and job["pages_crawled"] == 1, job
        assert client.crawl_status(job["job_id"])["job_id"] == job["job_id"]
        await client.aclose()
    with BlazeCrawl(api_key="invalid-artifact-smoke-key", max_retries=0) as client:
        try:
            client.scrape("https://example.com", render="static")
        except AuthError:
            pass
        else:
            raise AssertionError("Python SDK did not propagate authentication error")
    print("PASS installed Python SDK version, Markdown unwrap, auth error, async crawl polling")
    python_mcp = Path(sys.executable).parent / (
        "blazecrawl-mcp.exe" if os.name == "nt" else "blazecrawl-mcp"
    )
    await asyncio.wait_for(mcp_smoke(str(python_mcp), [], args.version), timeout=90)
    node_root = Path(args.node_root)
    metadata = json.loads((node_root / "node_modules/@blazecrawl/mcp/package.json").read_text())
    assert metadata["version"] == args.version
    node = shutil.which("node")
    assert node is not None, "Node executable is required for the installed npm MCP check"
    await asyncio.wait_for(
        mcp_smoke(
            node,
            [str(node_root / "node_modules/@blazecrawl/mcp/src/index.js")],
            args.version,
        ),
        timeout=90,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--node-root", required=True)
    asyncio.run(main(parser.parse_args()))
