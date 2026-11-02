#!/usr/bin/env python3
"""pocketdex — a tiny local code index, one database per project.

One file, no dependencies, no background server. Each project gets its own
SQLite FTS5 database inside pocketdex's own data folder, so indexes never mix.

    pocketdex index ~/Projects/my-app
    pocketdex search "parse_amount_cents"
    pocketdex stats
    pocketdex serve            # MCP stdio server
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

VERSION = "0.2.0"
SCHEMA_VERSION = "1"

PACKAGE_DIR = Path(__file__).resolve().parent


def default_data_dir() -> Path:
    """Repo-local data for a clone; user data dir for an installed package."""
    env = os.environ.get("POCKETDEX_DATA")
    if env:
        return Path(env).expanduser()
    local = PACKAGE_DIR / "data" / "projects"
    try:
        local.parent.mkdir(parents=True, exist_ok=True)
        if os.access(local.parent, os.W_OK):
            return local
    except OSError:
        pass
    return Path("~/.local/share/pocketdex/projects").expanduser()

# Directories that never contain hand-written source worth indexing.
IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "env",
    "node_modules", "bower_components", "dist", "build", "out", "target",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".nox", ".eggs", "coverage", "htmlcov", ".next", ".nuxt", ".cache",
    "vendor", "third_party", "site-packages",
}

# Lockfiles and generated maps are noise for code search.
IGNORE_NAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    "poetry.lock", "uv.lock", "Pipfile.lock", "Cargo.lock", "composer.lock",
    "Gemfile.lock",
}

MAX_FILE_BYTES = 512 * 1024
MAX_LINE_LENGTH = 1000
CHUNK_LINES = 40
CHUNK_OVERLAP = 12
MAX_TOKENS_IN_QUERY = 8

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY,
    root       TEXT NOT NULL UNIQUE,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    size       INTEGER NOT NULL,
    UNIQUE (project_id, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL,
    path       TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    content    TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    path,
    content='chunks',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2 tokenchars '_'",
    prefix='2 3'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content, path)
    VALUES (new.id, new.content, new.path);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content, path)
    VALUES ('delete', old.id, old.content, old.path);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content, path)
    VALUES ('delete', old.id, old.content, old.path);
    INSERT INTO chunks_fts(rowid, content, path)
    VALUES (new.id, new.content, new.path);
END;

CREATE INDEX IF NOT EXISTS idx_chunks_project_path
    ON chunks(project_id, path);
"""


class PocketdexError(Exception):
    """A problem the user (or agent) can fix: no index, missing folder."""


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)

    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
    elif row["value"] != SCHEMA_VERSION:
        raise PocketdexError(
            f"index schema {row['value']} is not version {SCHEMA_VERSION}; "
            f"delete {db_path} and re-index"
        )
    return conn


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_root(root: str | Path) -> Path:
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise PocketdexError(f"not a directory: {path}")
    return path


def data_dir_path(data_dir: Path | str | None) -> Path:
    return Path(data_dir if data_dir is not None else default_data_dir()).expanduser()


def project_db_path(root: str | Path, data_dir: Path | str | None = None) -> Path:
    """One stable database file per absolute project path."""
    root = normalize_root(root)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip(".-") or "project"
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
    return data_dir_path(data_dir) / f"{slug}-{digest}.db"


def get_or_create_project(conn: sqlite3.Connection, root: Path) -> int:
    row = conn.execute("SELECT id FROM projects WHERE root = ?", (str(root),)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO projects(root, indexed_at) VALUES(?, ?)", (str(root), utc_now())
    )
    return int(cur.lastrowid)


# --------------------------------------------------------------------------
# indexing
# --------------------------------------------------------------------------


