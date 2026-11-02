# pocketdex — architecture

This document explains how pocketdex works, why each choice was made, and
where to extend it. It describes version 0.2.0.

## The problem

A coding agent answering "where are badge rules calculated?" has two options:

1. Open candidate files until it finds the answer.
2. Query an index that returns the passage.

Option 1 is simple but expensive: a 500-line file costs roughly 4,000–5,000
tokens, and an agent may open five of them before finding the right one.
Option 2 costs a few hundred tokens, but needs an index that stays current.

pocketdex is the smallest useful version of option 2:

- one Python file, standard library only;
- SQLite FTS5, which ships with Python;
- one database per project, so projects never mix;
- MCP over stdio, so Antigravity can call it like any other server.

It deliberately does **not** do embeddings, a web UI, file watching, or
multi-user access. Those are the parts that make xerj a service. For 20–500
files, none of them are needed.

## Process model

pocketdex has two modes:

```
CLI:   python3 pocketdex.py index|search|stats ...
MCP:   python3 pocketdex.py serve
```

In MCP mode it is a child process of the agent client. It reads one JSON-RPC
message per line on stdin and writes one response per line on stdout. It logs
to stderr only, because anything on stdout that is not a protocol message
breaks the client. There is no port, no daemon, and no state in memory between
requests — the databases are the state.

The server is started once per agent session and exits when stdin closes.

## Isolation model

Every absolute project path maps to exactly one database file:

```
db = <data-dir>/<slug>-<sha1(abs_path)[:8]>.db
```

- `slug` is the folder name, reduced to `[A-Za-z0-9._-]`, max 40 characters.
- The hash suffix prevents collisions between two folders with the same name.
- The default data dir is `<pocketdex>/data/projects/`, not the project folder
  and not a global cache.
- `search` and `stats` accept a project path; the server derives the database
  path, opens it read-write, and closes it after the request.
- `stats` discovers projects by scanning `*.db` in the data dir and reading
  each database's `projects` table. There is no central registry that can drift
  out of sync.
- Deleting a database file is a complete, safe way to remove a project's index.

This is why a project's content cannot leak into another project's results:
they are different SQLite files.

## Data model

Each database contains four tables and one FTS5 virtual table:

```
projects  1 row per database: id, root, indexed_at
   │
   └── files   one row per indexed file: path (relative), mtime_ns, size
          │
          └── chunks   one row per 40-line window: path, start_line,
                       end_line, content
                         │
                         └── chunks_fts   FTS5 index over content + path
```

`chunks_fts` is an **external-content** FTS5 table (`content='chunks'`). It
stores only the inverted index, not a second copy of the text. Three triggers
(`chunks_ai`, `chunks_ad`, `chunks_au`) keep the FTS index in sync on insert,
delete, and update. The application deletes old chunks before inserting new
ones, so the delete trigger always has the old row to work with.

Why line windows instead of AST chunks?

- AST parsing would require a parser per language. That is a large dependency
  and a large failure surface.
- Fixed windows are language-agnostic and predictable.
- 40 lines with 12 lines of overlap means a function near a boundary appears
  whole in at least one chunk. The cost is ~1.4× duplicated text, which is
  cheap at this scale.

The overlap is the reason `indexed_chars` is larger than the sum of the source
files. It is not a bug.

## Indexing pipeline

`index_project(root, force=False, data_dir=...)`:

1. **Normalize** the root (`expanduser().resolve()`) and derive the database
   path.
2. **Open** the database, enable WAL and foreign keys, create the schema if
   needed, and check `meta.schema_version`.
3. **Begin** an immediate transaction. One transaction per indexing run means
   a crash cannot leave half a file indexed.
4. **Load** the existing `(path, mtime_ns, size)` rows for this project.
5. **Walk** the tree with `os.walk`:
   - skip ignored directory names (`node_modules`, `.venv`, `dist`, `target`,
     `__pycache__`, …);
   - skip any directory starting with `.`;
   - skip lockfiles and `.min.js`;
   - skip files of 0 bytes or over 512 KB.
6. **Compare** each file's `mtime_ns` and size with the stored row. If both
   match and `force` is false, count it as unchanged and move on. This is why
   a second index run on a 34-file project takes a few milliseconds.
7. **Read** changed files:
   - reject anything with a NUL byte in the first 4 KB (binary);
   - decode as UTF-8 with replacement, so one bad byte does not drop a file;
   - reject minified content (≤3 lines, one line >1000 chars).
8. **Replace** the file's chunks: delete old chunks (trigger removes old FTS
   rows), delete the old `files` row, insert the new row, insert new chunks
   (trigger adds new FTS rows).
9. **Remove** files that disappeared: any stored path not seen during the walk
   is deleted; the chunk cascade and trigger clean up FTS.
10. **Commit** and update `projects.indexed_at`.

The result reports `files_indexed`, `files_unchanged`, `files_skipped`
(binary/minified), `files_removed`, `chunks`, and `seconds`.

## Tokenization and ranking

FTS5 is created with:

```sql
tokenize="unicode61 remove_diacritics 2 tokenchars '_'"
prefix='2 3'
```

- `unicode61` handles Unicode letters and digits.
- `tokenchars '_'` keeps `parse_amount_cents` as one token instead of three.
  Without it, searching for an identifier would match unrelated fragments.
- `prefix='2 3'` makes short prefix queries fast. The query builder adds `*`
  to every token, so `parse_amount` matches `parse_amount_cents`.
- The `path` column is indexed too, but ranking gives content the higher
  weight.

Queries are built from `[A-Za-z_][A-Za-z0-9_]*|\d+` tokens:

```
"parse_amount"* AND "cents"*
```

