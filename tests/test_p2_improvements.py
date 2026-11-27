import importlib
import logging
from datetime import datetime, timedelta

from db.database import session_scope
from db.model import MessageMap

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


def test_persistence_interval_reads_env(monkeypatch):
    monkeypatch.setenv("PERSISTENCE_INTERVAL", "300")
    assert main_mod.persistence_interval() == 300


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
    with session_scope() as session:
        session.add(
            MessageMap(
                user_chat_message_id=900001,
                group_chat_message_id=900002,
                user_id=900,
                created_at=datetime(2000, 1, 1),
            )
        )

    result = cleanup.cleanup_old_message_maps(retention_days=90, dry_run=True)

    assert result["deleted"] == 0


def test_cleanup_deletes_only_old_rows():
    old_id, recent_id = 920001, 920003
    with session_scope() as session:
        session.add(
            MessageMap(
                user_chat_message_id=old_id,
                group_chat_message_id=920002,
                user_id=920,
                created_at=datetime(2000, 1, 1),
            )
        )
        session.add(
            MessageMap(
                user_chat_message_id=recent_id,
                group_chat_message_id=920004,
                user_id=920,
                created_at=datetime.now() - timedelta(days=1),
            )
        )

    result = cleanup.cleanup_old_message_maps(retention_days=90, dry_run=False)

    assert result["deleted"] >= 1
    with session_scope() as session:
        assert (
            session.query(MessageMap)
            .filter(MessageMap.user_chat_message_id == old_id)
            .count()
            == 0
        )
        assert (
            session.query(MessageMap)
            .filter(MessageMap.user_chat_message_id == recent_id)
            .count()
            == 1
        )
