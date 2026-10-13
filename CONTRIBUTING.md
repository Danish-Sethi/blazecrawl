# Contributing to BlazeCrawl Core

Thanks for your interest. This guide gets you from clone to a mergeable PR.

## Setup

Use Python 3.12 and [uv](https://docs.astral.sh/uv/) to match Ubuntu 24.04 CI
(core supports Python 3.11+). Linux/macOS:

```bash
git clone https://github.com/Danish-Sethi/blazecrawl.git
cd blazecrawl
uv python install 3.12
uv sync --python 3.12 --extra dev --extra metrics
uv run playwright install chromium
```

On Linux CI, `uv run playwright install --with-deps chromium` also supplies the OS
libraries; use it if your Linux browser cannot launch. Native Windows PowerShell:

```powershell
git clone https://github.com/Danish-Sethi/blazecrawl.git
Set-Location blazecrawl
uv python install 3.12
uv sync --python 3.12 --extra dev --extra metrics
uv run playwright install chromium
$env:BLAZECRAWL_API_KEY = uv run python -c "import secrets; print('blz_local_' + secrets.token_urlsafe(32))"
uv run blazecrawl-server
```

Native Windows needs an explicit random API key (POSIX key-file bootstrap is not
supported). Save it privately and use the same value in your client terminal.
On Linux/macOS, run `uv run blazecrawl-server` and copy the first-run key.
See the [authenticated requests and troubleshooting](README.md#quickstart).

For a source container with optional Redis: `docker compose up --build -d`, then
`docker compose logs -f api` for the first-run key. Compose binds only to loopback.

Node surfaces (SDK + MCP) need Node 18+ and have **no** runtime dependencies.
CI exercises Node 20; release/trusted-publishing CI uses Node 24 and npm >=11.5.1.

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
uv run pytest -q
uv run ruff check blazecrawl_core scripts/release tests/test_release_versions.py
uv run ruff format --check blazecrawl_core scripts/release tests/test_release_versions.py
uv build
npm --prefix sdks/node test
node --test mcp/node/test/*.test.mjs
uv run --directory sdks/python --extra dev pytest tests -q
uv run --directory mcp/python --with pytest pytest tests -q
```

Tests must pass and lint must be clean before a PR is merged.
The Linux read-only release candidate job additionally builds/installs all five
distributions and tests the image. See [release preflight](docs/RELEASING.md).
On Windows with Node 20, shells do not expand test globs; use
`node --test sdks\node\test\sdk.test.mjs mcp\node\test\schema.test.mjs` for the two
Node suites instead of the glob-based npm command.

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
