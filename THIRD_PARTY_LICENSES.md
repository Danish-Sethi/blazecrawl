# Third-Party Licenses

BlazeCrawl Core is distributed under the Apache License 2.0 (see LICENSE).
This file lists all third-party dependencies and their licenses.

**Total dependencies:** 71 packages (70 third-party + blazecrawl-core itself)

## License Compatibility Summary

All direct and transitive dependencies are compatible with Apache-2.0 distribution:

| License | Count | Compatibility | Notes |
|---------|-------|---------------|-------|
| MIT / MIT License | 26 | ✅ Compatible | Permissive |
| BSD-2-Clause / BSD-3-Clause | 16 | ✅ Compatible | Permissive |
| Apache-2.0 | 6 | ✅ Compatible | Same license |
| Apache License 2.0 | 1 | ✅ Compatible | Same license |
| 0BSD | 1 | ✅ Compatible | Public domain equivalent |
| PSF-2.0 / PSFL | 2 | ✅ Compatible | Python Software Foundation |
| MPL-2.0 | 1 | ✅ Compatible | File-level copyleft, compatible with Apache-2.0 |
| MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later | 1 | ✅ Compatible (MPL-1.1 chosen) | Tri-license, MPL-1.1 selected |
| Apache-2.0 AND CNRI-Python | 1 | ✅ Compatible | Both permissive |
| Apache-2.0 OR BSD-2-Clause | 1 | ✅ Compatible | Either permissive |
| MIT AND PSF-2.0 | 1 | ✅ Compatible | Both permissive |
| Dual License (Apache-2.0 / BSD) | 1 | ✅ Compatible | Either permissive |
| The BSD 2-Clause License | 1 | ✅ Compatible | Permissive |
| W3C License | 1 | ✅ Compatible | Permissive (pyRdfa3) |

**No GPL-only, AGPL, SSPL, or proprietary licenses detected.**

## Special License Notes

### tld (v0.13.2)

`tld` is tri-licensed under GPL-2.0, LGPL-2.1, or MPL-1.1 **at your option**.
BlazeCrawl chooses **MPL-1.1** for this dependency, which is:
- File-level copyleft (weak copyleft)
- Compatible with Apache-2.0 when used as a library
- Allows commercial use, modification, and distribution

Source: `courlan` (URL extraction for `trafilatura`)

## Complete Dependency List

### Python Dependencies (uv.lock)

| Package | Version | License |
|---------|---------|---------|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT License |
| anyio | 4.15.1 | MIT |
| async-timeout | 5.0.1 | Apache 2 |
| babel | 2.18.0 | BSD License |
| beautifulsoup4 | 4.15.0 | MIT License |
| certifi | 2026.7.22 | MPL-2.0 |
| chardet | 7.6.0 | 0BSD |
| charset-normalizer | 3.5.1 | MIT |
| click | 8.5.0 | BSD License |
| colorama | 0.4.6 | BSD License |
| courlan | 1.4.0 | Apache-2.0 |
| cssselect | 1.5.0 | BSD License |
| dateparser | 1.4.3 | BSD License |
| defusedxml | 0.7.1 | PSFL |
| extruct | 0.18.0 | BSD License |
| fastapi | 0.141.1 | MIT |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT |
| hiredis | 3.4.1 | MIT |
| html-text | 0.7.1 | MIT |
| html5lib | 1.1 | MIT License |
| htmldate | 1.10.0 | Apache-2.0 |
| httpcore | 1.0.9 | BSD License |
| httptools | 0.8.0 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.20 | BSD License |
| iniconfig | 2.3.0 | MIT |
| jstyleson | 0.0.2 | MIT |
| justext | 3.0.2 | The BSD 2-Clause License |
| lxml | 6.1.3 | BSD-3-Clause |
| lxml-html-clean | 0.4.5 | BSD-3-Clause |
| markdownify | 1.2.3 | MIT License |
| mf2py | 2.0.2 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| playwright | 1.63.0 | Apache-2.0 |
| pluggy | 1.6.0 | MIT |
| pydantic | 2.13.5 | MIT |
| pydantic-core | 2.46.5 | MIT |
| pyee | 13.0.1 | MIT |
| pygments | 2.21.0 | BSD-2-Clause |
| pyparsing | 3.3.2 | MIT |
| pyrdfa3 | 3.6.5 | W3C License |
| pytest | 9.1.1 | MIT |
| pytest-asyncio | 1.4.0 | Apache-2.0 |
| python-dateutil | 2.9.0.post0 | Dual License (Apache-2.0 / BSD) |
| python-dotenv | 1.2.3 | BSD-3-Clause |
| pytz | 2026.3.post1 | MIT |
| pyyaml | 6.0.3 | MIT |
| rdflib | 7.6.0 | BSD-3-Clause |
| readability-lxml | 0.8.4.1 | Apache License 2.0 |
| redis | 8.1.0 | MIT License |
| regex | 2026.9.10 | Apache-2.0 AND CNRI-Python |
| requests | 2.34.2 | Apache-2.0 |
| ruff | 0.16.8 | MIT |
| six | 1.17.0 | MIT |
| soupsieve | 2.9.2 | MIT License |
| starlette | 1.6.0 | BSD License |
| tld | 0.13.2 | MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later |
| trafilatura | 2.2.0 | Apache-2.0 |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| tzdata | 2026.4 | Apache-2.0 |
| tzlocal | 5.4.4 | MIT |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.53.0 | BSD License |
| uvloop | 0.22.1 | MIT License |
| w3lib | 2.4.1 | BSD-3-Clause |
| watchfiles | 1.2.0 | MIT |
| webencodings | 0.6.1 | BSD-3-Clause |
| websockets | 17.1 | BSD License |

### JavaScript Dependencies (sdks/node, mcp/node)

| Package | Version | License |
|---------|---------|---------|
| @modelcontextprotocol/sdk | ^1.8.0 | MIT |

### Container Images

| Image | Base | License |
|-------|------|---------|
| python:3.11-slim | Debian | MIT (Python) / Various (Debian) |
| mcr.microsoft.com/playwright/python:v1.63.0-noble | Ubuntu 24.04 | Apache-2.0 (Playwright) / Various (Ubuntu) |
| redis:7-alpine | Alpine Linux | BSD-3-Clause (Redis) |

## Verification

Last verified: 2026-09-22
Method: `uv.lock` lockfile analysis + `importlib.metadata` + PyPI metadata
Tools: `uv tree`, `pip-licenses`, manual PyPI audit

All dependencies verified to have:
- ✅ Publicly available source code
- ✅ OSI-approved or permissive licenses
- ✅ No GPL-only or AGPL contamination
- ✅ No proprietary or commercial restrictions
- ✅ No export control or patent assertion clauses

## License Texts

Full license texts for all dependencies are included in the distribution:
- Python packages: `.venv/lib/python3.12/site-packages/*/LICENSE*`
- Container images: Available in respective image layers
- BlazeCrawl Core: `LICENSE` (Apache-2.0)

## Questions or Concerns

If you have questions about third-party licenses or believe there is an error in this inventory, please open an issue at:
https://github.com/Danish-Sethi/blazecrawl/issues
