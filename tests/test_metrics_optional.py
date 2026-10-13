"""The base installation must work without the metrics extra."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from blazecrawl_core.config import Settings


def test_configuration_defaults_off(monkeypatch):
    monkeypatch.delenv("METRICS_ENABLED", raising=False)
    assert Settings().METRICS_ENABLED is False
    monkeypatch.setenv("METRICS_ENABLED", "true")
    assert Settings().METRICS_ENABLED is True


def test_prometheus_is_only_an_optional_dependency():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    assert not any("prometheus" in dependency for dependency in project["dependencies"])
    assert project["optional-dependencies"]["metrics"] == ["prometheus-client>=0.21.0"]


@pytest.mark.parametrize("enabled", [None, False, True])
def test_optional_dependency_is_not_imported_by_default(enabled):
    code = """
import importlib.abc
import sys

class NoPrometheus(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "prometheus_client" or fullname.startswith("prometheus_client."):
            raise ModuleNotFoundError("blocked optional package", name="prometheus_client")

sys.meta_path.insert(0, NoPrometheus())
from blazecrawl_core.api.main import app
from blazecrawl_core.engine.browser_pool import get_browser_pool
assert get_browser_pool().metrics is None
assert all(route.path != "/metrics" for route in app.routes)
assert "prometheus_client" not in sys.modules
assert "blazecrawl_core.engine.metrics" not in sys.modules
from fastapi.testclient import TestClient
assert TestClient(app).get("/metrics").status_code == 404
"""
    env = os.environ.copy()
    env.pop("METRICS_ENABLED", None)
    if enabled is not None:
        env["METRICS_ENABLED"] = str(enabled).lower()
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
    )
    if enabled:
        assert result.returncode != 0
        assert "METRICS_ENABLED=true requires the metrics extra" in result.stderr
        assert 'pip install "blazecrawl-core[metrics]"' in result.stderr
    else:
        assert result.returncode == 0, result.stderr
