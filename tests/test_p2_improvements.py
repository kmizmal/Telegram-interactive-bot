import importlib
import logging
from datetime import datetime, timedelta

import db.database as database

main_mod = importlib.import_module("interactive-bot.__main__")
captcha = importlib.import_module("interactive-bot.services.captcha")
cleanup = importlib.import_module("interactive-bot.services.cleanup")


def test_user_handlers_are_reexported_from_entry_point():
    user_handlers = importlib.import_module("interactive-bot.handlers.user")
    assert main_mod.start is user_handlers.start
    assert main_mod.check_human is user_handlers.check_human
    assert main_mod.callback_query_vcode is user_handlers.callback_query_vcode


def test_logging_uses_rotating_file_handler():
    handlers = [
        handler
        for handler in logging.getLogger("interactive-bot").handlers
        if isinstance(handler, logging.handlers.RotatingFileHandler)
    ]
    assert handlers
    assert handlers[0].maxBytes == 10 * 1024 * 1024
    assert handlers[0].backupCount == 5


def test_allowed_updates_limits_polling():
    assert main_mod.allowed_updates() == ["message", "callback_query"]


def test_http_settings_have_bounded_defaults(monkeypatch):
    monkeypatch.delenv("HTTP_POOL_SIZE", raising=False)
    settings = main_mod.http_settings()
    assert settings["connection_pool_size"] == 8
    assert settings["connect_timeout"] == 5
    assert settings["read_timeout"] == 10
    assert settings["write_timeout"] == 20


def test_captcha_image_names_are_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(
        captcha.os, "listdir", lambda path: calls.append(path) or ["image_abc.png"]
    )

    first = captcha.image_choices()
    second = captcha.image_choices()

    assert first == second == ("image_abc.png",)
    assert len(calls) == 1


def test_cleanup_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_DATA_CLEANUP", raising=False)
    assert cleanup.enabled() is False


def test_cleanup_dry_run_keeps_rows():
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO message_map "
            "(user_chat_message_id, group_chat_message_id, user_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (900001, 900002, 900, "2000-01-01 00:00:00"),
        )

    result = cleanup.cleanup_old_message_maps(retention_days=90, dry_run=True)

    assert result["deleted"] == 0


def test_cleanup_deletes_only_old_rows():
    old_id, recent_id = 920001, 920003
    recent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO message_map "
            "(user_chat_message_id, group_chat_message_id, user_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (old_id, 920002, 920, "2000-01-01 00:00:00"),
        )
        connection.execute(
            "INSERT INTO message_map "
            "(user_chat_message_id, group_chat_message_id, user_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (recent_id, 920004, 920, recent),
        )

    result = cleanup.cleanup_old_message_maps(retention_days=90, dry_run=False)

    assert result["deleted"] >= 1
    counts = {
        row[0]: row[1]
        for row in database.connect().execute(
            "SELECT user_chat_message_id, COUNT(*) FROM message_map "
            "WHERE user_chat_message_id IN (?, ?) GROUP BY user_chat_message_id",
            (old_id, recent_id),
        )
    }
    assert counts.get(old_id, 0) == 0
    assert counts.get(recent_id, 0) == 1
