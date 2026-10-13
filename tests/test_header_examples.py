"""Run both contributor examples against a local mock of the current API."""

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("language", ["python", "node"])
def test_custom_header_example_runs_with_current_sdk_response(language):
    if language == "node" and shutil.which("node") is None:
        pytest.skip("Node is required to run the Node SDK example")
    received = []

    class MockAPI(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, dict(self.headers), body))
            website_headers = {"user-agent": "BlazeCrawl/0.1.0"}
            website_headers.update({k.lower(): v for k, v in body["headers"].items()})
            payload = json.dumps(
                {"success": True, "data": {"plaintext": json.dumps(website_headers)}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        **os.environ,
        "BLAZECRAWL_API_URL": f"http://127.0.0.1:{server.server_port}",
        "BLAZECRAWL_API_KEY": "fake-local-example-api-key",
        "BLAZECRAWL_HEADER_DEMO_URL": "https://echo.example.test/headers",
        "PYTHONPATH": str(ROOT / "sdks" / "python"),
    }
    command = (
        [sys.executable, str(ROOT / "examples" / "custom_headers.py")]
        if language == "python"
        else ["node", str(ROOT / "examples" / "custom_headers.mjs")]
    )
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 0, result.stderr
    assert len(received) == 5
    for path, raw_headers, body in received:
        headers = {k.lower(): v for k, v in raw_headers.items()}
        assert path == "/v1/scrape"
        assert headers["authorization"] == "Bearer fake-local-example-api-key"
        assert body["url"] == "https://echo.example.test/headers"
        assert body["formats"] == ["text"]
        assert body["render"] == "static"
        assert body["only_main_content"] is False
        assert "fake-local-example-api-key" not in json.dumps(body)
    assert received[0][2]["headers"]["user-agent"] == "MyBot/1.0 (contact@example.com)"
    assert received[1][2]["headers"]["Accept-Language"] == "fr-FR,fr;q=0.9"
    assert received[2][2]["headers"]["Authorization"] == "Bearer fake-example-token"
    assert received[3][2]["headers"]["Accept-Language"] == "de-DE,de;q=0.9"
    assert received[4][2]["headers"] == {}
    for expected in ("MyBot/1.0", "fr-FR", "de-DE", "fake-example-token", "BlazeCrawl/0.1.0"):
        assert expected in result.stdout
    assert "fake-local-example-api-key" not in result.stdout
    assert "undefined" not in result.stdout
