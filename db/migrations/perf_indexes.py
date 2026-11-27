"""Apply performance indexes to an existing SQLite database.

Usage::

    python -m db.migrations.perf_indexes --database assets/db.sqlite3
"""

import argparse
import sqlite3
from pathlib import Path

SQL_FILE = Path(__file__).with_name("001_perf_indexes.sql")


def migrate(database_path) -> dict:
    """Add created_at (if missing), apply indexes, and check integrity."""
    path = Path(database_path)
    if not path.exists():
        return {"ok": False, "error": f"database not found: {path}"}

    connection = sqlite3.connect(path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(message_map)")}
        added_column = "created_at" not in columns
        if added_column:
            connection.execute("ALTER TABLE message_map ADD COLUMN created_at DATETIME")
        # Always backfill NULLs: a previous interrupted run may have left them.
        connection.execute(
            "UPDATE message_map SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )

        connection.executescript(SQL_FILE.read_text(encoding="utf-8"))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        connection.commit()
        return {"ok": integrity == "ok", "integrity": integrity, "added_column": added_column}
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="assets/db.sqlite3")
    args = parser.parse_args()
    result = migrate(args.database)
    print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
