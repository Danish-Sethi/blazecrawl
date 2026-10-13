"""Check all release manifest and lockfile versions; used before build/publication."""

import argparse
import json
import re
import tomllib
from pathlib import Path


def check(root: Path, expected: str) -> dict[str, str]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected):
        raise ValueError("Release version must be X.Y.Z")
    versions = {}
    for name in ("pyproject.toml", "sdks/python/pyproject.toml", "mcp/python/pyproject.toml"):
        versions[name] = tomllib.loads((root / name).read_text())["project"]["version"]
    for directory in ("sdks/node", "mcp/node"):
        versions[f"{directory}/package.json"] = json.loads(
            (root / directory / "package.json").read_text()
        )["version"]
        lock = json.loads((root / directory / "package-lock.json").read_text())
        versions[f"{directory}/package-lock.json root"] = lock["version"]
        versions[f"{directory}/package-lock.json package"] = lock["packages"][""]["version"]
    lock = tomllib.loads((root / "uv.lock").read_text())
    versions["uv.lock core"] = next(
        p["version"] for p in lock["package"] if p["name"] == "blazecrawl-core"
    )
    mismatch = {name: version for name, version in versions.items() if version != expected}
    if mismatch:
        raise ValueError(f"Release {expected} does not match {mismatch}")
    return versions


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    print(f"All {len(check(args.root, args.version))} manifest/lock versions match {args.version}")
