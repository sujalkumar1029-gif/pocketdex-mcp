"""Tests for pocketdex. Run with: uv run --with pytest pytest -q"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pocketdex  # noqa: E402

SCRIPT = ROOT / "pocketdex.py"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def parse_amount_cents(value):\n"
        "    return int(value)\n"
        "\n"
        "def unused_helper():\n"
        "    pass\n"
    )
    (root / "README.md").write_text("# Demo\n\nparse_amount_cents is used here.\n")
    junk = root / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "index.js").write_text("parse_amount_cents SHOULD_NOT_BE_INDEXED\n")
    (root / "blob.bin").write_bytes(b"\x00\x01parse_amount_cents\x00")
    return root


def test_index_and_search(data_dir: Path, project: Path) -> None:
    result = pocketdex.index_project(project, data_dir=data_dir)

    assert result["files_indexed"] == 2
    assert result["chunks"] >= 2
    assert Path(result["db"]).parent == data_dir
    assert Path(result["db"]).parent != project

    found = pocketdex.search(project, "parse_amount_cents", data_dir=data_dir)
    paths = [item["path"] for item in found["results"]]
    assert "src/app.py" in paths
    assert "README.md" in paths
    assert all("node_modules" not in path for path in paths)
    assert "blob.bin" not in paths

    app_hit = next(item for item in found["results"] if item["path"] == "src/app.py")
    assert app_hit["lines"].startswith("1-")
    assert "parse_amount_cents" in app_hit["snippet"]


def test_prefix_and_underscore_token(data_dir: Path, project: Path) -> None:
    pocketdex.index_project(project, data_dir=data_dir)

    found = pocketdex.search(project, "parse_amount", data_dir=data_dir)
    assert found["count"] >= 1
    assert any(item["path"] == "src/app.py" for item in found["results"])


def test_path_prefix_filters(data_dir: Path, project: Path) -> None:
    pocketdex.index_project(project, data_dir=data_dir)

    only_src = pocketdex.search(
        project, "parse_amount_cents", path_prefix="src", data_dir=data_dir
    )
    assert only_src["count"] >= 1
    assert all(item["path"].startswith("src/") for item in only_src["results"])

    only_docs = pocketdex.search(
        project, "parse_amount_cents", path_prefix="README", data_dir=data_dir
    )
    assert only_docs["count"] == 1


def test_incremental_reindex(data_dir: Path, project: Path) -> None:
    pocketdex.index_project(project, data_dir=data_dir)

    app = project / "src" / "app.py"
    app.write_text(app.read_text() + "\ndef brand_new_symbol():\n    return 42\n")

    second = pocketdex.index_project(project, data_dir=data_dir)
    assert second["files_indexed"] == 1
    assert second["files_unchanged"] == 1
    assert second["files_skipped"] == 1  # blob.bin is binary

    found = pocketdex.search(project, "brand_new_symbol", data_dir=data_dir)
    assert found["count"] == 1
    assert found["results"][0]["path"] == "src/app.py"


def test_deleted_file_is_removed(data_dir: Path, project: Path) -> None:
    pocketdex.index_project(project, data_dir=data_dir)

    (project / "README.md").unlink()
    result = pocketdex.index_project(project, data_dir=data_dir)
    assert result["files_removed"] == 1

    found = pocketdex.search(project, "parse_amount_cents", data_dir=data_dir)
    assert all(item["path"] != "README.md" for item in found["results"])
    report = pocketdex.stats(project, data_dir=data_dir)
    assert report["projects"][0]["files"] == 1


def test_stats(data_dir: Path, project: Path) -> None:
    pocketdex.index_project(project, data_dir=data_dir)

    report = pocketdex.stats(project, data_dir=data_dir)
    assert Path(report["data_dir"]) == data_dir
    entry = report["projects"][0]
    assert entry["project"] == str(project.resolve())
    assert entry["files"] == 2
    assert entry["chunks"] >= 2
    assert entry["db_bytes"] > 0


def test_projects_are_isolated(data_dir: Path, tmp_path: Path) -> None:
    first = tmp_path / "alpha"
    second = tmp_path / "beta"
    first.mkdir()
    second.mkdir()
    (first / "a.py").write_text("shared_identifier = 'alpha'\n")
    (second / "b.py").write_text("shared_identifier = 'beta'\n")

    one = pocketdex.index_project(first, data_dir=data_dir)
    two = pocketdex.index_project(second, data_dir=data_dir)
    assert one["db"] != two["db"]

    only_first = pocketdex.search(first, "shared_identifier", data_dir=data_dir)
    assert only_first["count"] == 1
    assert only_first["results"][0]["path"] == "a.py"

    only_second = pocketdex.search(second, "shared_identifier", data_dir=data_dir)
    assert only_second["count"] == 1
    assert only_second["results"][0]["path"] == "b.py"

    # Omitting the project uses the most recently indexed one (beta).
    latest = pocketdex.search(None, "shared_identifier", data_dir=data_dir)
    assert latest["results"][0]["path"] == "b.py"

    assert len(pocketdex.stats(data_dir=data_dir)["projects"]) == 2


def test_unknown_project_is_a_clear_error(data_dir: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(pocketdex.PocketdexError, match="not indexed"):
        pocketdex.search(other, "anything", data_dir=data_dir)


def test_search_before_any_index(data_dir: Path) -> None:
    with pytest.raises(pocketdex.PocketdexError, match="no projects"):
        pocketdex.search(None, "anything", data_dir=data_dir)


class MCPClient:
    def __init__(self, data_dir: Path) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "--data-dir",
                str(data_dir),
                "serve",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def request(self, method: str, params: dict | None = None, req_id: int = 1) -> dict:
        assert self.proc.stdin and self.proc.stdout
        message = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            message["params"] = params
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"no response; stderr={self.proc.stderr.read() if self.proc.stderr else ''}"
        return json.loads(line)

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait(timeout=10)


def test_mcp_handshake_and_tools(data_dir: Path, project: Path) -> None:
    client = MCPClient(data_dir)
    try:
        init = client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        )
        assert init["result"]["serverInfo"]["name"] == "pocketdex"
        assert init["result"]["protocolVersion"] == "2025-06-18"

        listed = client.request("tools/list", req_id=2)
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert names == {"index_project", "search", "stats"}

        indexed = client.request(
            "tools/call",
            {"name": "index_project", "arguments": {"root": str(project)}},
            req_id=3,
        )
        assert indexed["result"]["isError"] is False
        payload = json.loads(indexed["result"]["content"][0]["text"])
        assert payload["files_indexed"] == 2
        assert Path(payload["db"]).parent == data_dir

        searched = client.request(
            "tools/call",
            {"name": "search", "arguments": {"query": "unused_helper", "limit": 5}},
            req_id=4,
        )
        assert searched["result"]["isError"] is False
        payload = json.loads(searched["result"]["content"][0]["text"])
        assert payload["count"] == 1
        assert payload["results"][0]["path"] == "src/app.py"

        bad = client.request(
            "tools/call", {"name": "search", "arguments": {"query": ""}}, req_id=5
        )
        assert bad["result"]["isError"] is True
    finally:
        client.close()


def test_mcp_unknown_method(data_dir: Path) -> None:
    client = MCPClient(data_dir)
    try:
        response = client.request("nope/nope", req_id=1)
        assert response["error"]["code"] == -32601
    finally:
        client.close()
