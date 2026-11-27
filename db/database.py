import os
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

POOL_SIZE = 5
MAX_OVERFLOW = 5

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL", "sqlite:///./assets/db.sqlite3"
)

_connect_args = {"timeout": 30}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_timeout=30,
    connect_args=_connect_args,
)


def apply_sqlite_pragmas(target_engine) -> None:
    """Install per-connection pragmas for a SQLite engine."""

    @event.listens_for(target_engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    apply_sqlite_pragmas(engine)

SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(session_factory=SessionMaker):
    """One short transaction per unit of work, safe on any caller thread."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


Base = declarative_base()
