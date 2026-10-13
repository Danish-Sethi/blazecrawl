# Contributing to BlazeCrawl Core

Thanks for your interest. This guide gets you from clone to a mergeable PR.

## Setup

```bash
git clone <repo> && cd blazecrawl
python -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -e ".[dev]"
playwright install chromium
```

Node surfaces (SDK + MCP) need Node 18+ and have **no** runtime dependencies.

## Architecture map

```
blazecrawl_core/
├── network/      # ssrf.py (validate+pin), egress.py (pinned client + browser guard), ip_utils.py
├── engine/       # scraper.py, content_extractor.py, markdown_converter.py,
│                 # browser_pool.py, crawler.py, job_store.py, mapper.py, robots.py,
│                 # cache.py, url_normalizer.py
├── api/          # main.py (FastAPI), auth.py (local key), schemas.py
└── cli.py
sdks/python, sdks/node, mcp/python, mcp/node
```

The rule that matters most: **every fetch of a user-controlled URL goes through
`network/egress.py`**. Never bypass it.

## Running tests

```bash
pytest                      # full Python suite
ruff check blazecrawl_core  # lint
ruff format --check .       # format
cd sdks/node && npm test    # Node SDK
```

Tests must pass and lint must be clean before a PR is merged.

## Style

* Python 3.11+, `ruff` enforced (see `pyproject.toml`).
* Keep the public API surface small and backward-compatible within a minor line.
* Security-sensitive changes (anything touching `network/`) require a test that
  would fail without the fix.

## Issue & PR workflow

1. Check the backlog ([docs/CONTRIBUTION_BACKLOG.md](docs/CONTRIBUTION_BACKLOG.md))
   or open an issue to discuss non-trivial changes first.
2. Fork, branch (`feat/...`, `fix/...`), commit logically.
3. Include tests proving the change.
4. Open a PR; fill the template; link the issue.
5. A maintainer reviews (target <72h). Address feedback; keep commits clean.

## Commits

Use conventional-commit style (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`,
`ci:`). One logical change per commit.

## Recognition

Contributors are credited in release notes and the project README. Sustained
contributors may be invited as committers (see GOVERNANCE.md).

## License

By contributing you agree your contributions are licensed under Apache-2.0.
