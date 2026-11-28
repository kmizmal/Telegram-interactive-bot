import asyncio
import importlib
from types import SimpleNamespace

import pytest

TaskScheduler = importlib.import_module("interactive-bot.services.task_scheduler").TaskScheduler


@pytest.mark.asyncio
async def test_run_once_delivers_job_context():
    scheduler = TaskScheduler()
    bot = object()
    application = SimpleNamespace(
        bot=bot, bot_data={}, user_data={}, chat_data={}
    )
    scheduler.set_application(application)
    seen = []

    async def callback(context):
        seen.append((context.job.data, context.job.chat_id, context.bot is bot))

    scheduler.run_once(callback, 0, chat_id=123, name="job-1", data="payload")
    await asyncio.sleep(0.05)

    assert seen == [("payload", 123, True)]
    assert scheduler.get_jobs_by_name("job-1") == []


@pytest.mark.asyncio
async def test_named_job_is_replaced_and_can_be_removed():
    scheduler = TaskScheduler()
    application = SimpleNamespace(
        bot=object(), bot_data={}, user_data={}, chat_data={}
    )
    scheduler.set_application(application)
    seen = []

    async def callback(context):
        seen.append(context.job.data)

    first = scheduler.run_once(callback, 0.3, name="same", data="first")
    second = scheduler.run_once(callback, 0.2, name="same", data="second")
    await asyncio.sleep(0.05)

    assert first.cancelled is True
    assert second.cancelled is False
    assert seen == []
    assert scheduler.remove_job("same") is True
    await asyncio.sleep(0.25)
    assert seen == []
    assert scheduler.get_jobs_by_name("same") == []
