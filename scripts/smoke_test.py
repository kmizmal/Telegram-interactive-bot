"""Smoke check: import the bot package, load configuration and create the SQLite schema.

This does not contact Telegram and is safe to run in CI. It only verifies that the
declared dependencies and the application entry modules are importable.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

# When invoked as `python scripts/smoke_test.py`, sys.path[0] is scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Configuration is read at import time by interactive-bot/__init__.py.
os.environ.setdefault("BOT_TOKEN", "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
os.environ.setdefault("APP_NAME", "interactive-bot")
os.environ.setdefault("ADMIN_GROUP_ID", "-1001234567890")
os.environ.setdefault("ADMIN_USER_IDS", "123456789")

# The database URL is relative to the current directory; use a throwaway one.
workdir = Path(tempfile.mkdtemp(prefix="telegram-interactive-bot-smoke-"))
(workdir / "assets").mkdir(parents=True, exist_ok=True)
os.chdir(workdir)

bot_package = importlib.import_module("interactive-bot")

from db.database import Base, engine  # noqa: E402
import db.model  # noqa: E402,F401  (register ORM models)

Base.metadata.create_all(bind=engine)

expected_tables = {"formn_status", "media_group_message", "message_map", "user"}
actual_tables = set(Base.metadata.tables)
if actual_tables != expected_tables:
    raise SystemExit(f"unexpected tables: {sorted(actual_tables)}")

print(f"smoke ok: app={bot_package.app_name}, tables={sorted(actual_tables)}")
