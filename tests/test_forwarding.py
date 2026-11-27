import importlib
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import event

import db.database as database
import db.model as model
from db.database import session_scope

main_mod = importlib.import_module("interactive-bot.__main__")
probe = importlib.import_module("scripts.perf_probe")
repository = importlib.import_module("db.repository")
executor = importlib.import_module("db.executor")


class SQLCounter:
    def __init__(self):
        self.user_selects = 0
        self.commits = 0

    def install(self):
        def before_cursor(connection, cursor, statement, parameters, context, many=False):
            lowered = statement.lower()
            if lowered.startswith("select") and "from user" in lowered:
                self.user_selects += 1

        def on_commit(connection):
            self.commits += 1

        self._before = before_cursor
        self._commit = on_commit
        event.listen(database.engine, "before_cursor_execute", before_cursor)
        event.listen(database.engine, "commit", on_commit)

    def remove(self):
        event.remove(database.engine, "before_cursor_execute", self._before)
        event.remove(database.engine, "commit", self._commit)


def _seed_user(user_id=1, thread_id=7):
    with session_scope() as session:
        session.add(
            model.User(
                user_id=user_id,
                first_name="Seeded",
                message_thread_id=thread_id,
            )
        )


@pytest.mark.asyncio
async def test_forwarding_uses_copy_message_not_get_chat():
    _seed_user(user_id=11, thread_id=17)
    bot = probe._configured_bot()
    context = probe.make_context(bot)
    update = probe.build_update("text", user_id=11, chat_id=11, message_id=2001)

    await main_mod.forwarding_message_u2a(update, context)

    assert bot.copy_message.await_count == 1
    assert bot.get_chat.await_count == 0


@pytest.mark.asyncio
async def test_forwarding_queries_user_once():
    _seed_user(user_id=12, thread_id=18)
    counter = SQLCounter()
    counter.install()
    try:
        context = probe.make_context(probe._configured_bot())
        update = probe.build_update("text", user_id=12, chat_id=12, message_id=2002)
        await main_mod.forwarding_message_u2a(update, context)
    finally:
        counter.remove()

    assert counter.user_selects <= 1


@pytest.mark.asyncio
async def test_new_user_first_message_commits_at_most_twice():
    counter = SQLCounter()
    counter.install()
    try:
        context = probe.make_context(probe._configured_bot())
        update = probe.build_update("text", user_id=13, chat_id=13, message_id=2003)
        await main_mod.forwarding_message_u2a(update, context)
    finally:
        counter.remove()

    assert counter.commits <= 2


@pytest.mark.asyncio
async def test_run_db_executes_in_worker_thread():
    caller_thread = threading.get_ident()
    worker_thread = await executor.run_db(threading.get_ident)

    assert worker_thread != caller_thread


def test_repository_returns_plain_dto():
    _seed_user(user_id=14, thread_id=19)

    dto = repository.get_user_by_user_id(14)

    assert dto.user_id == 14
    assert not isinstance(dto, model.User)


def test_repository_batch_insert_commits_once():
    counter = SQLCounter()
    counter.install()
    try:
        repository.create_message_maps_batch(
            [(3001, 4001, 15), (3002, 4002, 15), (3003, 4003, 15)]
        )
    finally:
        counter.remove()

    assert counter.commits == 1


@pytest.mark.asyncio
async def test_first_album_reuses_persisted_topic_and_routes_later_copy():
    bot = probe._configured_bot()
    context = probe.make_context(bot)
    for message_id in (2101, 2102):
        await main_mod.forwarding_message_u2a(
            probe.build_update(
                "media", user_id=21, chat_id=21, message_id=message_id,
                media_group_id="first-album",
            ),
            context,
        )

    assert repository.get_user_by_user_id(21).message_thread_id == 7
    assert bot.create_forum_topic.await_count == 1
    assert context.job_queue.run_once.call_count == 1
    (callback, _), job = context.job_queue.run_once.call_args
    context.job = SimpleNamespace(data=job["data"], name=job["name"])
    await callback(context)

    assert bot.copy_messages.await_args.kwargs["message_thread_id"] == 7
    assert bot.copy_messages.await_args.kwargs["message_ids"] == [2101, 2102]


@pytest.mark.asyncio
async def test_reply_uses_own_private_chat_when_message_ids_collide():
    _seed_user(user_id=22, thread_id=20)
    _seed_user(user_id=23, thread_id=21)
    repository.create_message_maps_batch([(99, 801, 22), (99, 802, 23)])
    bot = probe._configured_bot()
    await main_mod.forwarding_message_u2a(
        probe.build_update("reply", user_id=23, chat_id=23, message_id=2201),
        probe.make_context(bot),
    )

    assert bot.copy_message.await_args.kwargs["reply_to_message_id"] == 802
