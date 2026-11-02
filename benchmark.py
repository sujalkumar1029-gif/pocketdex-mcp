#!/usr/bin/env python3
"""Reproduce pocketdex's token savings on any folder.

    python3 benchmark.py ~/Projects/my-app
    python3 benchmark.py ~/Projects/my-app --queries "main" "config" "error"

Prints index time, database size, and the approximate token cost of each
query result versus the total source size. Token counts are characters / 4.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pocketdex  # noqa: E402


def source_chars(root: Path) -> int:
    """Characters that pocketdex would actually index (skips binaries)."""
    total = 0
    for path, _ in pocketdex.iter_source_files(root):
        text = pocketdex.read_source(path)
        if text is not None:
            total += len(text)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="project folder to index")
    parser.add_argument(
        "--queries",
        nargs="*",
        default=["main", "config", "error"],
        help="queries to measure (default: main config error)",
    )
    parser.add_argument("--data-dir", default=None, help="index location")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    indexed = pocketdex.index_project(root, data_dir=args.data_dir)
    print("index:", json.dumps(indexed, indent=2))

    total = source_chars(root)
    print(f"source: {total:,} chars (~{total // 4:,} tokens)")

    print(f"\n{'query':22} {'hits':>4} {'chars':>7} {'~tokens':>8}")
    for query in args.queries:
        result = pocketdex.search(root, query, limit=6, data_dir=args.data_dir)
        text = json.dumps(result, ensure_ascii=False)
        print(f"{query:22} {result['count']:>4} {len(text):>7} {len(text) // 4:>8}")

    report = pocketdex.stats(root, data_dir=args.data_dir)
    db = Path(report["projects"][0]["db"])
    print(f"\nindex database: {db} ({db.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
