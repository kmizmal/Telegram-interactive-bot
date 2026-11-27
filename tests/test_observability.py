import asyncio
import importlib
import json
import logging
import time

import pytest

mod = importlib.import_module("interactive-bot.observability")


def _perf_messages(caplog):
    return [r.message for r in caplog.records if r.message.startswith("PERF ")]


def test_log_perf_emits_json_when_enabled(monkeypatch, caplog):
    monkeypatch.setenv("PERF_LOG", "1")
    monkeypatch.setenv("PERF_SAMPLE_RATE", "1")
    with caplog.at_level(logging.INFO):
        mod.log_perf("handler", handler="start", duration_ms=12.5, ok=True)

    lines = _perf_messages(caplog)
    assert len(lines) == 1
    payload = json.loads(lines[0][5:])
    assert payload["event"] == "handler"
    assert payload["handler"] == "start"
    assert payload["duration_ms"] == 12.5
    assert payload["ok"] is True


def test_log_perf_silent_when_disabled(monkeypatch, caplog):
    monkeypatch.setenv("PERF_LOG", "0")
    with caplog.at_level(logging.INFO):
        mod.log_perf("handler", handler="start", duration_ms=1)

    assert _perf_messages(caplog) == []


def test_stage_timer_logs_elapsed_ms(monkeypatch, caplog):
    monkeypatch.setenv("PERF_LOG", "1")
    monkeypatch.setenv("PERF_SAMPLE_RATE", "1")
    with caplog.at_level(logging.INFO):
        with mod.stage_timer("stage", name="db"):
            time.sleep(0.01)

    lines = _perf_messages(caplog)
    assert len(lines) == 1
    payload = json.loads(lines[0][5:])
    assert payload["event"] == "stage"
    assert payload["name"] == "db"
    assert payload["duration_ms"] >= 5


@pytest.mark.asyncio
async def test_event_loop_probe_logs_lag(monkeypatch):
    seen = []
    monkeypatch.setattr(mod, "log_perf", lambda event, **fields: seen.append((event, fields)))

    task = asyncio.create_task(mod.event_loop_probe(interval=0.005))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(seen) >= 2
    assert all(event == "event_loop_lag" for event, _ in seen)
    assert all(fields["lag_ms"] >= 0 for _, fields in seen)
