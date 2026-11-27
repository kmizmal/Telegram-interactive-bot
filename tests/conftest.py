import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="tgbot-tests-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_DB_DIR / 'test.sqlite3'}")

# The bot package validates its environment at import time. Give tests a
# deterministic, non-production configuration before any bot module is loaded.
os.environ.setdefault("BOT_TOKEN", "123456:UNIT_TEST_TOKEN")
os.environ.setdefault("APP_NAME", "unit-test-bot")
os.environ.setdefault("ADMIN_GROUP_ID", "-1001234567890")
os.environ.setdefault("ADMIN_USER_IDS", "1,2")
os.environ.setdefault("PERF_LOG", "0")
os.environ.setdefault("PERF_SAMPLE_RATE", "1")
os.environ.setdefault("DISABLE_CAPTCHA", "TRUE")
os.environ.setdefault("MESSAGE_INTERVAL", "0")

import db.database as _database  # noqa: E402
import db.model as _model  # noqa: E402

_model.Base.metadata.create_all(_database.engine)
