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
