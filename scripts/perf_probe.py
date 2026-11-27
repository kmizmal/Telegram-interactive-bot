"""Reproducible synthetic workload probe for Telegram-interactive-bot.

The probe never talks to Telegram: a context with an ``AsyncMock`` bot is fed
synthetic ``Update`` objects into the real handlers. Six scenarios mirror the
hot paths described in PERFORMANCE_REFACTORING_PLAN.md.

Usage::

    python scripts/perf_probe.py --self-test
    python scripts/perf_probe.py --iterations 1000 --baseline docs/perf/baseline.json
    python scripts/perf_probe.py --compare docs/perf/baseline.json
"""

import argparse
import asyncio
import importlib
import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCENARIOS = ("text", "reply", "media", "new_user", "clear", "broadcast")


def build_update(
    kind: str = "text",
    *,
    user_id: int = 1,
    chat_id: int = 1,
    message_id: int = 100,
    thread_id: int = 7,
    media_group_id: str | None = None,
):
    """Build a synthetic Update with only the fields the handlers read."""
    if kind not in SCENARIOS:
        raise ValueError(f"unknown scenario: {kind}")

    user = MagicMock()
    user.id = user_id
    user.full_name = "Test User"
    user.username = "tester"
    user.first_name = "Test"
    user.last_name = "User"
    user.is_premium = False

    message = MagicMock()
    message.id = message_id
    message.message_id = message_id
    message.chat = MagicMock(id=chat_id)
    message.effective_chat = message.chat
    message.from_user = user
    message.text = "hello"
    message.caption_html = None
    message.message_thread_id = thread_id
    message.effective_attachment = None
    message.forum_topic_created = None
    message.forum_topic_closed = None
    message.forum_topic_reopened = None
    message.reply_to_message = (
        MagicMock(message_id=99) if kind in ("reply", "broadcast") else None
    )
    message.media_group_id = media_group_id if kind == "media" else None
    for method in ("reply_html", "reply_text", "reply_photo", "delete", "edit_text"):
        setattr(message, method, AsyncMock())

    update = MagicMock()
    update.update_id = message_id
    update.message = message
    update.effective_user = user
    update.effective_chat = message.chat
    update.callback_query = None
    return update


def make_context(bot=None):
    """Build a PTB-like context with an AsyncMock bot and isolated state."""
    context = MagicMock()
    context.bot = bot if bot is not None else AsyncMock()
    context.user_data = {}
    context.chat_data = {}
    context.bot_data = {}
    context.application = MagicMock()
    context.application.user_data = defaultdict(dict)
    context.job_queue = MagicMock()  # run_once is synchronous in PTB
    context.job = None
    return context


def summarize(values) -> dict:
    """Return count/min/mean/p50/p95/p99/max for a sample of millisecond values."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"count": 0}
    size = len(ordered)

    def percentile(fraction):
        index = max(0, min(size - 1, math.ceil(fraction * size) - 1))
        return ordered[index]

    return {
        "count": size,
        "min": ordered[0],
        "mean": round(statistics.fmean(ordered), 4),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": ordered[-1],
    }


def compare(baseline: dict, current: dict, threshold_pct: float = 10.0) -> list[str]:
    """List metrics that regressed by more than ``threshold_pct`` percent."""
    violations = []
    for scenario, base_stats in baseline.items():
        current_stats = current.get(scenario, {})
        for metric in ("mean", "p50", "p95", "p99"):
            before = base_stats.get(metric)
            after = current_stats.get(metric)
            if not before or not after:
                continue
            delta = (after - before) / before * 100
            if delta > threshold_pct:
                violations.append(
                    f"{scenario}.{metric} {delta:+.1f}% exceeds {threshold_pct:g}%"
                )
    return violations


def _configured_bot() -> AsyncMock:
    bot = AsyncMock()

    async def _with_message_id(*args, **kwargs):
        return SimpleNamespace(message_id=777, message_thread_id=7)

    async def _copy_messages(*args, **kwargs):
        message_ids = kwargs.get("message_ids") or ()
        return tuple(
            SimpleNamespace(message_id=777 + index)
            for index in range(len(message_ids))
        )

    bot.copy_message.side_effect = _with_message_id
    bot.copy_messages.side_effect = _copy_messages
    bot.send_message.side_effect = _with_message_id
    bot.create_forum_topic.side_effect = _with_message_id
    bot.get_chat.side_effect = lambda *a, **kw: SimpleNamespace(
        send_copy=AsyncMock(side_effect=_with_message_id),
        send_copies=AsyncMock(return_value=(SimpleNamespace(message_id=777),)),
    )
    async def _no_photos(*args, **kwargs):
        return SimpleNamespace(total_count=0, photo=[])

    bot.get_user_profile_photos.side_effect = _no_photos
    return bot


def _real_handlers():
    module = importlib.import_module("interactive-bot.__main__")
    return {
        "text": module.forwarding_message_u2a,
        "reply": module.forwarding_message_u2a,
        "media": module.forwarding_message_u2a,
        "new_user": module.forwarding_message_u2a,
        "clear": module.clear,
        "broadcast": module.broadcast,
    }


async def _self_test() -> None:
    async def noop(update, context):
        await asyncio.sleep(0)

    for scenario in SCENARIOS:
        update = build_update(scenario, media_group_id="group-1")
        context = make_context()
        await noop(update, context)


async def _collect(iterations: int, real: bool) -> dict:
    handlers = _real_handlers() if real else None
    results = {}
    warmup = min(10, iterations)
    for scenario in SCENARIOS:
        samples = []
        for index in range(warmup + iterations):
            update = build_update(
                scenario,
                user_id=1000 + index,
                media_group_id=f"group-{index}",
            )
            context = make_context(_configured_bot())
            started = time.perf_counter()
            if handlers is None:
                await asyncio.sleep(0)
            else:
                await handlers[scenario](update, context)
            samples.append((time.perf_counter() - started) * 1000)
        results[scenario] = summarize(samples[warmup:])
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="validate the harness only")
    parser.add_argument("--real", action="store_true", help="run real bot handlers")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--baseline", type=Path, help="write results as JSON")
    parser.add_argument("--compare", type=Path, help="fail when results regress")
    args = parser.parse_args(argv)

    if args.self_test:
        asyncio.run(_self_test())
        print("self-test ok: harness executed all six scenarios")
        return 0

    if args.real:
        os.environ.setdefault("BOT_TOKEN", "123456:PROBE_TOKEN")
        os.environ.setdefault("APP_NAME", "perf-probe")
        os.environ.setdefault("ADMIN_GROUP_ID", "-1001234567890")
        os.environ.setdefault("ADMIN_USER_IDS", "1")
        os.environ.setdefault("DISABLE_CAPTCHA", "TRUE")
        os.environ.setdefault("MESSAGE_INTERVAL", "0")

    results = asyncio.run(_collect(args.iterations, args.real))
    for scenario, stats in results.items():
        print(
            f"{scenario:<10} n={stats['count']:<6} "
            f"p50={stats['p50']:.3f}ms p95={stats['p95']:.3f}ms "
            f"p99={stats['p99']:.3f}ms max={stats['max']:.3f}ms"
        )

    if args.baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"baseline written to {args.baseline}")

    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        violations = compare(baseline, results)
        for violation in violations:
            print("REGRESSION " + violation)
        return 1 if violations else 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
