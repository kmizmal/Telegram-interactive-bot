import importlib
import sqlite3

database = importlib.import_module("db.database")
repository = importlib.import_module("db.repository")
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


def test_init_db_creates_expected_tables():
    database.init_db()
    names = {
        row[0]
        for row in database.connect().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "user",
        "message_map",
        "formn_status",
        "media_group_message",
        "user_state",
    } <= names


def test_sqlite_pragmas_are_applied():
    connection = database.connect()
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert connection.execute("PRAGMA cache_size").fetchone()[0] == -512
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_transaction_commits():
    database.init_db()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO user (user_id, first_name) VALUES (?, ?)", (998, "commit")
        )
    assert database.connect().execute(
        "SELECT COUNT(*) FROM user WHERE user_id=?", (998,)
    ).fetchone()[0] == 1


def test_transaction_rolls_back():
    database.init_db()
    try:
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO user (user_id, first_name) VALUES (?, ?)",
                (999, "rollback"),
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert database.connect().execute(
        "SELECT COUNT(*) FROM user WHERE user_id=?", (999,)
    ).fetchone()[0] == 0


def test_repository_roundtrip():
    database.init_db()
    user = repository.get_or_create_user(1001, "Round", "Trip")
    assert user.user_id == 1001
    repository.set_user_thread(1001, 77)
    repository.create_message_map(1001, 2001, 1001)

    assert repository.find_message_map_by_user_chat(1001, 1001) == 2001
    assert repository.get_user_by_user_id(1001).message_thread_id == 77


def test_repository_batch_insert_commits_once():
    database.init_db()
    statements = []
    database.set_trace_callback(statements.append)
    try:
        repository.create_message_maps_batch(
            [(3001, 4001, 15), (3002, 4002, 15), (3003, 4003, 15)]
        )
    finally:
        database.set_trace_callback(None)

    assert sum(1 for statement in statements if statement.strip().upper() == "COMMIT") == 1


def test_message_map_created_at_default():
    database.init_db()
    repository.create_message_map(1002, 2002, 1002)
    row = database.connect().execute(
        "SELECT created_at FROM message_map WHERE user_chat_message_id=1002"
    ).fetchone()
    assert row is not None
    assert row[0] is not None


def test_user_state_repository_roundtrip():
    database.init_db()
    assert repository.get_user_state(2001).is_human is False
    repository.set_user_vcode(2001, "abc")
    assert repository.get_user_state(2001).vcode == "abc"
    repository.mark_user_human(2001)
    assert repository.get_user_state(2001).is_human is True
    repository.set_user_captcha_error(2001, 123.5)
    assert repository.get_user_state(2001).is_human_error_time == 123.5


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
