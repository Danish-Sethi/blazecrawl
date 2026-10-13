#!/usr/bin/env node
/**
 * BlazeCrawl MCP server (Node.js) — scrape / map / crawl tools over stdio.
 *
 * Implements the MCP stdio JSON-RPC handshake directly (no SDK dependency).
 *
 *   BLAZECRAWL_API_URL   (default http://127.0.0.1:8000)
 *   BLAZECRAWL_API_KEY
 */

import { readFileSync } from "node:fs";

const VERSION = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version;
const API_KEY = process.env.BLAZECRAWL_API_KEY || null;
const BASE = (process.env.BLAZECRAWL_API_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");

const TOOLS = [
  {
    name: "scrape",
    description: "Scrape a URL and return clean Markdown (LLM-ready).",
    inputSchema: {
      type: "object",
      properties: {
        url: { type: "string", description: "http/https URL to scrape" },
        render: {
          type: "string",
          description: "Rendering mode: auto-select, static HTTP, or browser rendering",
          enum: ["auto", "static", "browser"],
          default: "auto",
        },
      },
      required: ["url"],
    },
  },
  {
    name: "map",
    description: "Discover the URL set of a site (sitemap + link graph).",
    inputSchema: {
      type: "object",
      properties: {
        url: {
          type: "string",
          description: "http/https site URL whose discoverable links should be mapped",
        },
      },
      required: ["url"],
    },
  },
  {
    name: "crawl",
    description: "Crawl a site (BFS, same-origin) and return page Markdown.",
    inputSchema: {
      type: "object",
      properties: {
        url: { type: "string", description: "http/https site URL to crawl" },
        max_pages: {
          type: "integer",
          description: "Maximum number of pages to return from the crawl",
        },
        max_depth: {
          type: "integer",
          description: "Maximum same-origin link depth from the starting URL",
        },
      },
      required: ["url"],
    },
  },
];

function headers() {
  const h = { "Content-Type": "application/json" };
  if (API_KEY) h["Authorization"] = `Bearer ${API_KEY}`;
  return h;
}

async function post(path, body) {
  const r = await fetch(`${BASE}${path}`, { method: "POST", headers: headers(), body: JSON.stringify(body) });
  return r.json();
}
async function get(path) {
  const r = await fetch(`${BASE}${path}`, { headers: headers() });
  return r.json();
}

async function callTool(name, args) {
  if (name === "scrape") {
    const body = { url: args.url };
    if (args.render) body.render = args.render;
    const d = await post("/v1/scrape", body);
    return (d.data && d.data.markdown) || JSON.stringify(d);
  }
  if (name === "map") {
    const d = await post("/v1/map", { url: args.url });
    return (d.urls || []).join("\n") || JSON.stringify(d);
  }
  if (name === "crawl") {
    const body = { url: args.url };
    if (args.max_pages) body.max_pages = args.max_pages;
    if (args.max_depth !== undefined) body.max_depth = args.max_depth;
    const job = await post("/v1/crawl", body);
    const id = job.job_id;
    for (let i = 0; i < 600; i++) {
      const st = await get(`/v1/crawl/${id}`);
      if (["completed", "failed", "cancelled"].includes(st.status)) {
        return (st.pages || []).map((p) => `## ${p.url}\n\n${p.markdown || ""}`).join("\n\n") || JSON.stringify(st);
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    return "crawl timed out";
  }
  return `unknown tool: ${name}`;
}

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

let buffer = "";
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let idx;
  while ((idx = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, idx).trim();
    buffer = buffer.slice(idx + 1);
    if (!line) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      continue;
    }
    handle(msg).catch(() => {});
  }
});

async function handle(msg) {
  const { id, method, params } = msg;
  if (method === "initialize") {
    send({
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: "blazecrawl", version: VERSION },
      },
    });
    return;
  }
  if (method === "notifications/initialized") return;
  if (method === "tools/list") {
    send({ jsonrpc: "2.0", id, result: { tools: TOOLS } });
    return;
  }
  if (method === "tools/call") {
    try {
      const text = await callTool(params.name, params.arguments || {});
      send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text }] } });
    } catch (e) {
      send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: String(e) }], isError: true } });
    }
    return;
  }
  if (id !== undefined) {
    send({ jsonrpc: "2.0", id, error: { code: -32601, message: `method not found: ${method}` } });
  }
}
