import importlib
import inspect
import pickle
import sqlite3

import db.database as database
import db.repository as repository
from db.schema import SCHEMA_SQL

user_state_migration = importlib.import_module("db.migrations.user_state")
import_pickle_state = importlib.import_module("scripts.import_pickle_state")
main_mod = importlib.import_module("interactive-bot.__main__")


def test_user_state_migration_creates_table_idempotently(tmp_path):
    path = tmp_path / "state.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, user_id INTEGER)")
    connection.close()

    first = user_state_migration.migrate(path)
    assert first["ok"] is True
    assert first["created_table"] is True

    second = user_state_migration.migrate(path)
    assert second["ok"] is True
    assert second["created_table"] is False


def test_pickle_import_writes_user_state_only(tmp_path):
    database_path = tmp_path / "import.sqlite"
    connection = sqlite3.connect(database_path)
    connection.executescript(SCHEMA_SQL)
    connection.close()
    pickle_path = tmp_path / "legacy.pickle"
    pickle_path.write_bytes(
        pickle.dumps(
            {
                "user_data": {
                    123: {
                        "is_human": True,
                        "vcode": "abc",
                        "is_human_error_time": 42.5,
                    }
                }
            }
        )
    )

    code = import_pickle_state.main(
        ["--pickle", str(pickle_path), "--database", str(database_path)]
    )
    assert code == 0

    connection = sqlite3.connect(database_path)
    try:
        state = connection.execute(
            "SELECT is_human, vcode, is_human_error_time FROM user_state WHERE user_id=123"
        ).fetchone()
        assert state == (1, "abc", 42.5)
        assert connection.execute(
            "SELECT COUNT(*) FROM user WHERE user_id=123"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_captcha_state_survives_reconnect():
    database.init_db()
    repository.set_user_vcode(42, "abc")
    database.close()
    database.init_db()
    assert repository.get_user_state(42).vcode == "abc"


def test_pickle_persistence_is_gone():
    source = inspect.getsource(main_mod)
    assert "PicklePersistence" not in source
    assert not hasattr(main_mod, "persistence_interval")


def test_pickle_import_rejects_missing_database(tmp_path):
    pickle_path = tmp_path / "legacy.pickle"
    pickle_path.write_bytes(pickle.dumps({"user_data": {123: {"is_human": True}}}))
    missing_db = tmp_path / "missing.sqlite"
    assert import_pickle_state.main(
        ["--pickle", str(pickle_path), "--database", str(missing_db)]
    ) == 2
    assert not missing_db.exists()


def test_pickle_import_does_not_overwrite_existing_user_state(tmp_path):
    database_path = tmp_path / "import.sqlite"
    connection = sqlite3.connect(database_path)
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO user_state (user_id, is_human, vcode, is_human_error_time) "
        "VALUES (123, 1, 'new-code', NULL)"
    )
    connection.commit()
    connection.close()
    pickle_path = tmp_path / "legacy.pickle"
    pickle_path.write_bytes(
        pickle.dumps({"user_data": {123: {"is_human": False, "vcode": "old-code"}}})
    )

    assert import_pickle_state.main(
        ["--pickle", str(pickle_path), "--database", str(database_path)]
    ) == 0
    connection = sqlite3.connect(database_path)
    try:
        state = connection.execute(
            "SELECT is_human, vcode FROM user_state WHERE user_id=123"
        ).fetchone()
        assert state == (1, "new-code")
    finally:
        connection.close()
