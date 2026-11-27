import gc
import importlib
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

    def fake_page(limit, offset):
        pages.append((limit, offset))
        return [SimpleNamespace(user_id=1)] if offset == 0 else []

    monkeypatch.setattr(main_mod.repository, "get_users_page", fake_page)
    context = probe.make_context(probe._configured_bot())
    context.job = SimpleNamespace(data="555_-1001")

    await main_mod._broadcast(context)

    assert pages == [(500, 0), (500, 1)]
