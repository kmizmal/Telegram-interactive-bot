import importlib

import pytest

mod = importlib.import_module("scripts.perf_probe")


def test_build_update_shapes():
    text = mod.build_update("text")
    assert text.message.message_id == 100
    assert text.effective_user.id == 1
    assert text.message.media_group_id is None

    media = mod.build_update("media", media_group_id="group-1")
    assert media.message.media_group_id == "group-1"

    with pytest.raises(ValueError):
        mod.build_update("unknown")


def test_summarize_percentiles():
    stats = mod.summarize(list(range(1, 101)))

    assert stats["count"] == 100
    assert stats["min"] == 1
    assert stats["max"] == 100
    assert stats["p50"] == 50
    assert stats["p95"] == 95


def test_compare_detects_regression_above_threshold():
    baseline = {"text": {"p95": 10.0}}
    current = {"text": {"p95": 12.0}}

    assert mod.compare(baseline, current, threshold_pct=10) == [
        "text.p95 +20.0% exceeds 10%"
    ]


def test_compare_accepts_change_within_threshold():
    baseline = {"text": {"p95": 10.0}, "media": {"p95": 40.0}}
    current = {"text": {"p95": 10.5}, "media": {"p95": 44.0}}

    assert mod.compare(baseline, current, threshold_pct=10) == []