Each token is quoted to neutralise FTS5 operators (`AND`, `OR`, `NEAR`, `-`,
`:`), so a query cannot accidentally become a different query or a syntax
error. At most eight tokens are used.

Ranking uses SQLite's `bm25()` with weights `(1.0, 0.4)` — content matters more
than path. Lower (more negative) is better; the API reports the sign-flipped
value so higher is better.

Snippets come from `snippet(chunks_fts, 0, '**', '**', '…', 12)`: the matching
passage, highlights wrapped in `**`, at most 12 tokens of context.

Two results per file maximum keeps one large file from filling the result list.
If the first query returns nothing, a fallback splits identifiers on `_` and
camelCase boundaries and searches for any part with `OR`. That catches
`parseAmount` → `parse_amount` without weakening normal queries.

## Search path

`search(root, query, limit, path_prefix, data_dir)`:

1. Resolve the project: explicit root, or the most recently indexed database
   (`latest_root` reads `indexed_at` from each DB).
2. Open that project's database. If it does not exist, raise a clear "not
   indexed yet" error instead of returning empty results.
3. Build the FTS match expression; an empty/punctuation-only query returns
   zero results without touching the index.
4. Run the ranked query, optionally filtered by `path_prefix` via `LIKE`.
5. Deduplicate to at most two chunks per file and stop at `limit`.
6. Return `{query, project, db, count, results[]}`. Each result is
   `{path, lines, score, snippet}`.

Typical latency on this machine is 8–29 ms for a 34-file project. The dominant
cost is opening the database per MCP call, not the search itself.

## MCP protocol

The server implements the subset an agent needs:

| Method | Response |
|---|---|
| `initialize` | protocol version, `tools` capability, server name/version |
| `notifications/initialized` | no response (notifications never get one) |
| `ping` | `{}` |
| `tools/list` | the three tool schemas |
| `tools/call` | `{content:[{type:"text",text:"<json>"}], isError}` |

Rules that matter for compatibility:

- **stdout is protocol only.** Logs go to stderr.
- **Notifications have no `id`,** so they must not be answered.
- **Unknown methods** get JSON-RPC `-32601`.
- **Tool failures** are a successful JSON-RPC response with `isError: true`
  and a human-readable message, not a protocol error. This lets the agent read
  the reason and correct itself.
- **Unexpected exceptions** are caught and reported as `isError`, so a bug in
  one tool cannot kill the session.
- **No traceback** is ever written into the protocol stream.

The protocol version is echoed if the client requests one of the supported
versions (`2024-11-05`, `2025-03-26`, `2025-06-18`); otherwise the server
answers `2025-06-18`.

## Tool contracts

### `index_project`

```json
{"root": "/abs/path", "force": false}
```

Returns the indexing counters plus `project` and `db`. `force` re-reads every
file; normal use leaves it false.

### `search`

```json
{"query": "badge rules", "project": "/abs/path", "path_prefix": "src", "limit": 8}
```

Only `query` is required. `project` defaults to the most recently indexed
project. `limit` is clamped to 1–25. `path_prefix` is a relative prefix, not a
glob.

### `stats`

```json
{"project": "/abs/path"}
```

With no arguments, lists every project. Each entry includes its separate `db`
path and `db_bytes`, which makes accidental mixing visible.

## Performance envelope

| Operation | Observed |
|---|---|
| Index 34 files (cold) | 0.06 s |
| Re-index 34 files (warm) | ~0.01 s |
| Search, 34-file project | 8–29 ms |
| Index size, 34 files | ~0.5 MB |
| Process memory | small; no cache or model |

The design target is hundreds of files, not millions. If a project exceeds
roughly 50 MB of text, expect slower indexing and a larger database; at that
point a real search service is the right tool.

## Failure modes and what happens

| Failure | Behaviour |
|---|---|
| Project not indexed | `PocketdexError` with "call index_project first" |
| No projects at all | "no projects indexed yet" |
| Path is not a directory | "not a directory" |
| Binary / minified file | counted in `files_skipped`, not indexed |
| File deleted | removed on the next index run |
| Schema version mismatch | explicit error; delete that DB and re-index |
| Corrupt database | `sqlite3.Error` during `stats` scanning is skipped |
| Agent sends bad arguments | `isError: true` with the reason |

## Why no vectors

Embeddings would add a model, a download, an API key or a local runtime, and
latency. Code search is dominated by exact identifiers, which BM25 handles
well. Semantic search helps with "find the concept of…", but an agent can
usually turn that into identifiers after one keyword search. Keeping the tool
dependency-free is worth more than fuzzy recall at this scale.

If semantic search is ever needed, the extension point is a second table
(`chunk_vectors`) and a hybrid ranker; the MCP contract does not need to
change.

## Extension points

- **Regex tool:** add a `search_regex` tool that runs a compiled pattern over
  `chunks.content` and returns line ranges. Keep it separate from the FTS
  path.
- **Symbol index:** parse `def`/`class`/`function` lines into a `symbols` table
  for exact "go to definition" queries.
- **Watch mode:** a `watch` subcommand using `inotify`/`watchdog` that calls
  `index_project` on change. This adds a daemon, so it should stay opt-in.
- **Gitignore support:** currently a fixed ignore list; reading `.gitignore`
  would reduce noise in projects with generated directories.
- **Project-local config:** a `.pocketdex.json` in a project could set extra
  ignores or chunk size. Do not put the database there — keep data in
  pocketdex's folder.

## Security notes

- pocketdex reads files the user can read; it does not need elevated
  permissions.
- It never writes into the indexed project.
- Database files are per-project, so one project's code is not readable
  through another project's search.
- No network access, no telemetry, no external service.
- The index is plaintext SQLite. Treat the data folder with the same care as
  the source it indexes; do not point it at a secrets directory.
