import gc
import importlib
import inspect
import os
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
import db.repository as repository

memory_cache = importlib.import_module("interactive-bot.services.memory_cache")
processor = importlib.import_module("interactive-bot.services.update_processor")
main_mod = importlib.import_module("interactive-bot.__main__")
probe = importlib.import_module("scripts.perf_probe")


def test_ttl_cache_evicts_expired_and_oldest():
    cache = memory_cache.TTLCache(maxsize=2, ttl_seconds=10)
    cache.set("a", 1, now=0)
    cache.set("b", 2, now=1)
    assert cache.get("b", now=2) == 2
    cache.set("c", 3, now=3)
    assert cache.get("a", now=3) is None
    assert cache.get("b", now=12) is None


def test_rate_limit_resets_after_interval(monkeypatch):
    memory_cache.clear_all()
    monkeypatch.setattr(memory_cache.time, "monotonic", lambda: 100.0)
    assert memory_cache.rate_limit_ok(7, 5) is True
    assert memory_cache.rate_limit_ok(7, 5) is False
    monkeypatch.setattr(memory_cache.time, "monotonic", lambda: 106.0)
    assert memory_cache.rate_limit_ok(7, 5) is True


def test_user_locks_are_weak_and_evicted():
    locks = [processor.user_lock(None, user_id) for user_id in range(2000)]
    assert processor.lock_count() == 2000
    del locks
    gc.collect()
    assert processor.lock_count() < 100


def test_media_group_first_flag():
    database.init_db()
    group_id = 987654321
    assert repository.add_media_group_message(1, 1001, group_id, None) is True
    assert repository.add_media_group_message(1, 1002, group_id, None) is False
    repository.delete_media_group_messages(group_id, 1)
    assert repository.add_media_group_message(1, 1003, group_id, None) is True


@pytest.mark.asyncio
async def test_broadcast_fetches_users_in_pages(monkeypatch):
    pages = []

    def fake_page(limit, after_user_id):
        pages.append((limit, after_user_id))
        return [SimpleNamespace(user_id=1)] if after_user_id == 0 else []

    monkeypatch.setattr(main_mod.repository, "get_users_page_after", fake_page)
    context = probe.make_context(probe._configured_bot())
    context.job = SimpleNamespace(data="555_-1001")

    await main_mod._broadcast(context)

    assert pages == [(500, 0), (500, 1)]


def test_pyproject_does_not_depend_on_sqlalchemy_or_pytz():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    dependencies = [item.lower() for item in data["project"]["dependencies"]]
    assert not any("sqlalchemy" in item for item in dependencies)
    assert not any("pytz" in item for item in dependencies)


def test_runtime_does_not_import_sqlalchemy():
    importlib.import_module("interactive-bot.__main__")
    assert "sqlalchemy" not in sys.modules


def test_utils_source_uses_stdlib_timezone():
    utils = importlib.import_module("interactive-bot.utils")

    source = inspect.getsource(utils)
    assert "pytz" not in source
    assert "timezone.utc" in source


def test_init_db_upgrades_old_message_map_schema(tmp_path):
    database_path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE message_map ("
        "id INTEGER PRIMARY KEY, user_chat_message_id INTEGER, "
        "group_chat_message_id INTEGER, user_id INTEGER)"
    )
    connection.commit()
    connection.close()

    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{database_path}"
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-c", "import db.database as d; d.init_db(); d.close()"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    connection = sqlite3.connect(database_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(message_map)")}
        assert "created_at" in columns
    finally:
        connection.close()
