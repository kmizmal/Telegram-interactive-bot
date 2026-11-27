import asyncio
import importlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, RetryAfter

from db import repository
from db.database import session_scope
from db.model import User

main_mod = importlib.import_module("interactive-bot.__main__")
processor = importlib.import_module("interactive-bot.services.update_processor")
probe = importlib.import_module("scripts.perf_probe")


def test_concurrency_limit_defaults_off_and_can_be_enabled(monkeypatch):
    monkeypatch.delenv("PERF_CONCURRENT_UPDATES", raising=False)
    assert processor.concurrency_limit() == 0

    monkeypatch.setenv("PERF_CONCURRENT_UPDATES", "8")
    assert processor.concurrency_limit() == 8


def test_configure_builder_only_sets_concurrency_when_enabled(monkeypatch):
    calls = []

    class Builder:
        def concurrent_updates(self, limit):
            calls.append(limit)
            return self

    monkeypatch.setenv("PERF_CONCURRENT_UPDATES", "0")
    builder = Builder()
    assert processor.configure_builder(builder) is builder
    assert calls == []

    monkeypatch.setenv("PERF_CONCURRENT_UPDATES", "4")
    processor.configure_builder(builder)
    assert calls == [4]


@pytest.mark.asyncio
async def test_user_lock_serializes_same_user():
    order = []

    async def work(user_id, position):
        async with processor.user_lock(None, user_id):
            await asyncio.sleep(0)
            order.append((user_id, position))

    await asyncio.gather(*(work(1, index) for index in range(10)), work(2, 99))

    assert [position for user_id, position in order if user_id == 1] == list(range(10))


@pytest.mark.asyncio
async def test_broadcast_copies_once_per_user_and_retries(monkeypatch):
    users = [SimpleNamespace(user_id=user_id) for user_id in (101, 102, 103)]
    monkeypatch.setattr(main_mod.repository, "get_all_users", lambda: users)

    calls = []

    async def copy_message(*args, **kwargs):
        calls.append(kwargs["chat_id"])
        if len(calls) == 1:
            raise RetryAfter(timedelta(milliseconds=1))
        return SimpleNamespace(message_id=1)

    bot = probe._configured_bot()
    bot.copy_message.side_effect = copy_message
    context = probe.make_context(bot)
    context.job = SimpleNamespace(data="555_-1001")

    await main_mod._broadcast(context)

    assert sorted(calls) == [101, 101, 102, 103]


@pytest.mark.asyncio
async def test_broadcast_uses_at_most_five_parallel_sends(monkeypatch):
    users = [SimpleNamespace(user_id=user_id) for user_id in range(200, 215)]
    monkeypatch.setattr(main_mod.repository, "get_all_users", lambda: users)

    active = 0
    peak = 0

    async def copy_message(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return SimpleNamespace(message_id=1)

    bot = probe._configured_bot()
    bot.copy_message.side_effect = copy_message
    context = probe.make_context(bot)
    context.job = SimpleNamespace(data="555_-1001")

    await main_mod._broadcast(context)

    assert peak <= 5


@pytest.mark.asyncio
async def test_clear_retries_rate_limit_and_keeps_failed_chunk_mappings(monkeypatch):
    with session_scope() as session:
        session.add(User(user_id=77, message_thread_id=314))
    repository.create_message_maps_batch(
        [(message_id, message_id + 1000, 77) for message_id in range(1, 251)]
        + [(1, 5000, 78)]
    )
    monkeypatch.setattr(main_mod, "is_delete_user_messages", True)
    bot = probe._configured_bot()
    bot.delete_messages.side_effect = [True, BadRequest("cannot delete"),
                                       RetryAfter(timedelta(milliseconds=1)), True]
    update = probe.build_update("clear", user_id=1, chat_id=-1001, thread_id=314)

    await main_mod.clear(update, probe.make_context(bot))

    chunks = [call.args[1] for call in bot.delete_messages.await_args_list]
    assert [len(chunk) for chunk in chunks] == [100, 100, 50, 50]
    assert chunks[2] == chunks[3]
    assert sorted(repository.get_message_ids_for_user(77)) == list(range(101, 201))
    assert repository.get_message_ids_for_user(78) == [1]
