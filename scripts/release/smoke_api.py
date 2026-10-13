"""Bounded live smoke of an installed core wheel or container, never source imports."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PUBLIC_PAGE = "https://example.com"
CRAWL_SEED = "https://quotes.toscrape.com/"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def run(command: list[str], **kwargs) -> str:
    return subprocess.check_output(command, text=True, timeout=180, **kwargs).strip()


def request(base: str, key: str | None, path: str, body=None, *, status=200):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    try:
        response = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        data = json.load(response)
        assert response.code == status, f"{path}: HTTP {response.code}, expected {status}: {data}"
        return data


def wait_ready(base: str, version: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            health = request(base, None, "/health")
        except urllib.error.URLError:
            time.sleep(1)
            continue
        assert health["version"] == version, health
        ready = request(base, None, "/ready")
        assert ready["ready"] and ready["browser"]["is_healthy"], ready
        return
    raise TimeoutError("API failed to start within 90s; see service log")


def poll(base: str, key: str, path: str, predicate) -> dict:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = request(base, key, path)
        assert job["status"] not in ("failed", "cancelled"), job
        assert job["pages_failed"] == 0, job
        if predicate(job):
            return job
        time.sleep(0.2)
    raise TimeoutError(f"Crawl did not reach expected state in 120s: {job}")


def smoke(args) -> None:
    with tempfile.TemporaryDirectory(prefix="blazecrawl-smoke-") as directory:
        root = Path(directory)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        key = secrets.token_urlsafe(32)
        env = {
            **os.environ,
            "BLAZECRAWL_API_URL": base,
            "BLAZECRAWL_API_KEY": key,
            "BLAZECRAWL_AUTH_DISABLED": "false",
            "BLAZECRAWL_HOST": "127.0.0.1",
            "BLAZECRAWL_PORT": str(port),
            "BLAZECRAWL_STATE_DIR": str(root / "state"),
            "CRAWL_CHECKPOINTS_ENABLED": "true",
            # Leave enough time to stop after the first committed page.
            "CRAWL_HOST_RATE": "0.1",
            "CRAWL_HOST_BURST": "1",
            "BROWSER_POOL_SIZE": "1",
            "METRICS_ENABLED": "false",
            "REDIS_URL": "",
        }
        env.pop("EGRESS_UPSTREAM_PROXY", None)
        name = f"blazecrawl-smoke-{uuid.uuid4().hex}"
        volume = name + "-state"
        process = None
        container_started = False
        log = (root / "server.log").open("w", encoding="utf-8")
        if args.image:
            run(["docker", "volume", "create", volume])
            cli = ["docker", "exec", name, "blazecrawl"]
        else:
            python = str(Path(args.core_python).resolve())
            bin_dir = Path(python).parent
            cli = [str(bin_dir / ("blazecrawl.exe" if os.name == "nt" else "blazecrawl"))]
            reported = run(
                [python, "-I", "-c", "import blazecrawl_core; print(blazecrawl_core.__version__)"],
                cwd=root,
            )
            assert reported == args.version, reported

        def start():
            nonlocal process, container_started
            if args.image:
                command = [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--platform",
                    "linux/amd64",
                    "-p",
                    f"127.0.0.1:{port}:8000",
                    "-v",
                    f"{volume}:/data/blazecrawl",
                    "-e",
                    "BLAZECRAWL_API_KEY",
                    "-e",
                    "CRAWL_CHECKPOINTS_ENABLED",
                    "-e",
                    "CRAWL_HOST_RATE",
                    "-e",
                    "CRAWL_HOST_BURST",
                    "-e",
                    "BROWSER_POOL_SIZE",
                    args.image,
                ]
                run(command, env=env, cwd=root)
                container_started = True
            else:
                process = subprocess.Popen(
                    [python, "-I", "-m", "blazecrawl_core.api.main"],
                    cwd=root,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
            wait_ready(base, args.version)

        def stop():
            nonlocal process, container_started
            if container_started:
                run(["docker", "stop", "--time", "30", name])
                # Logs cannot contain the explicit key; it is never bootstrapped/printed.
                with (root / "server.log").open("a", encoding="utf-8") as output:
                    subprocess.run(
                        ["docker", "logs", name], stdout=output, stderr=output, check=True
                    )
                run(["docker", "rm", name])
                container_started = False
            if process is not None:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired as error:
                    process.kill()
                    process.wait(timeout=10)
                    raise TimeoutError("Core did not shut down within 30s") from error
                finally:
                    process = None

        try:
            start()
            assert run([*cli, "--version"], env=env, cwd=root) == f"blazecrawl-core {args.version}"
            assert json.loads(run([*cli, "health"], env=env, cwd=root))["version"] == args.version
            body = {
                "url": PUBLIC_PAGE,
                "render": "static",
                "formats": ["markdown"],
                "use_cache": False,
                "only_main_content": False,
            }
            request(base, None, "/v1/scrape", body, status=401)
            request(base, key, "/v1/scrape", {**body, "url": "http://127.0.0.1/"}, status=400)
            data = request(base, key, "/v1/scrape", body)
            assert data["success"] and "Example Domain" in data["data"]["markdown"], data
            assert (
                "domain"
                in run(
                    [*cli, "scrape", PUBLIC_PAGE, "--render", "static"], env=env, cwd=root
                ).lower()
            )
            data = request(
                base,
                key,
                "/v1/scrape",
                {
                    **body,
                    "render": "browser",
                    "formats": ["markdown", "screenshot"],
                    "full_page": True,
                },
            )
            assert data["success"] and "Example Domain" in data["data"]["markdown"], data
            png = base64.b64decode(data["data"]["screenshot"], validate=True)
            assert png.startswith(PNG_MAGIC) and 100 < len(png) <= 5 * 1024 * 1024
            print(
                "PASS runtime/CLI/health, bearer auth, SSRF, live static + browser/PNG", flush=True
            )

            created = request(
                base,
                key,
                "/v1/crawl",
                {
                    "url": CRAWL_SEED,
                    "max_pages": 2,
                    "max_depth": 1,
                },
                status=202,
            )
            path = created["status_url"]
            before = poll(base, key, path, lambda job: job["pages_crawled"] >= 1)
            assert before["status"] == "running" and before["pages_crawled"] == 1, before
            stop()
            start()
            after = poll(base, key, path, lambda job: job["status"] == "completed")
            assert after["job_id"] == created["job_id"]
            assert after["created_at"] == before["created_at"]
            assert after["pages_crawled"] == len(after["pages"]) == after["max_pages"] == 2, after
            assert after["max_depth"] == 1 and after["pages"][0] == before["pages"][0], after
            assert len({page["url"] for page in after["pages"]}) == 2, after
            assert all(page["markdown"] for page in after["pages"]), after
            print(
                "PASS bounded live crawl, polling, restart-resume with retained first page",
                flush=True,
            )

            if args.clients_python:
                scripts = Path(__file__).resolve().parent
                subprocess.run(
                    [
                        args.clients_python,
                        "-I",
                        str(scripts / "smoke_clients.py"),
                        args.version,
                        "--node-root",
                        args.node_root,
                    ],
                    env=env,
                    cwd=root,
                    check=True,
                    timeout=180,
                )
                subprocess.run(
                    ["node", str(Path(args.node_root) / "smoke_sdk.mjs"), args.version],
                    env=env,
                    cwd=root,
                    check=True,
                    timeout=180,
                )
        finally:
            try:
                stop()
            finally:
                log.close()
                if args.image:
                    run(["docker", "volume", "rm", volume])
                # Print diagnostics on failure, never credentials or screenshots.
                import sys

                if sys.exc_info()[0] is not None:
                    print((root / "server.log").read_text(encoding="utf-8"), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--core-python")
    target.add_argument("--image")
    parser.add_argument("--clients-python")
    parser.add_argument("--node-root")
    arguments = parser.parse_args()
    if bool(arguments.clients_python) != bool(arguments.node_root):
        parser.error("--clients-python and --node-root must be provided together")
    smoke(arguments)
