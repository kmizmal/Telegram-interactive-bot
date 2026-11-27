"""Optional, opt-in cleanup of stale message maps."""

import os
from datetime import datetime, timedelta

from db import repository


def enabled() -> bool:
    return os.getenv("ENABLE_DATA_CLEANUP", "0") == "1"


def cleanup_old_message_maps(retention_days: int = 90, dry_run: bool = False) -> dict:
    """Count (and optionally delete) message maps older than the retention."""
    cutoff = datetime.now() - timedelta(days=retention_days)
    if dry_run:
        return {
            "deleted": 0,
            "matched": repository.count_old_message_maps(cutoff),
            "cutoff": cutoff.isoformat(),
            "dry_run": True,
        }
    deleted = repository.delete_old_message_maps(cutoff)
    return {"deleted": deleted, "cutoff": cutoff.isoformat(), "dry_run": False}
