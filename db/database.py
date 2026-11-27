"""Process-wide SQLite connection management.

The bot runs a single long-lived systemd process and has low database volume.
One connection guarded by a re-entrant lock keeps memory predictable while
allowing the existing ``ThreadPoolExecutor`` repository calls to stay
off the asyncio event loop.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from db.schema import SCHEMA_SQL

_LOCK = threading.RLock()
_TX_DEPTH = threading.local()
_CONNECTION: sqlite3.Connection | None = None
_TRACE_CALLBACK: Callable[[str], None] | None = None


def database_path() -> Path:
    """Return the SQLite path encoded by DATABASE_URL."""
    url = os.getenv("DATABASE_URL", "sqlite:///./assets/db.sqlite3")
    if not url.startswith("sqlite"):
        raise ValueError(f"only sqlite DATABASE_URL is supported: {url!r}")
    if url.endswith(":memory:"):
        return Path(":memory:")
    if not url.startswith("sqlite:///"):
        raise ValueError(f"unsupported sqlite URL: {url!r}")
    return Path(url[len("sqlite:///") :])


def _open_connection() -> sqlite3.Connection:
    path = database_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(path),
        timeout=30,
        check_same_thread=False,
        isolation_level="",
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA cache_size=-512")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA foreign_keys=ON")
    if _TRACE_CALLBACK is not None:
        connection.set_trace_callback(_TRACE_CALLBACK)
    return connection


def connect() -> sqlite3.Connection:
    """Return the process-wide connection, opening it lazily."""
    global _CONNECTION
    with _LOCK:
        if _CONNECTION is None:
            _CONNECTION = _open_connection()
        return _CONNECTION


def set_trace_callback(callback: Callable[[str], None] | None) -> None:
    """Install a SQL trace callback on the current and future connections.

    This exists for tests that need to count statements without an ORM event
    system; production code never sets a callback.
    """
    global _TRACE_CALLBACK
    with _LOCK:
        _TRACE_CALLBACK = callback
        if _CONNECTION is not None:
            _CONNECTION.set_trace_callback(callback)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Run a short write transaction under the process lock.

    Nested transactions are rejected: SQLite has no nested transaction here,
    and committing an inner scope would silently commit the outer one.
    """
    with _LOCK:
        if getattr(_TX_DEPTH, "depth", 0):
            raise RuntimeError("nested transaction is not supported")
        connection = connect()
        _TX_DEPTH.depth = 1
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            _TX_DEPTH.depth = 0


@contextmanager
def read_connection() -> Iterator[sqlite3.Connection]:
    """Yield the shared connection for a read-only query."""
    with _LOCK:
        yield connect()


def _upgrade_existing_schema(connection: sqlite3.Connection) -> None:
    """Apply additive fixes that CREATE TABLE IF NOT EXISTS cannot make."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(message_map)")}
    if "created_at" not in columns:
        connection.execute("ALTER TABLE message_map ADD COLUMN created_at DATETIME")
    connection.execute(
        "UPDATE message_map SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL"
    )


def init_db() -> None:
    """Create all tables/indexes and upgrade additive columns on existing DBs."""
    with _LOCK:
        connection = connect()
        connection.executescript(SCHEMA_SQL)
        _upgrade_existing_schema(connection)
        connection.commit()


def close() -> None:
    """Close and forget the process-wide connection (tests/shutdown)."""
    global _CONNECTION
    with _LOCK:
        if _CONNECTION is not None:
            _CONNECTION.close()
            _CONNECTION = None


if __name__ == "__main__":
    init_db()
    print("database initialized")
