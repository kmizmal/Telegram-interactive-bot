"""Create the user_state table on an existing database.

Usage::

    python -m db.migrations.user_state --database assets/db.sqlite3
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

USER_STATE_DDL = """
CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY,
    is_human INTEGER NOT NULL DEFAULT 0,
    vcode TEXT,
    is_human_error_time REAL
)
"""


def migrate(database_path) -> dict:
    """Create user_state if missing and verify database integrity."""
    path = Path(database_path)
    if not path.exists():
        return {"ok": False, "error": f"database not found: {path}"}

    connection = sqlite3.connect(path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_state'"
        ).fetchone()
        created_table = exists is None
        connection.execute(USER_STATE_DDL)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        connection.commit()
        return {
            "ok": integrity == "ok",
            "integrity": integrity,
            "created_table": created_table,
        }
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
