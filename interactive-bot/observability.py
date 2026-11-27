"""Lightweight performance instrumentation.

Everything here is opt-in: set ``PERF_LOG=1`` to emit one JSON line per
instrumented event on stdout (captured by systemd journal). ``PERF_SAMPLE_RATE``
keeps the volume bounded on a busy bot. The default is completely silent and
adds only one environment lookup per event.
"""

import asyncio
import json
import logging
import os
import random
import time
from contextlib import contextmanager

LOGGER = logging.getLogger("interactive-bot.perf")


def _enabled() -> bool:
    if os.getenv("PERF_LOG", "0") != "1":
        return False
    try:
        rate = float(os.getenv("PERF_SAMPLE_RATE", "1"))
    except ValueError:
        rate = 1.0
    return rate >= 1.0 or random.random() < rate


def log_perf(event: str, **fields) -> None:
    """Emit a sampled JSON perf record, or nothing when disabled."""
    if not _enabled():
        return
    payload = {"event": event, **fields}
    LOGGER.info("PERF " + json.dumps(payload, sort_keys=True, default=str))


@contextmanager
def stage_timer(event: str, **fields):
    """Time a block and emit ``duration_ms`` when instrumentation is on."""
    started = time.perf_counter()
    try:
        yield
    finally:
        log_perf(
            event,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            **fields,
        )


async def event_loop_probe(interval: float = 1.0) -> None:
    """Run until cancelled, logging how late each periodic wake-up was."""
    loop = asyncio.get_running_loop()
    while True:
        target = loop.time() + interval
        await asyncio.sleep(interval)
        lag_ms = max(0.0, (loop.time() - target) * 1000)
        log_perf("event_loop_lag", lag_ms=round(lag_ms, 3))
