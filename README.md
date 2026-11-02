# PocketDex MCP

[![tests](https://github.com/sujalkumar1029-gif/pocketdex-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/sujalkumar1029-gif/pocketdex-mcp/actions/workflows/test.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

A tiny local code index for AI agents, served over MCP. One Python file, no
dependencies, no background server, no vectors — just SQLite FTS5.

Ask an agent *"where are badge rules calculated?"* and it gets a file path, a
line range, and a short highlighted snippet instead of opening five files.

## Why

An agent exploring a repo opens file after file, and every file costs context.
PocketDex indexes the folder once, then answers `search` with the passage the
agent needs.

Measured on a 34-file Flask app:

| Query | Result size | `app.py` alone |
|---|---|---|
| `parse_amount_cents` | ~120 tokens | ~4,442 tokens |
| `badge rules` | ~69 tokens | ~4,442 tokens |
| `sqlite connection` | ~285 tokens | ~4,442 tokens |

Index time: 0.06 s. Index size: ~0.5 MB. Query latency: 8–29 ms.
(Token counts are `characters / 4`, an approximation.)

## Why it is different

| | PocketDex MCP | Shared code index | `grep` / `rg` |
|---|---|---|---|
| Dependencies | None (stdlib only) | Service + client | None |
| Setup | One file, no install | Install + run server | None |
| Storage | **One DB per project** | Often one shared index | None |
| Results | Ranked passages + lines | Ranked passages | Raw matching lines |
| Context cost | Low | Low | Low, but no ranking |
| Best at | 20–500 files | Large repos / corpora | One known symbol |

The important design choice is **isolation**. Each project gets its own
database inside PocketDex's folder:

```
PocketDex/
├── pocketdex.py
├── data/projects/
│   ├── my-app-3f2a1b9c.db
│   └── other-api-7d1e0c44.db
├── tests/
└── ARCHITECTURE.md
```

- Nothing is written into the project being indexed.
- Two projects never share a database, so results cannot leak across them.
- Deleting a `.db` file removes that project's index completely.
- There is no central registry that can drift out of sync.

Override the location with `--data-dir` or `POCKETDEX_DATA`. Run from a clone,
data stays in `data/projects/` (gitignored). Installed as a package, it falls
back to `~/.local/share/pocketdex/projects/` because site-packages is not
writable.

`rg` is still the right tool for one known symbol. PocketDex is for exploring
an unfamiliar repo without paying for whole files.

## Install

There is nothing to install. Clone the repo and run it:

```bash
git clone https://github.com/sujalkumar1029-gif/pocketdex-mcp.git
cd pocketdex-mcp
python3 pocketdex.py --help
```

Requirements: Python 3.10+ and SQLite built with FTS5 (standard in CPython
builds).

## Quick start

```bash
# index or refresh a project (incremental)
python3 pocketdex.py index ~/Projects/my-app

# search
python3 pocketdex.py search "parse_amount_cents"
python3 pocketdex.py search "badge rules" --limit 5
python3 pocketdex.py search "connect" --path-prefix src/db

# inspect every project's separate index
python3 pocketdex.py stats

# MCP server (what the agent runs)
python3 pocketdex.py serve
```

`search` without `--project` uses the most recently indexed project. Pass
`--project ~/Projects/other` when several are indexed.

## MCP configuration

Add this to your MCP client's config (for example
`~/.gemini/config/mcp_config.json` for Antigravity):

```json
{
  "mcpServers": {
    "pocketdex": {
      "command": "python3",
      "args": ["/absolute/path/to/pocketdex.py", "serve"]
    }
  }
}
```

Tools exposed:

| Tool | Purpose |
|---|---|
| `index_project` | Index or refresh one folder into its own DB. Incremental. |
| `search` | Keyword/identifier search in one project, with snippets. |
| `stats` | Every indexed project, its DB path, file/chunk counts, size. |

## Reproduce the numbers

```bash
python3 benchmark.py ~/Projects/my-app
```

It indexes the folder, prints index time and database size, then measures the
approximate token cost of a few queries against the total source size.

## How it works

- Walks the folder, skipping `.git`, `node_modules`, `.venv`, `dist`, build
  output, lockfiles, hidden folders, files over 512 KB, and binaries.
- Splits files into 40-line chunks with 12 lines of overlap.
- Stores chunks in SQLite FTS5 with `_` as a token character, so
  `parse_amount_cents` stays one searchable token.
- BM25 ranking, at most two results per file, snippets capped at 12 tokens of
  context.
- Re-indexing compares `mtime_ns` and size; unchanged files are skipped.

`ARCHITECTURE.md` explains the data model, indexing pipeline, ranking, MCP
lifecycle, failure modes, and extension points.

## Limits (on purpose)

- **No embeddings / vector search.** Keyword + prefix matching only. Code
  search is dominated by exact identifiers.
- **No regex tool.** Add one only if a real need appears.
- **Small-to-medium projects.** A 20–500 file range is the target; a multi-GB
  monorepo belongs on a dedicated search service.
- **No file watching.** Call `index_project` after large edits; unchanged
  files are cheap to skip.
- **Plaintext index.** Treat the data folder with the same care as the source
  it indexes.

## Tests

```bash
python3 -m pip install pytest
python3 -m pytest -q
```

11 tests: indexing, ignored dirs, binary skip, incremental re-index, deletion,
path filters, stats, per-project isolation, latest-project selection, and a
real MCP stdio handshake (initialize → tools/list → tools/call).

## Status

Early (0.x). The MCP contract and CLI are stable; internals may change.
Issues and pull requests are welcome.

## License

MIT — see [LICENSE](LICENSE).
