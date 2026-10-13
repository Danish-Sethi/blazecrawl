import json
import shutil
from pathlib import Path

import pytest

from scripts.release import smoke_api
from scripts.release.versions import check

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "pyproject.toml",
    "sdks/python/pyproject.toml",
    "mcp/python/pyproject.toml",
    "sdks/node/package.json",
    "mcp/node/package.json",
    "sdks/node/package-lock.json",
    "mcp/node/package-lock.json",
    "uv.lock",
)


def test_release_versions():
    assert len(check(ROOT, "0.2.0")) == 10


@pytest.mark.parametrize("file", FILES)
def test_rejects_version_drift(tmp_path, file):
    for name in FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    target = tmp_path / file
    target.write_text(target.read_text().replace("0.2.0", "9.8.7"))
    with pytest.raises(ValueError, match="does not match"):
        check(tmp_path, "0.2.0")


@pytest.mark.parametrize("field", ["root", "package"])
def test_rejects_independent_npm_lock_drift(tmp_path, field):
    for name in FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    target = tmp_path / "sdks/node/package-lock.json"
    lock = json.loads(target.read_text())
    (lock if field == "root" else lock["packages"][""])["version"] = "0.1.2"
    target.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="does not match"):
        check(tmp_path, "0.2.0")


@pytest.mark.parametrize("version", ["v0.2.0", "0.2", "0.2.0-extra", "0.2.0\n"])
def test_rejects_invalid_release_version(version):
    with pytest.raises(ValueError, match="X.Y.Z"):
        check(ROOT, version)


def test_readiness_retries_startup_reset(monkeypatch):
    replies = iter(
        [
            ConnectionResetError("container listener is starting"),
            {"version": "0.2.0"},
            {"ready": True, "browser": {"is_healthy": True}},
        ]
    )

    def request(*args, **kwargs):
        result = next(replies)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(smoke_api, "request", request)
    monkeypatch.setattr(smoke_api.time, "sleep", lambda _: None)
    smoke_api.wait_ready("http://127.0.0.1:8000", "0.2.0")


def test_readiness_does_not_hide_version_mismatch(monkeypatch):
    monkeypatch.setattr(smoke_api, "request", lambda *args, **kwargs: {"version": "0.1.2"})
    with pytest.raises(AssertionError):
        smoke_api.wait_ready("http://127.0.0.1:8000", "0.2.0")


def test_readiness_does_not_accept_degraded_browser(monkeypatch):
    replies = iter(
        [
            {"version": "0.2.0"},
            {"ready": True, "browser": {"is_healthy": False}},
        ]
    )
    monkeypatch.setattr(smoke_api, "request", lambda *args, **kwargs: next(replies))
    with pytest.raises(AssertionError):
        smoke_api.wait_ready("http://127.0.0.1:8000", "0.2.0")
