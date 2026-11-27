import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The bot package validates its environment at import time. Give tests a
# deterministic, non-production configuration before any bot module is loaded.
os.environ.setdefault("BOT_TOKEN", "123456:UNIT_TEST_TOKEN")
os.environ.setdefault("APP_NAME", "unit-test-bot")
os.environ.setdefault("ADMIN_GROUP_ID", "-1001234567890")
os.environ.setdefault("ADMIN_USER_IDS", "1,2")
os.environ.setdefault("PERF_LOG", "0")
os.environ.setdefault("PERF_SAMPLE_RATE", "1")
