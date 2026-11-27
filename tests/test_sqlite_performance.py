import importlib
import sqlite3

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

database = importlib.import_module("db.database")
model = importlib.import_module("db.model")
migration = importlib.import_module("db.migrations.perf_indexes")

EXPECTED_INDEXES = {
    "message_map": {
        "ix_message_map_user_chat",
        "ix_message_map_group_chat",
        "ix_message_map_user",
    },
    "formn_status": {"ix_formn_status_thread"},
    "media_group_message": {"ix_media_group_lookup"},
    "user": {"ix_user_thread"},
}


def test_model_declares_hot_indexes():
    tables = model.Base.metadata.tables
    for table_name, expected in EXPECTED_INDEXES.items():
        actual = {index.name for index in tables[table_name].indexes}
        assert expected <= actual, table_name


def test_primary_keys_have_no_redundant_index():
    tables = model.Base.metadata.tables
    for table_name in EXPECTED_INDEXES:
        actual = {index.name for index in tables[table_name].indexes}
        assert f"ix_{table_name}_id" not in actual


def test_message_map_created_at_is_nullable():
    column = model.Base.metadata.tables["message_map"].columns["created_at"]
    assert column.nullable is True


def test_engine_pool_is_bounded():
    assert database.POOL_SIZE == 5
    assert database.MAX_OVERFLOW == 5
    assert database.engine.pool._max_overflow == database.MAX_OVERFLOW


def test_sqlite_pragmas_are_applied(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pragmas.sqlite'}")
    database.apply_sqlite_pragmas(engine)

    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA synchronous")).scalar() == 1
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 5000


def test_session_scope_commits(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scope.sqlite'}")
    model.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    with database.session_scope(factory) as session:
        session.add(model.User(user_id=42, first_name="A"))

    reader = factory()
    try:
        assert reader.query(model.User).filter_by(user_id=42).one() is not None
    finally:
        reader.close()


def test_session_scope_rolls_back(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback.sqlite'}")
    model.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    try:
        with database.session_scope(factory) as session:
            session.add(model.User(user_id=43, first_name="B"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    reader = factory()
    try:
        assert reader.query(model.User).filter_by(user_id=43).count() == 0
    finally:
        reader.close()


LEGACY_SCHEMA = """
CREATE TABLE user (
    id INTEGER NOT NULL, user_id INTEGER, first_name VARCHAR(64),
    last_name VARCHAR(64), username VARCHAR(64), is_premium BOOLEAN,
    updated_at DATETIME, message_thread_id INTEGER, PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_user_user_id ON user (user_id);
CREATE INDEX ix_user_id ON user (id);
CREATE TABLE message_map (
    id INTEGER NOT NULL, user_chat_message_id INTEGER,
    group_chat_message_id INTEGER, user_id INTEGER, PRIMARY KEY (id)
);
CREATE INDEX ix_message_map_id ON message_map (id);
CREATE TABLE formn_status (
    id INTEGER NOT NULL, chat_id INTEGER, message_thread_id INTEGER,
    status VARCHAR(64), PRIMARY KEY (id)
);
CREATE INDEX ix_formn_status_id ON formn_status (id);
CREATE TABLE media_group_message (
    id INTEGER NOT NULL, chat_id INTEGER, message_id INTEGER,
    media_group_id INTEGER, is_header BOOLEAN, caption_html VARCHAR(65536),
    PRIMARY KEY (id)
);
CREATE INDEX ix_media_group_message_id ON media_group_message (id);
INSERT INTO message_map (user_chat_message_id, group_chat_message_id, user_id)
VALUES (11, 22, 33), (44, 55, 66);
"""


def test_migration_is_idempotent_and_preserves_rows(tmp_path):
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.close()

    first = migration.migrate(path)
    assert first["ok"] is True
    assert first["added_column"] is True

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM message_map").fetchone()[0] == 2
        columns = {row[1] for row in connection.execute("PRAGMA table_info(message_map)")}
        assert "created_at" in columns
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(message_map)")}
        assert EXPECTED_INDEXES["message_map"] <= indexes
        user_indexes = {row[1] for row in connection.execute("PRAGMA index_list(user)")}
        assert "ix_user_id" not in user_indexes
    finally:
        connection.close()

    second = migration.migrate(path)
    assert second["ok"] is True
    assert second["added_column"] is False
