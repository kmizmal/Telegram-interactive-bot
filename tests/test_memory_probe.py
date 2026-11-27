import json
import os

import scripts.memory_probe as mod


def test_parse_proc_status():
    text = "VmRSS:  66816 kB\nVmHWM:  70000 kB\nVmData: 68556 kB\nThreads: 3\n"
    assert mod.parse_proc_status(text) == {
        "rss_kb": 66816,
        "hwm_kb": 70000,
        "data_kb": 68556,
        "threads": 3,
    }


def test_snapshot_uses_current_process():
    result = mod.snapshot()
    assert result["pid"] == os.getpid()
    assert result["rss_kb"] > 0
    assert result["threads"] >= 1


def test_main_self_check(capsys):
    assert mod.main(["--self-check"]) == 0
    assert "self-check ok" in capsys.readouterr().out


def test_main_emits_json_lines(capsys):
    assert mod.main(["--pid", str(os.getpid()), "--duration", "0", "--interval", "1"]) == 0
    output = capsys.readouterr().out.strip()
    assert output
    payload = json.loads(output.splitlines()[0])
    assert payload["pid"] == os.getpid()
    assert payload["rss_kb"] > 0
