#!/usr/bin/env python3
"""One-time, read-only connection export. Run locally beside Spendy's DB, never in the API.

Exports encrypted access tokens only (not transactions or cursors). The output
is still a secret: transfer it through Dokploy's secret environment UI, not Git.
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path


def export(database: Path, output: Path) -> int:
    if not database.is_file():
        raise ValueError("Source database does not exist")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT item_id, access_token_encrypted, institution_name FROM plaid_items "
            "ORDER BY item_id"
        ).fetchall()
    if not rows:
        raise ValueError("No connected items found")
    # Exclusive create: never overwrite, follow an existing symlink, or emit secrets on stdout.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump([dict(row) for row in rows], handle, separators=(",", ":"))
        handle.write("\n")
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        count = export(args.db, args.out)
    except (ValueError, OSError, sqlite3.Error):
        parser.exit(1, "Export failed. Check source DB/schema and use a new output file.\n")
    print(f"Exported {count} encrypted connections to a mode-0600 file. Treat it as a secret.")


if __name__ == "__main__":
    main()
