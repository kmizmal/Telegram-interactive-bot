"""Sample a running bot's memory from /proc and emit JSON Lines.

Examples::

    uv run python scripts/memory_probe.py --self-check
    uv run python scripts/memory_probe.py --pid "$(systemctl show -p MainPID --value telegram-bot.service)" --interval 60 --duration 1800
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_FIELDS = {
    "VmRSS": "rss_kb",
    "VmHWM": "hwm_kb",
    "VmData": "data_kb",
    "Threads": "threads",
}


def parse_proc_status(text: str) -> dict:
    """Parse the memory/thread fields from /proc/<pid>/status."""
    result = {name: None for name in _FIELDS.values()}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key not in _FIELDS:
            continue
        parts = value.split()
        if not parts:
            continue
        try:
            result[_FIELDS[key]] = int(parts[0])
        except ValueError:
            continue
    return result


def snapshot(pid: int | None = None) -> dict:
    """Return one memory snapshot for *pid* (default: current process)."""
    target = os.getpid() if pid is None else int(pid)
    status_path = Path(f"/proc/{target}/status")
    values = parse_proc_status(status_path.read_text())
    if values["rss_kb"] is None:
        raise RuntimeError(f"VmRSS not found in {status_path}")
    values.update(
        pid=target,
        timestamp=datetime.now(timezone.utc).isoformat(),
        host=os.uname().nodename,
    )
    return values


def _self_check() -> int:
    sample = "VmRSS:  66816 kB\nVmHWM:  70000 kB\nVmData: 68556 kB\nThreads: 3\n"
    expected = {
        "rss_kb": 66816,
        "hwm_kb": 70000,
        "data_kb": 68556,
        "threads": 3,
    }
    if parse_proc_status(sample) != expected:
        print("self-check failed: unexpected parse result", file=sys.stderr)
        return 1
    print("self-check ok")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, default=None, help="target PID (default: self)")
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between samples")
    parser.add_argument(
        "--duration",
        type=float,
        default=1800.0,
        help="total sampling seconds; 0 emits one sample",
    )
    parser.add_argument("--self-check", action="store_true", help="validate the parser and exit")
    args = parser.parse_args(argv)

    if args.self_check:
        return _self_check()
    if args.interval <= 0:
        parser.error("--interval must be positive")

    deadline = time.monotonic() + max(0.0, args.duration)
    while True:
        try:
            sample = snapshot(args.pid)
        except (OSError, RuntimeError) as error:
            print(f"memory probe failed: {error}", file=sys.stderr)
            return 1
        print(json.dumps(sample, ensure_ascii=False), flush=True)

        remaining = deadline - time.monotonic()
        if args.duration <= 0 or remaining <= 0:
            break
        time.sleep(min(args.interval, remaining))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
