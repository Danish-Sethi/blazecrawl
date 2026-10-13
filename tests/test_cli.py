"""CLI output tests using an in-memory HTTP transport."""

import base64
import json

import httpx
import pytest

from blazecrawl_core import cli

PNG = b"\x89PNG\r\n\x1a\nfake-png-body"


def _capture_scrape(monkeypatch, response):
    seen = {}

    def handle(request):
        seen["body"] = json.loads(request.content)
        return response

    monkeypatch.setattr(
        cli,
        "_client",
        lambda: httpx.Client(
            base_url="https://blazecrawl.test", transport=httpx.MockTransport(handle)
        ),
    )
    return seen


@pytest.fixture
def mock_scrape(monkeypatch):
    def configure(response):
        def handle(request):
            assert request.method == "POST"
            assert request.url.path == "/v1/scrape"
            assert json.loads(request.content)["url"] == "https://example.com"
            return response

        monkeypatch.setattr(
            cli,
            "_client",
            lambda: httpx.Client(
                base_url="https://blazecrawl.test", transport=httpx.MockTransport(handle)
            ),
        )

    return configure


@pytest.mark.parametrize("nested", [True, False])
def test_scrape_output_writes_markdown(tmp_path, capsys, mock_scrape, nested):
    markdown = "# 안녕하세요\n\nA page with Unicode: café."
    data = {"markdown": markdown}
    mock_scrape(httpx.Response(200, json={"data": data} if nested else data))
    output = tmp_path / "page with spaces.md"

    assert cli.main(["scrape", "https://example.com", "--output", str(output)]) == 0

    assert output.read_text(encoding="utf-8") == markdown + "\n"
    assert capsys.readouterr() == ("", "")


def test_scrape_output_json(tmp_path, capsys, mock_scrape):
    data = {"success": True, "data": {"markdown": "# Example"}}
    mock_scrape(httpx.Response(200, json=data))
    output = tmp_path / "page.json"

    assert cli.main(["scrape", "https://example.com", "--json", "--output", str(output)]) == 0

    assert json.loads(output.read_text(encoding="utf-8")) == data
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("exists", [True, False])
def test_scrape_error_does_not_write_output(tmp_path, capsys, mock_scrape, exists):
    mock_scrape(httpx.Response(400, json={"detail": "url_rejected"}))
    output = tmp_path / "page.md"
    if exists:
        output.write_text("keep this", encoding="utf-8")

    assert cli.main(["scrape", "https://example.com", "--output", str(output)]) == 1

    if exists:
        assert output.read_text(encoding="utf-8") == "keep this"
    else:
        assert not output.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "url_rejected" in captured.err


def test_scrape_output_write_error(tmp_path, capsys, mock_scrape):
    mock_scrape(httpx.Response(200, json={"data": {"markdown": "# Example"}}))

    assert cli.main(["scrape", "https://example.com", "--output", str(tmp_path)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Could not write output" in captured.err
    assert str(tmp_path) in captured.err


@pytest.mark.parametrize("as_json", [True, False])
def test_scrape_without_output_keeps_stdout(capsys, mock_scrape, as_json):
    data = {"data": {"markdown": "# Example"}}
    mock_scrape(httpx.Response(200, json=data))

    assert cli.main(["scrape", "https://example.com"] + (["--json"] if as_json else [])) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    if as_json:
        assert json.loads(captured.out) == data
    else:
        assert captured.out == "# Example\n"


def test_scrape_screenshot_writes_png_bytes_and_sends_full_page(tmp_path, capsys, monkeypatch):
    encoded = base64.b64encode(PNG).decode("ascii")
    seen = _capture_scrape(monkeypatch, httpx.Response(200, json={"data": {"screenshot": encoded}}))
    output = tmp_path / "shot.png"

    argv = ["scrape", "https://example.com", "--formats", "screenshot", "--full-page"]
    assert cli.main(argv + ["--output", str(output)]) == 0

    assert output.read_bytes() == PNG
    assert seen["body"]["formats"] == ["screenshot"]
    assert seen["body"]["full_page"] is True
    assert capsys.readouterr() == ("", "")


def test_scrape_screenshot_without_output_or_json_is_a_usage_error(capsys, monkeypatch):
    seen = _capture_scrape(monkeypatch, httpx.Response(200, json={"data": {}}))

    assert cli.main(["scrape", "https://example.com", "--formats", "screenshot"]) == 2

    assert seen == {}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--output" in captured.err


def test_scrape_screenshot_json_keeps_full_response(capsys, monkeypatch):
    encoded = base64.b64encode(PNG).decode("ascii")
    data = {"success": True, "data": {"screenshot": encoded}}
    _capture_scrape(monkeypatch, httpx.Response(200, json=data))

    assert cli.main(["scrape", "https://example.com", "--formats", "screenshot", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == data


def test_scrape_screenshot_error_writes_nothing(tmp_path, capsys, monkeypatch):
    _capture_scrape(monkeypatch, httpx.Response(422, json={"detail": "requires browser rendering"}))
    output = tmp_path / "shot.png"

    argv = ["scrape", "https://example.com", "--formats", "screenshot", "--render", "static"]
    assert cli.main(argv + ["--output", str(output)]) == 1

    assert not output.exists()
    assert "requires browser rendering" in capsys.readouterr().err
