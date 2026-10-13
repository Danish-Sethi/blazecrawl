# v0.2.0 release preflight and handoff

The five package names remain `blazecrawl-core`, `blazecrawl`, `blazecrawl-mcp`,
`@blazecrawl/sdk`, and `@blazecrawl/mcp`. The new image is
`ghcr.io/danish-sethi/blazecrawl:0.2.0` (Linux amd64). Do not mutate the old personal
GHCR image or registry permissions. This document does not claim publication.

## Before publication

Every PR runs the **Release / Clean candidate** job on Ubuntu 24.04, without registry
write/OIDC permissions. It builds wheels/sdists, validates them with Twine, packs
npm distributions, and builds the image. Clean temporary environments install only
the built wheels/packs; core imports use isolated Python and run outside the checkout.
The image installs a non-editable package, not source-backed imports.

Checks require live public DNS/TLS/HTTP, not mocked API responses:
runtime/CLI/health versions, auth rejection, private-URL rejection, authenticated
static Markdown from example.com, actual Chromium rendering + PNG signature/cap,
two-page depth-one quotes.toscrape.com crawl with polling, stop/restart with the same
checkpoint storage, retained first page and completed bounded result. Installed
Python/Node SDKs exercise version/health, Markdown unwrapping, auth errors and crawl
polling; both MCP distributions perform real initialize/list_tools/call_tool over
stdio through the local API. No SSRF override or skip-as-success is used.
Public site outages are real failures, not substituted fixtures.

Manual read-only candidate, once this workflow is on main:

```bash
gh workflow run release.yml --repo Danish-Sethi/blazecrawl --ref main \
  -f mode=candidate -f version=0.2.0
```

Local equivalent on a Linux machine with Docker, Python 3.12, uv, Node 24/npm:

```bash
python scripts/release/versions.py 0.2.0
bash scripts/release/artifacts.sh built 0.2.0
```

The job uploads three artifacts: Python distributions, npm packs, and the tested
image archive. Every publisher depends on this complete gate; GHCR loads/pushes the
tested image without rebuilding, with provenance and an SPDX SBOM attested to its
registry digest. Service shutdown, temporary state/volume removal
and subprocess deadlines are part of the smoke checks. The job has a 35-minute cap.

## Tag publication (coordinator only)

After review/merge and green code/artifact CI, the coordinator can create `v0.2.0`
on the reviewed main ancestor. The exact `vX.Y.Z` tag gate verifies all five manifests,
both npm lockfile root/package-root versions, and uv's core version. A PR or manual
dispatch cannot publish. PyPI uses the existing `release`, `release-pypi-blazecrawl`,
and `release-pypi-mcp` environments. npm uses `release` and OIDC provenance, initially
under the **candidate** dist-tag. There is **no token fallback** and no credential
or trusted-publisher mutation in the workflow.

The transferred repository identity may need an owner-side trusted-publisher update.
If OIDC rejects publication, the owner must configure the existing packages to trust
owner **Danish-Sethi**, repository **blazecrawl**, workflow **release.yml**, and the
corresponding environment above (npm packages: **release**). Inspect the registry's
actual error before changing anything. Historical v0.1.2 attestations naming
`danishxsethi/blazecrawl` are not proof of current authorization. Keep the release
draft while blocked; do not add tokens to work around it.

## After publication

The tag workflow runs the same smoke checks using exact versions freshly installed
from **public PyPI and npm**, plus an **anonymous** GHCR pull with an empty Docker
config. It does not install local builds. A newly created GHCR package may be private:
push success is not public availability. If anonymous pull fails, the owner must
review the package's visibility separately; the agent does not change access.

After an owner resolves a registry/visibility blocker, rerun only the public check:

```bash
gh workflow run release.yml --repo Danish-Sethi/blazecrawl --ref main \
  -f mode=published -f version=0.2.0
```

Or on Linux: `bash scripts/release/artifacts.sh registry 0.2.0`.
Allow normal registry propagation, but do not call a failed or skipped job success.
Publication is not atomic across registries; already-published immutable versions
must not be overwritten. Re-running a successful publisher may reject duplicates;
use the read-only published mode for verification, not blind full-workflow reruns.

Only after all public checks pass does the coordinator finalize the GitHub release,
update the README candidate notice, promote the verified image digest to `latest`,
and arrange owner-authorized npm `latest` dist-tags for both 0.2.0 packages. npm OIDC
publication alone does not grant authenticated `npm dist-tag add`; do not add a token
fallback to automate that promotion. Until promotion, use exact version pins.
The [five-person trial](FIRST_RUN_TRIAL.md) is a separate human-feedback gate with
empty results, not fabricated CI evidence.
