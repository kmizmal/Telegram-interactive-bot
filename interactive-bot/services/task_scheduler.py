"""A tiny in-process scheduler for the few delayed jobs the bot needs.

The bot only needs one-shot delays and one daily cleanup.  Keeping those jobs
on ``asyncio`` avoids importing APScheduler (and the PTB job-queue extra),
which saves roughly 2 MB RSS on the small production host while preserving
the small public surface used by the handlers: ``run_once``, ``run_daily``,
``get_jobs_by_name`` and ``schedule_removal``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as datetime_time, timedelta
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Callback = Callable[[Any], Awaitable[None]]


class ScheduledJob:
    """A handle returned by :class:`TaskScheduler`."""

    def __init__(self, *, name: str | None, data: Any, chat_id: Any):
        self.name = name
        self.data = data
        self.chat_id = chat_id
        self.cancelled = False
        self._task: asyncio.Task | None = None

    def schedule_removal(self) -> None:
        self.cancelled = True
        if self._task is not None and not self._task.done():
            self._task.cancel()


class TaskScheduler:
    """Minimal replacement for ``telegram.ext.JobQueue`` used by this bot."""

    def __init__(self) -> None:
        self._application = None
        self._jobs: set[ScheduledJob] = set()
        self._named: dict[str, ScheduledJob] = {}
        self._daily_specs: list[tuple[Callback, datetime_time, str | None]] = []
        self._started = False

    def set_application(self, application) -> None:
        self._application = application

    async def start(self) -> None:
        self._started = True
        for callback, at, name in list(self._daily_specs):
            self._spawn_daily(callback, at, name)

    async def stop(self, wait: bool = True) -> None:
        self._started = False
        tasks = [job._task for job in self._jobs if job._task is not None]
        for task in tasks:
            task.cancel()
        if wait and tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
        self._named.clear()

    def run_once(
        self,
        callback: Callback,
        when,
        *,
        chat_id=None,
        name: str | None = None,
        data: Any = None,
    ) -> ScheduledJob:
        delay = (
            when.total_seconds()
            if isinstance(when, timedelta)
            else max(0.0, float(when))
        )
        job = ScheduledJob(name=name, data=data, chat_id=chat_id)
        self._replace_named(job)

        async def runner() -> None:
            try:
                await asyncio.sleep(delay)
                if not job.cancelled:
                    await callback(self._make_context(job))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduled job failed: %s", name)
            finally:
                self._forget(job)

        job._task = asyncio.create_task(runner())
        self._jobs.add(job)
        return job

    def run_daily(
        self,
        callback: Callback,
        time: datetime_time,
        days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6),
        name: str | None = None,
    ) -> ScheduledJob:
        del days  # every day is sufficient for this bot
        spec = (callback, time, name)
        self._daily_specs.append(spec)
        if self._started:
            return self._spawn_daily(callback, time, name)
        job = ScheduledJob(name=name, data=None, chat_id=None)
        self._replace_named(job)
        return job

    def get_jobs_by_name(self, name: str) -> list[ScheduledJob]:
        job = self._named.get(name)
        return [job] if job is not None and not job.cancelled else []

    def remove_job(self, name: str) -> bool:
        job = self._named.pop(name, None)
        if job is None:
            return False
        job.schedule_removal()
        self._jobs.discard(job)
        return True

    def _spawn_daily(
        self, callback: Callback, at: datetime_time, name: str | None
    ) -> ScheduledJob:
        job = ScheduledJob(name=name, data=None, chat_id=None)
        self._replace_named(job)

        async def runner() -> None:
            try:
                while not job.cancelled:
                    now = datetime.now()

                    target = datetime.combine(now.date(), at)
                    if target <= now:
                        target += timedelta(days=1)
                    await asyncio.sleep((target - now).total_seconds())
                    if not job.cancelled:
                        await callback(self._make_context(job))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("daily job failed: %s", name)
            finally:
                self._forget(job)

        job._task = asyncio.create_task(runner())
        self._jobs.add(job)
        return job

    def _replace_named(self, job: ScheduledJob) -> None:
        if job.name is None:
            return
        old = self._named.get(job.name)
        if old is not None and old is not job:
            old.schedule_removal()
            self._jobs.discard(old)
        self._named[job.name] = job

    def _forget(self, job: ScheduledJob) -> None:
        self._jobs.discard(job)
        if job.name is not None and self._named.get(job.name) is job:
            self._named.pop(job.name, None)

    def _make_context(self, job: ScheduledJob):
        if self._application is None:
            raise RuntimeError("scheduler is not attached to an application")
        return SimpleNamespace(
            application=self._application,
            bot=self._application.bot,
            user_data=self._application.user_data,
            chat_data=self._application.chat_data,
            bot_data=self._application.bot_data,
            job=job,
        )