def iter_source_files(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if name in IGNORE_NAMES or name.endswith(".min.js"):
                continue
            path = Path(dirpath) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size == 0 or stat.st_size > MAX_FILE_BYTES:
                continue
            yield path, stat


def read_source(path: Path) -> str | None:
    """Return UTF-8 text, or None for binary / unreadable files."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # Minified bundles: a couple of enormous lines, no structure.
    if lines and len(lines) <= 3 and max(len(line) for line in lines) > MAX_LINE_LENGTH:
        return None
    return text


def chunk_text(text: str) -> Iterator[tuple[int, int, str]]:
    lines = text.splitlines()
    if not lines:
        return
    step = CHUNK_LINES - CHUNK_OVERLAP
    start = 0
    while start < len(lines):
        end = min(start + CHUNK_LINES, len(lines))
        yield start + 1, end, "\n".join(lines[start:end])
        if end == len(lines):
            break
        start += step


def index_project(
    root: str | Path,
    force: bool = False,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    root = normalize_root(root)
    db_path = project_db_path(root, data_dir)
    conn = connect(db_path)
    started = time.perf_counter()

    conn.execute("BEGIN IMMEDIATE")
    try:
        project_id = get_or_create_project(conn, root)
        existing = {
            row["path"]: row
            for row in conn.execute(
                "SELECT id, path, mtime_ns, size FROM files WHERE project_id = ?",
                (project_id,),
            )
        }

        seen: set[str] = set()
        indexed = unchanged = skipped = removed = chunks_written = 0

        for path, stat in iter_source_files(root):
            rel = path.relative_to(root).as_posix()
            seen.add(rel)
            prior = existing.get(rel)
            if (
                not force
                and prior is not None
                and int(prior["mtime_ns"]) == stat.st_mtime_ns
                and int(prior["size"]) == stat.st_size
            ):
                unchanged += 1
                continue

            text = read_source(path)
            if text is None:
                if prior is not None:
                    conn.execute(
                        "DELETE FROM chunks WHERE project_id = ? AND path = ?",
                        (project_id, rel),
                    )
                    conn.execute(
                        "DELETE FROM files WHERE project_id = ? AND path = ?",
                        (project_id, rel),
                    )
                    removed += 1
                skipped += 1
                continue

            # Delete old chunks first so the FTS triggers fire cleanly.
            conn.execute(
                "DELETE FROM chunks WHERE project_id = ? AND path = ?",
                (project_id, rel),
            )
            conn.execute(
                "DELETE FROM files WHERE project_id = ? AND path = ?",
                (project_id, rel),
            )
            cur = conn.execute(
                "INSERT INTO files(project_id, path, mtime_ns, size) VALUES(?,?,?,?)",
                (project_id, rel, stat.st_mtime_ns, stat.st_size),
            )
            file_id = int(cur.lastrowid)

            for start, end, content in chunk_text(text):
                conn.execute(
                    "INSERT INTO chunks(file_id, project_id, path, start_line, "
                    "end_line, content) VALUES(?,?,?,?,?,?)",
                    (file_id, project_id, rel, start, end, content),
                )
                chunks_written += 1
            indexed += 1

        for rel, prior in existing.items():
            if rel not in seen:
                conn.execute("DELETE FROM files WHERE id = ?", (int(prior["id"]),))
                removed += 1

        conn.execute(
            "UPDATE projects SET indexed_at = ? WHERE id = ?",
            (utc_now(), project_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise

    result = {
        "project": str(root),
        "db": str(db_path),
        "files_indexed": indexed,
        "files_unchanged": unchanged,
        "files_skipped": skipped,
        "files_removed": removed,
        "chunks": chunks_written,
        "seconds": round(time.perf_counter() - started, 2),
    }
    conn.close()
    return result


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


def build_match(query: str) -> str | None:
    tokens = TOKEN_RE.findall(query)[:MAX_TOKENS_IN_QUERY]
    if not tokens:
        return None
    return " AND ".join(f'"{token}"*' for token in tokens)


def _rows_to_results(rows: Iterable[sqlite3.Row], limit: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    per_file: dict[str, int] = {}
    for row in rows:
        path = str(row["path"])
        if per_file.get(path, 0) >= 2:
            continue
        per_file[path] = per_file.get(path, 0) + 1
        results.append(
            {
                "path": path,
                "lines": f"{row['start_line']}-{row['end_line']}",
                "score": round(float(-row["rank"]), 3),
                "snippet": str(row["snippet"]),
            }
        )
        if len(results) >= limit:
            break
    return results


def latest_root(data_dir: Path | str | None = None) -> Path:
    """Most recently indexed project, read straight from the per-project DBs."""
    directory = data_dir_path(data_dir)
    best: tuple[str, str] | None = None
    for db_path in sorted(directory.glob("*.db")) if directory.is_dir() else []:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT root, indexed_at FROM projects ORDER BY indexed_at DESC LIMIT 1"
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            continue
        if row is not None:
            candidate = (str(row["root"]), str(row["indexed_at"]))
            if best is None or candidate[1] > best[1]:
                best = candidate

    if best is None:
        raise PocketdexError(
            f"no projects indexed yet in {directory}; call index_project first"
        )
    return Path(best[0])


def project_rows(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, root, indexed_at FROM projects ORDER BY id LIMIT 1"
    ).fetchone()


def list_projects(data_dir: Path | str | None = None) -> list[dict[str, Any]]:
    directory = data_dir_path(data_dir)
    items: list[dict[str, Any]] = []
    for db_path in sorted(directory.glob("*.db")) if directory.is_dir() else []:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            row = project_rows(conn)
            if row is None:
                conn.close()
                continue
            pid = int(row["id"])
            files = conn.execute(
                "SELECT COUNT(*) AS n FROM files WHERE project_id=?", (pid,)
            ).fetchone()["n"]
            chunks = conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE project_id=?", (pid,)
            ).fetchone()["n"]
            chars = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(content)),0) AS n FROM chunks WHERE project_id=?",
                (pid,),
            ).fetchone()["n"]
            conn.close()
        except sqlite3.Error:
            continue

        items.append(
            {
                "project": str(row["root"]),
                "db": str(db_path),
                "db_bytes": db_path.stat().st_size,
                "files": int(files),
                "chunks": int(chunks),
                "indexed_chars": int(chars),
                "indexed_at": str(row["indexed_at"]),
            }
        )

    items.sort(key=lambda item: item["indexed_at"], reverse=True)
    return items


def open_indexed(root: str | Path, data_dir: Path | str | None = None) -> sqlite3.Connection:
    root = normalize_root(root)
    db_path = project_db_path(root, data_dir)
    if not db_path.exists():
        raise PocketdexError(
            f"{root} is not indexed yet; call index_project with that path first"
        )
    conn = connect(db_path)
    if project_rows(conn) is None:
        conn.close()
        raise PocketdexError(f"{root} has an empty index; re-index it")
    return conn


def search(
    root: str | Path | None,
    query: str,
    limit: int = 8,
    path_prefix: str | None = None,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    root = normalize_root(root) if root else latest_root(data_dir)
    conn = open_indexed(root, data_dir)
    limit = max(1, min(int(limit), 25))
    match = build_match(query)
    if match is None:
        conn.close()
        return {
            "query": query,
            "project": str(root),
            "db": str(project_db_path(root, data_dir)),
            "count": 0,
            "results": [],
        }

    pid = int(project_rows(conn)["id"])

    def run(match_expression: str) -> list[sqlite3.Row]:
        where = ["chunks_fts MATCH ?", "c.project_id = ?"]
        params: list[Any] = [match_expression, pid]
        if path_prefix:
            where.append("c.path LIKE ?")
            params.append(path_prefix.rstrip("/") + "%")
        sql = f"""
            SELECT c.path, c.start_line, c.end_line,
                   snippet(chunks_fts, 0, '**', '**', '…', 12) AS snippet,
                   bm25(chunks_fts, 1.0, 0.4) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            WHERE {' AND '.join(where)}
            ORDER BY rank
            LIMIT ?
        """
        return conn.execute(sql, (*params, limit * 6)).fetchall()

    results = _rows_to_results(run(match), limit)

    if not results:
        # Second chance: split identifiers on _ and camelCase, match any part.
        parts: list[str] = []
        for token in TOKEN_RE.findall(query)[:MAX_TOKENS_IN_QUERY]:
            parts.extend(p for p in re.split(r"_+|(?<=[a-z])(?=[A-Z])", token) if p)
        if parts:
            fallback = " OR ".join(f'"{part}"*' for part in dict.fromkeys(parts))
            results = _rows_to_results(run(fallback), limit)

    response = {
        "query": query,
        "project": str(root),
        "db": str(project_db_path(root, data_dir)),
        "count": len(results),
        "results": results,
    }
    conn.close()
    return response


def stats(
    project: str | Path | None = None,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    directory = data_dir_path(data_dir)
    if project:
        root = normalize_root(project)
        items = [item for item in list_projects(directory) if item["project"] == str(root)]
        if not items:
            raise PocketdexError(
                f"{root} is not indexed yet; call index_project with that path first"
            )
    else:
        items = list_projects(directory)

    return {"data_dir": str(directory), "projects": items}


# --------------------------------------------------------------------------
# MCP server (stdio, newline-delimited JSON-RPC 2.0)
# --------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "index_project",
        "description": (
            "Index or refresh one project folder in its own private database. "
            "Call once before searching an unfamiliar repository, and again "
            "after large changes. Incremental: unchanged files are skipped."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the project folder."},
                "force": {
                    "type": "boolean",
                    "description": "Re-index every file even if mtime and size match.",
                    "default": False,
                },
            },
            "required": ["root"],
        },
    },
    {
        "name": "search",
        "description": (
            "Search one project's index by keyword or identifier. Returns file "
            "paths, line ranges and short highlighted snippets, so use this "
            "before opening whole files. Good for 'where is X', 'who calls Y', "
            "'how is Z implemented'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words or identifiers to find."},
                "project": {
                    "type": "string",
                    "description": "Project root. Defaults to the most recently indexed project.",
                },
                "path_prefix": {
                    "type": "string",
                    "description": "Only search files under this relative path, e.g. 'src/api'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (1-25). Default 8.",
                    "minimum": 1,
                    "maximum": 25,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "stats",
        "description": (
            "Show indexed projects, their separate databases, file and chunk "
            "counts, and index size. Use to check whether a folder is indexed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project root. Defaults to all projects."}
            },
        },
    },
]


def call_tool(data_dir: Path | str | None, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "index_project":
        root = args.get("root")
        if not isinstance(root, str) or not root.strip():
            raise PocketdexError("root must be a non-empty string")
        return index_project(root, bool(args.get("force", False)), data_dir)

    if name == "search":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise PocketdexError("query must be a non-empty string")
        limit = args.get("limit", 8)
        if not isinstance(limit, int):
            raise PocketdexError("limit must be an integer")
        prefix = args.get("path_prefix")
        if prefix is not None and not isinstance(prefix, str):
            raise PocketdexError("path_prefix must be a string")
        project = args.get("project")
        if project is not None and not isinstance(project, str):
            raise PocketdexError("project must be a string")
        return search(project, query, limit=limit, path_prefix=prefix, data_dir=data_dir)

    if name == "stats":
        project = args.get("project")
        if project is not None and not isinstance(project, str):
            raise PocketdexError("project must be a string")
        return stats(project, data_dir)

    raise PocketdexError(f"unknown tool: {name}")


def mcp_response(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def mcp_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_request(
    data_dir: Path | str | None, request: dict[str, Any]
) -> dict[str, Any] | None:
    method = request.get("method")
    req_id = request.get("id")

    if req_id is None:  # notification: never answer
        return None

    if method == "initialize":
        params = request.get("params") or {}
        requested = params.get("protocolVersion")
        supported = {"2024-11-05", "2025-03-26", "2025-06-18"}
        version = requested if requested in supported else "2025-06-18"
        return mcp_response(
            req_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "pocketdex", "version": VERSION},
            },
        )

    if method == "ping":
        return mcp_response(req_id, {})

    if method == "tools/list":
        return mcp_response(req_id, {"tools": TOOLS})

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if not isinstance(name, str) or not isinstance(args, dict):
                raise PocketdexError("tools/call needs a tool name and an object of arguments")
            payload = call_tool(data_dir, name, args)
            return mcp_response(
                req_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
                    ],
                    "isError": False,
                },
            )
        except PocketdexError as exc:
            return mcp_response(
                req_id,
                {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True},
            )
        except Exception as exc:  # never leak a traceback into the protocol
            print(f"pocketdex: tool {name} failed: {exc!r}", file=sys.stderr)
            return mcp_response(
                req_id,
                {"content": [{"type": "text", "text": f"Internal error: {exc}"}], "isError": True},
            )

    return mcp_error(req_id, -32601, f"method not found: {method}")


def serve(data_dir: Path | str | None = None) -> int:
    directory = data_dir_path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    print(f"pocketdex {VERSION} ready (data={directory})", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"pocketdex: bad JSON: {exc}", file=sys.stderr, flush=True)
            continue
        if not isinstance(request, dict):
            continue
        response = handle_request(directory, request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pocketdex", description=__doc__)
    parser.add_argument(
        "--data-dir",
        default=str(default_data_dir()),
        help="where per-project index databases live",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="index or refresh a project folder")
    p_index.add_argument("root")
    p_index.add_argument("--force", action="store_true")

    p_search = sub.add_parser("search", help="search indexed code")
    p_search.add_argument("query")
    p_search.add_argument("--project")
    p_search.add_argument("--path-prefix")
    p_search.add_argument("--limit", type=int, default=8)

    p_stats = sub.add_parser("stats", help="show index statistics")
    p_stats.add_argument("--project")

    sub.add_parser("serve", help="run the MCP stdio server")

    args = parser.parse_args(argv)
    try:
        if args.command == "index":
            print(json.dumps(index_project(args.root, args.force, args.data_dir), indent=2))
        elif args.command == "search":
            print(
                json.dumps(
                    search(
                        args.project,
                        args.query,
                        limit=args.limit,
                        path_prefix=args.path_prefix,
                        data_dir=args.data_dir,
                    ),
                    indent=2,
                    ensure_ascii=False,
                )
            )
        elif args.command == "stats":
            print(json.dumps(stats(args.project, args.data_dir), indent=2))
        elif args.command == "serve":
            return serve(args.data_dir)
    except PocketdexError as exc:
        print(f"pocketdex: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
