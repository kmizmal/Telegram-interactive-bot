"""One-time import of captcha state from the legacy PicklePersistence file.

This reads only ``user_data`` and writes only ``user_state``. It never creates
or modifies rows in the ``user`` business table, so users that exist only in
the old pickle do not enter broadcast or user listings.

Usage::

    python scripts/import_pickle_state.py \
        --pickle assets/<APP_NAME>.pickle --database assets/db.sqlite3
"""

from __future__ import annotations

import argparse
import pickle
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.migrations.user_state import USER_STATE_DDL  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pickle", required=True)
    parser.add_argument("--database", default="assets/db.sqlite3")
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args(argv)

    pickle_path = Path(args.pickle)
    if not pickle_path.exists():
        if args.allow_missing:
            print(f"pickle not found, skipped: {pickle_path}")
            return 0
        print(f"pickle not found: {pickle_path}")
        return 2

    try:
        data = pickle.loads(pickle_path.read_bytes())
    except Exception as error:
        print(f"failed to read pickle {pickle_path}: {error}")
        return 1

    user_data = data.get("user_data", {}) if isinstance(data, dict) else {}
    if not isinstance(user_data, dict):
        print("pickle user_data is not a mapping")
        return 1

    connection = sqlite3.connect(args.database)
    imported = 0
    try:
        connection.execute(USER_STATE_DDL)
        for raw_user_id, state in user_data.items():
            if not isinstance(state, dict):
                continue
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                continue
            is_human = 1 if state.get("is_human") else 0
            vcode = state.get("vcode")
            error_time = state.get("is_human_error_time")
            if not is_human and vcode is None and error_time is None:
                continue
            connection.execute(
                """
                INSERT INTO user_state
                    (user_id, is_human, vcode, is_human_error_time)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    is_human=excluded.is_human,
                    vcode=excluded.vcode,
                    is_human_error_time=excluded.is_human_error_time
                """,
                (user_id, is_human, vcode, error_time),
            )
            imported += 1
        connection.commit()
    finally:
        connection.close()

    print(f"imported {imported} user_state rows from {pickle_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
