#!/usr/bin/env bash
# Linux candidate/public-registry checks. No source installs, mocks or SSRF overrides.
set -euo pipefail
mode="${1:?Usage: artifacts.sh built|registry X.Y.Z}"
version="${2:?Expected version required}"
repo="$(cd "$(dirname "$0")/../.." && pwd)"
python "$repo/scripts/release/versions.py" "$version"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
uv venv --python 3.12 --seed "$work/core"
uv venv --python 3.12 --seed "$work/clients"
mkdir "$work/node"
(
  cd "$work/node"
  npm init -y >/dev/null
)

case "$mode" in
  built)
    for package in . sdks/python mcp/python; do
      (cd "$repo/$package" && uv build && uvx twine check dist/*)
    done
    for package in sdks/node mcp/node; do
      (
        cd "$repo/$package"
        npm pack --json > "$work/$(basename "$(dirname "$package")")-pack.json"
      )
    done
    python - "$work" "$version" <<'PY'
import json
import re
import sys
from pathlib import Path
forbidden = re.compile(r"(^|/)(\.env|\.npmrc|\.pypirc|id_rsa|.*\.(pem|key|crt))$")
for path in Path(sys.argv[1]).glob("*-pack.json"):
    pack, = json.loads(path.read_text())
    assert pack["version"] == sys.argv[2], pack["version"]
    assert not any(forbidden.search(item["path"]) for item in pack["files"]), "Forbidden npm file"
    print(f"Validated npm pack: {pack['name']}@{pack['version']}")
PY
    "$work/core/bin/pip" install "$repo"/dist/*.whl
    "$work/clients/bin/pip" install "$repo"/sdks/python/dist/*.whl "$repo"/mcp/python/dist/*.whl
    (cd "$work/node" && npm install "$repo"/sdks/node/*.tgz "$repo"/mcp/node/*.tgz)
    image="blazecrawl-candidate:$version"
    docker build --platform linux/amd64 \
      --label "org.opencontainers.image.source=https://github.com/Danish-Sethi/blazecrawl" \
      --label "org.opencontainers.image.version=$version" \
      --label "org.opencontainers.image.revision=$(git -C "$repo" rev-parse HEAD)" \
      -t "$image" "$repo"
    ;;
  registry)
    # Explicit public indexes and no local wheel/tarball paths or npm credentials.
    "$work/core/bin/pip" install --index-url https://pypi.org/simple --no-cache-dir "blazecrawl-core==$version"
    "$work/clients/bin/pip" install --index-url https://pypi.org/simple --no-cache-dir \
      "blazecrawl==$version" "blazecrawl-mcp==$version"
    (cd "$work/node" && npm install --registry https://registry.npmjs.org \
      "@blazecrawl/sdk@$version" "@blazecrawl/mcp@$version")
    image="ghcr.io/danish-sethi/blazecrawl:$version"
    # Empty config proves the image is public, not merely readable with GITHUB_TOKEN.
    mkdir "$work/docker"
    DOCKER_CONFIG="$work/docker" docker pull --platform linux/amd64 "$image"
    ;;
  *) echo "Unknown artifact mode: $mode" >&2; exit 1 ;;
esac

cp "$repo/scripts/release/smoke_sdk.mjs" "$work/node/smoke_sdk.mjs"
"$work/core/bin/playwright" install --with-deps chromium
cd "$work"
python "$repo/scripts/release/smoke_api.py" "$version" --core-python "$work/core/bin/python" \
  --clients-python "$work/clients/bin/python" --node-root "$work/node"
python "$repo/scripts/release/smoke_api.py" "$version" --image "$image" \
  --clients-python "$work/clients/bin/python" --node-root "$work/node"
if [ "$mode" = built ]; then
  docker save "$image" -o "$repo/release-image.tar"
fi
