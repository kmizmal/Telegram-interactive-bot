# Telegram-interactive-bot 内存优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变业务行为的前提下，把 Telegram-interactive-bot 的数据层从 SQLAlchemy 换成标准库 sqlite3，把验证码状态外置到 SQLite，并移除 PicklePersistence；所有进程内用户缓存与锁必须有界，生产常驻 RSS 目标 ≤55 MB、24 小时增长 ≤3 MB。

**Architecture:** 单 systemd 进程保留 PTB 的 `run_polling`。数据层改为一个受 `threading.RLock` 保护的 sqlite3 连接，开启 WAL/NORMAL、`cache_size=-512`；repository 仍由 `ThreadPoolExecutor(max_workers=4)` 通过 `run_db()` 调用。验证码状态放 `user_state` 表，`user` 业务表不变；`context.user_data`、`bot_data`、`PicklePersistence` 不再承载持久状态。头像/验证码 file_id 用有 maxsize+TTL 的进程内缓存，每用户锁用 `WeakValueDictionary`。

**Tech Stack:** Python 3.11, python-telegram-bot 22.8, stdlib `sqlite3`, `uv`, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-27-memory-optimization-design.md`

## Global Constraints

- Python 3.11；依赖使用 `uv` 锁定，所有验证命令以 `uv run --frozen` 执行。
- 运行时不导入 SQLAlchemy；`sqlite3` 是唯一数据库驱动。
- SQLite PRAGMA 固定：`busy_timeout=5000`、`journal_mode=WAL`、`synchronous=NORMAL`、`cache_size=-512`、`temp_store=FILE`、`foreign_keys=ON`。
- 缓存固定：avatar `maxsize=2048`、captcha image `maxsize=128`、TTL `7*24*3600` 秒；message time `maxsize=4096`。
- 广播页大小 `BROADCAST_PAGE_SIZE=500`；`BROADCAST_MAX_PARALLEL=5` 保持不变。
- 内存验收：生产 30 分钟 idle `VmRSS ≤55,000 kB`；24 小时样本增长 `≤3,000 kB`；`perf_probe` mean/p50/p95/p99 回归 ≤10%。
- 生产切换必须先备份数据库和代码；迁移命令必须幂等；任何步骤失败立即回滚。

## Review Focus

- **Fresh install / empty DB:** `python -m interactive-bot` 或 `python -m db.database` 必须先创建全部表，再允许运行 ALTER-only 迁移；测试覆盖空目录启动。
- **Existing production DB:** 236 users / 6 message_map 的库在迁移后行数、索引、`created_at` 均保持可读；测试覆盖旧 schema 升级。
- **同一用户并发/多线程 DB 调用:** 单连接 + RLock 不能死锁，`WeakValueDictionary` 锁不能在等待者仍在队列时被回收；测试覆盖并发 gather 与 GC。
- **验证码状态跨重启:** `user_state` 必须持久，`PicklePersistence` 不能再被启动路径引用；测试覆盖断连重连与旧 pickle 导入。
- **大广播/大用户表:** `_broadcast` 必须逐页取用户，不得一次性物化全部 DTO/协程；测试覆盖分页调用次数和并发上限。

---

### Task 1: 替换数据层为 stdlib sqlite3

**Files:**
- Create: `db/schema.py`
- Rewrite: `db/database.py`
- Rewrite: `db/repository.py`
- Delete: `db/model.py`
- Modify: `interactive-bot/__main__.py:21-51`
- Modify: `scripts/smoke_test.py:30-38`
- Modify: `tests/conftest.py:25-28`
- Rewrite: `tests/test_sqlite_performance.py`
- Modify: `tests/test_forwarding.py:6-49`
- Modify: `tests/test_p2_improvements.py:5-10,67-118`
- Modify: `tests/test_concurrency_and_broadcast.py:9-11,108-127`

**Interfaces:**
- Consumes: `DATABASE_URL`, env default `sqlite:///./assets/db.sqlite3`.
- Produces:
  - `db.schema.SCHEMA_SQL: str`
  - `db.database.database_path() -> pathlib.Path`
  - `db.database.connect() -> sqlite3.Connection`
  - `db.database.transaction()` / `db.database.read_connection()` context managers
  - `db.database.init_db() -> None`, `db.database.close() -> None`
  - `db.database.set_trace_callback(callback: Callable[[str], None] | None) -> None`（仅测试用，连接创建/已存在连接都能挂上）
  - `db.repository.UserDTO`（字段保持：`user_id, message_thread_id, first_name, last_name, username, is_premium`）
  - `db.repository.MediaGroupDTO`（`message_id, caption_html`）
  - 所有现有 repository 函数保持同名同参数；新增 `get_users_page(limit, offset)`
  - `db.repository.UserStateDTO(is_human: bool = False, vcode: str | None = None, is_human_error_time: float | None = None)`
  - `db.repository.get_user_state(user_id) -> UserStateDTO`、`set_user_vcode(user_id, vcode)`、`mark_user_human(user_id)`、`set_user_captcha_error(user_id, timestamp)`（Task 2 使用）
  - `add_media_group_message(...) -> bool`（本任务实现，Task 3 用）

- [ ] **Step 1: 写数据层失败测试**

在 `tests/test_sqlite_performance.py` 写以下测试（使用 `tmp_path` 与 monkeypatch `DATABASE_URL`，或 conftest 的临时 DB）：

```python
def test_init_db_creates_expected_tables():
    database.init_db()
    names = {row[0] for row in database.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"user", "message_map", "formn_status", "media_group_message", "user_state"} <= names

def test_sqlite_pragmas_are_applied():
    conn = database.connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA cache_size").fetchone()[0] == -512

def test_transaction_rolls_back():
    database.init_db()
    try:
        with database.transaction() as conn:
            conn.execute("INSERT INTO user (user_id, first_name) VALUES (?, ?)", (999, "rollback"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert database.connect().execute(
        "SELECT COUNT(*) FROM user WHERE user_id=?", (999,)
    ).fetchone()[0] == 0
```

再写 `test_repository_roundtrip`（`get_or_create_user` → `set_user_thread` → `find_message_map_by_user_chat`）、`test_repository_batch_insert_commits_once`（`create_message_maps_batch` 后计数为 1 次 `COMMIT`）、`test_message_map_created_at_default`（插入后 `created_at` 非 NULL）。保留并重跑旧 `test_migration_is_idempotent_and_preserves_rows`（`db/migrations/perf_indexes.py` 的 2 条 message_map、索引与二次执行断言）。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --frozen pytest tests/test_sqlite_performance.py -q`
Expected: FAIL（`sqlalchemy`/`db.database` 旧 API 或 `init_db` 不存在）。

- [ ] **Step 3: 实现 `db/schema.py` 与 `db/database.py`**

`db/schema.py` 的 DDL 必须与生产 schema 一致：

```sql
CREATE TABLE IF NOT EXISTS user (
  id INTEGER NOT NULL PRIMARY KEY, user_id INTEGER, first_name VARCHAR(64),
  last_name VARCHAR(64), username VARCHAR(64), is_premium BOOLEAN,
  updated_at DATETIME, message_thread_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_user_user_id ON user (user_id);
CREATE INDEX IF NOT EXISTS ix_user_thread ON user (message_thread_id);
CREATE TABLE IF NOT EXISTS message_map (
  id INTEGER NOT NULL PRIMARY KEY, user_chat_message_id INTEGER,
  group_chat_message_id INTEGER, user_id INTEGER,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_message_map_user_chat ON message_map (user_chat_message_id);
CREATE INDEX IF NOT EXISTS ix_message_map_group_chat ON message_map (group_chat_message_id);
CREATE INDEX IF NOT EXISTS ix_message_map_user ON message_map (user_id);
CREATE TABLE IF NOT EXISTS formn_status (
  id INTEGER NOT NULL PRIMARY KEY, chat_id INTEGER, message_thread_id INTEGER,
  status VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS ix_formn_status_thread ON formn_status (message_thread_id);
CREATE TABLE IF NOT EXISTS media_group_message (
  id INTEGER NOT NULL PRIMARY KEY, chat_id INTEGER, message_id INTEGER,
  media_group_id INTEGER, is_header BOOLEAN, caption_html VARCHAR(65536)
);
CREATE INDEX IF NOT EXISTS ix_media_group_lookup ON media_group_message (media_group_id, chat_id);
CREATE TABLE IF NOT EXISTS user_state (
  user_id INTEGER PRIMARY KEY, is_human INTEGER NOT NULL DEFAULT 0,
  vcode TEXT, is_human_error_time REAL
);
```

`db/database.py` 使用模块级 `_CONNECTION`、`threading.RLock`；`connect()` 首次打开时执行全部 PRAGMA；`transaction()` 在锁内 `yield`、`commit`/`rollback`；`read_connection()` 只读；`init_db()` 执行 `SCHEMA_SQL`；`python -m db.database` 调用 `init_db()` 并打印 `database initialized`。

- [ ] **Step 4: 实现 `db/repository.py`**

逐函数重写为参数化 SQL；`UserDTO`/`MediaGroupDTO` 保持原字段。`get_or_create_user` 使用 `INSERT OR IGNORE` + `SELECT`；`get_user_by_user_id`/`get_user_by_thread` 使用 `ORDER BY id DESC LIMIT 1`；`find_message_map_*` 同样取最新一条；`create_message_map`、`create_message_maps_batch`、`set_user_thread_and_create_map` 显式写 `created_at=CURRENT_TIMESTAMP`；`delete_message_maps` 空列表 no-op；`count_old_message_maps`/`delete_old_message_maps` 将 cutoff 格式化为 `%Y-%m-%d %H:%M:%S`（UTC naive）；`add_media_group_message` 先查存在性再插入并返回 `is_first`；`get_users_page(limit, offset)` 使用 `LIMIT ? OFFSET ?`。实现 `get_user_state`/`set_user_vcode`/`mark_user_human`/`set_user_captcha_error`：查询缺失时返回默认 `UserStateDTO`，写入用 `INSERT ... ON CONFLICT(user_id) DO UPDATE`。

- [ ] **Step 5: 改启动与测试夹具**

`interactive-bot/__main__.py` 删除 `from db.database import Base, engine`，改为 `from db.database import init_db`，把 `Base.metadata.create_all(bind=engine)` 替换为 `init_db()`。
`scripts/smoke_test.py` 用 `database.init_db()` 并断言表集合为 4 张业务表 + `user_state`。
`tests/conftest.py` 删除 `db.model` 导入，改为 `db.database.init_db()`。
`tests/test_forwarding.py` 的 `_seed_user` 改 `repository.get_or_create_user`；把所有 `SQLCounter` 改为 `db.database.set_trace_callback()` 收集语句，`user_selects` 统计 `SELECT` + `FROM user`，commit 统计 `COMMIT`。
`tests/test_p2_improvements.py` 与 `tests/test_concurrency_and_broadcast.py` 的 `session_scope`/`model` 种数据改为 `repository.get_or_create_user` 或 `db.database.transaction()`。

- [ ] **Step 6: 运行全量测试**

Run: `uv run --frozen pytest -q`
Expected: PASS（≥40 tests；旧 SQLAlchemy 测试已重写为 sqlite3）。

- [ ] **Step 7: Commit**

```bash
git add db/schema.py db/database.py db/repository.py interactive-bot/__main__.py scripts/smoke_test.py tests
git commit -F - <<'MSG'
refactor(db): 用 stdlib sqlite3 替换 SQLAlchemy

保留 repository 函数接口，改为单连接 sqlite3 + WAL；新增 user_state 表与
分页用户查询，更新测试夹具和 smoke check。

协作模型: deepseek-v4.1-flash
MSG
```

---

### Task 2: 验证码状态外置并移除 PicklePersistence

**Files:**
- Create: `db/migrations/user_state.py`
- Create: `scripts/import_pickle_state.py`
- Modify: `interactive-bot/handlers/user.py`
- Modify: `interactive-bot/__main__.py`
- Modify: `tests/test_p2_improvements.py`
- Create/Modify: `tests/test_user_state.py`（或放入 `tests/test_memory_architecture.py`）

**Interfaces:**
- Consumes: `db.repository.UserStateDTO`、`get_user_state`、`set_user_vcode`、`mark_user_human`、`set_user_captcha_error`（Task 1 已定义）。
- Produces:
  - `db.migrations.user_state.migrate(database_path) -> dict`（`created_table: bool`）
  - `scripts.import_pickle_state.main(argv=None) -> int`
  - `check_human(update, context, user_state: UserStateDTO) -> bool`
  - `callback_query_vcode` 使用 `user_state`，不再使用 `context.user_data`

- [ ] **Step 1: 写状态迁移与 Pickle 移除失败测试**

新增测试：

```python
def test_user_state_migration_creates_table_idempotently(tmp_path):
    path = tmp_path / "state.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, user_id INTEGER)")
    conn.close()
    first = user_state_migration.migrate(path)
    assert first["ok"] is True and first["created_table"] is True
    second = user_state_migration.migrate(path)
    assert second["created_table"] is False

def test_pickle_import_writes_user_state_only(tmp_path):
    # pickle: {"user_data": {123: {"is_human": True, "vcode": "abc"}}}
    # 调用 main(["--pickle", str(path), "--database", str(db_path)])
    # 断言 user_state(123).is_human == 1；user 表仍无 user_id=123
```

再加 `test_captcha_state_survives_reconnect`：`set_user_vcode(42, "abc")` → `database.close()` → `init_db()`/新连接 → `get_user_state(42).vcode == "abc"`。
`test_pickle_persistence_is_gone`：`inspect.getsource(main_mod)` 不包含 `PicklePersistence`，且 `main_mod.persistence_interval` 不存在。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --frozen pytest tests/test_user_state.py -q`
Expected: FAIL（迁移模块或状态函数不存在）。

- [ ] **Step 3: 实现迁移与导入脚本**

`db/migrations/user_state.py` 使用 `sqlite3.connect`，检查 `sqlite_master` 是否已有 `user_state`；没有则执行 `CREATE TABLE IF NOT EXISTS user_state (...)`，执行 `PRAGMA integrity_check`，返回 `{"ok": integrity == "ok", "integrity": integrity, "created_table": created}`。
`scripts/import_pickle_state.py` 使用 `pickle.load` 读取 `user_data`；对每个 `user_id`/dict 执行 `INSERT INTO user_state ... ON CONFLICT(user_id) DO UPDATE SET ...`；只写 `user_state`，绝不写 `user`；`--allow-missing` 控制缺失 pickle 时是否视为成功。

- [ ] **Step 4: 修改 handlers 和入口**

`interactive-bot/handlers/user.py`：

```python
async def check_human(update, context, user_state):
    if user_state.is_human:
        return True
    if (user_state.is_human_error_time or 0) > time.time() - 120:
        ...
    ...
    await run_db(repository.set_user_vcode, user.id, code)
```

`callback_query_vcode` 改为 `state = await run_db(repository.get_user_state, user.id)`，比对 `state.vcode`；正确 `mark_user_human`，错误 `set_user_captcha_error`。
`interactive-bot/__main__.py`：私聊 handler 先 `state = await run_db(repository.get_user_state, update.effective_user.id)`，传给 `check_human`；验证通过后再 `get_or_create_user`。删除 `PicklePersistence` import、`persistence_interval()`、`.persistence(...)` 与其实例化；删除 `ConversationHandler` import 和未注册的 `error_in_send_media_group`。

- [ ] **Step 5: 运行状态/全量测试**

Run: `uv run --frozen pytest tests/test_user_state.py tests/test_p2_improvements.py -q`
Expected: PASS。再跑 `uv run --frozen pytest -q`，Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add db/migrations/user_state.py scripts/import_pickle_state.py interactive-bot/handlers/user.py interactive-bot/__main__.py tests
git commit -F - <<'MSG'
feat(state): 将验证码状态外置到 user_state 并移除 PicklePersistence

启动不再加载 pickle；旧状态可由一次性导入脚本迁移，user 业务表不受影响。

协作模型: deepseek-v4.1-flash
MSG
```

---

### Task 3: 有界缓存、弱引用锁与媒体组分页

**Files:**
- Create: `interactive-bot/services/memory_cache.py`
- Modify: `interactive-bot/handlers/user.py`
- Modify: `interactive-bot/__main__.py`
- Modify: `interactive-bot/services/update_processor.py`
- Create: `tests/test_memory_architecture.py`
- Modify: `tests/test_concurrency_and_broadcast.py`

**Interfaces:**
- Consumes: `repository.add_media_group_message(...) -> bool`（Task 1）。
- Produces:
  - `memory_cache.TTLCache(maxsize: int, ttl_seconds: float)`
  - `memory_cache.avatar_cache` / `captcha_image_cache`
  - `memory_cache.rate_limit_ok(user_id: int, interval: float) -> bool`
  - `memory_cache.clear_all() -> None`（仅测试）
  - `update_processor.lock_count() -> int`
  - `_broadcast` 使用 `repository.get_users_page(limit, offset)`，页大小 `BROADCAST_PAGE_SIZE=500`

- [ ] **Step 1: 写缓存/锁/分页失败测试**

`tests/test_memory_architecture.py`：

```python
def test_ttl_cache_evicts_expired_and_oldest():
    cache = TTLCache(maxsize=2, ttl_seconds=10)
    cache.set("a", 1, now=0)
    cache.set("b", 2, now=1)
    assert cache.get("b", now=2) == 2
    cache.set("c", 3, now=3)
    assert cache.get("a", now=3) is None
    assert cache.get("b", now=12) is None

def test_rate_limit_resets_after_interval(monkeypatch):
    monkeypatch.setattr(memory_cache.time, "monotonic", lambda: 100.0)
    assert memory_cache.rate_limit_ok(7, 5) is True
    assert memory_cache.rate_limit_ok(7, 5) is False
    monkeypatch.setattr(memory_cache.time, "monotonic", lambda: 106.0)
    assert memory_cache.rate_limit_ok(7, 5) is True
```

再加 `test_user_locks_are_weak_and_evicted`（创建 2000 把锁后 `del locks; gc.collect(); lock_count()` 显著下降）与 `test_media_group_first_flag`（同组第一条 True、第二条 False）。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --frozen pytest tests/test_memory_architecture.py -q`
Expected: FAIL（`memory_cache` 不存在）。

- [ ] **Step 3: 实现 `memory_cache.py`**

`TTLCache` 用 `OrderedDict`；`get` 检查 `expires_at <= now`、命中后 `move_to_end`；`set` 先清理过期，超 `maxsize` 删最旧。模块级缓存按设计常量创建；`rate_limit_ok` 使用 `time.monotonic()` 与 `_message_times`。

- [ ] **Step 4: 改 handler、入口与锁**

`handlers/user.py` 的 avatar/image 读写改为 `avatar_cache.get(user.id)` / `captcha_image_cache.get(code)`，并 `set` 回缓存；不再写 `context.bot_data`。
`__main__.py` 的 `last_message_time` 改为 `rate_limit_ok(user.id, message_interval)`；u2a/a2u 媒体组改用 `add_media_group_message` 的返回值判断首条；删除所有 `context.user_data`/`context.application.user_data` 用法。
`update_processor.py` 的 `_user_locks` 改为 `weakref.WeakValueDictionary`，新增 `lock_count()`。
`__main__.py` 的 `_broadcast` 增加 `BROADCAST_PAGE_SIZE = 500`，按 `get_users_page(limit, offset)` 逐页处理，保留 `BROADCAST_MAX_PARALLEL=5` 与重试逻辑；测试 monkeypatch `get_users_page`。

- [ ] **Step 5: 运行全量测试**

Run: `uv run --frozen pytest -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add interactive-bot/services/memory_cache.py interactive-bot/handlers/user.py interactive-bot/__main__.py interactive-bot/services/update_processor.py tests
git commit -F - <<'MSG'
perf(memory): 用户缓存有界化并改用弱引用锁

头像/验证码缓存加 maxsize+TTL，每用户锁不再永久累积，广播按 500 用户分页。

协作模型: deepseek-v4.1-flash
MSG
```

---

### Task 4: 移除 SQLAlchemy/pytz 依赖并更新文档

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `interactive-bot/utils.py:1-26`
- Modify: `README.md`
- Modify: `deploy/README.md`
- Modify: `docs/perf/README.md`
- Modify: `tests/test_memory_architecture.py`（如需要断言依赖）

- [ ] **Step 1: 写依赖/时区回归测试**

在 `tests/test_memory_architecture.py` 增加：

```python
def test_runtime_does_not_import_sqlalchemy():
    import interactive-bot.__main__ as main  # noqa: F401
    assert "sqlalchemy" not in sys.modules

def test_utils_no_longer_imports_pytz():
    import inspect
    import interactive-bot.utils as utils
    assert "pytz" not in inspect.getsource(utils)
    assert "timezone.utc" in inspect.getsource(utils)
```

- [ ] **Step 2: 修改依赖与代码**

`pyproject.toml` 删除 `sqlalchemy` 与 `pytz`，保留其余依赖。将 `interactive-bot/utils.py` 的 `import pytz` 删除，`datetime.datetime.now(pytz.utc)` 改为 `datetime.datetime.now(datetime.timezone.utc)`。
`README.md` / `deploy/README.md` / `docs/perf/README.md` 按设计文档 §5.4 更新；删除 `PERSISTENCE_INTERVAL` 与 SQLAlchemy/pytz 验证命令，加入 `python -m db.database`、两个迁移、`import_pickle_state.py` 和 `sqlalchemy not in sys.modules` 检查。

- [ ] **Step 3: 重算 lock 并验证**

Run:
```bash
export UV_CACHE_DIR=/root/dsh/.uv-cache UV_PYTHON_INSTALL_DIR=/root/dsh/.uv-python
uv lock
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen python scripts/smoke_test.py
```
Expected: `uv.lock` 不含 SQLAlchemy；pytest PASS；smoke `5` 表输出 `smoke ok`。

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock interactive-bot/utils.py README.md deploy/README.md docs/perf/README.md tests
git commit -F - <<'MSG'
chore(deps): 移除 SQLAlchemy 与 pytz

运行时只保留 sqlite3，文档同步新的迁移与验证流程。

协作模型: deepseek-v4.1-flash
MSG
```

---

### Task 5: 内存采样脚本与基线

**Files:**
- Create: `scripts/memory_probe.py`
- Create: `tests/test_memory_probe.py`
- Create: `docs/perf/memory-baseline-2026-11-27.json`
- Modify: `docs/perf/README.md`
- Create after rollout: `docs/perf/memory-after-2026-11-27.json`

**Interfaces:**
- `parse_proc_status(text: str) -> dict`：解析 `VmRSS`、`VmHWM`、`VmData`、`Threads`（单位 kB）。
- `snapshot(pid: int | None = None) -> dict`：默认当前进程；读 `/proc/<pid>/status`。
- `main(argv=None) -> int`：支持 `--pid`、`--interval`、`--duration`、`--self-check`，输出 JSON Lines。

- [ ] **Step 1: 写解析测试**

```python
def test_parse_proc_status():
    text = "VmRSS:  66816 kB\nVmHWM:  70000 kB\nVmData: 68556 kB\nThreads: 3\n"
    assert mod.parse_proc_status(text) == {
        "rss_kb": 66816, "hwm_kb": 70000, "data_kb": 68556, "threads": 3,
    }
```

- [ ] **Step 2: 实现 `memory_probe.py` 与基线 JSON**

基线 JSON 记录 2026-11-27 生产采样：`{"rss_kb": 66816, "vm_data_kb": 68556, "threads": 3, "vm_peak_kb": 290140, "sample_window_seconds": 360, "import_peak_mb": 58}`，并注明采样 PID/host。

- [ ] **Step 3: 更新 docs/perf/README**

加入：
```bash
uv run python scripts/memory_probe.py --self-check
uv run python scripts/memory_probe.py --pid "$(systemctl show -p MainPID --value telegram-bot.service)" --interval 60 --duration 1800
```
以及 30 分钟 / 24 小时验收标准。

- [ ] **Step 4: 运行测试并提交**

Run: `uv run --frozen pytest tests/test_memory_probe.py -q`；Expected: PASS。
Commit:
```bash
git add scripts/memory_probe.py tests/test_memory_probe.py docs/perf
git commit -F - <<'MSG'
feat(perf): 增加内存采样工具与基线记录

记录生产 RSS/VmData/Threads，支持 30 分钟与 24 小时验收采样。

协作模型: deepseek-v4.1-flash
MSG
```

---

### Task 6: 集成验证、PR 与生产上线

**Files:**
- Modify: `docs/perf/memory-after-2026-11-27.json`（生产采样结果）
- No production code changes expected.

- [ ] **Step 1: 全量本地验证**

Run:
```bash
export UV_CACHE_DIR=/root/dsh/.uv-cache UV_PYTHON_INSTALL_DIR=/root/dsh/.uv-python
uv lock --check
uv run --frozen pytest -q
uv run --frozen python scripts/smoke_test.py
uv run --frozen python scripts/perf_probe.py --self-test
uv run --frozen python scripts/perf_probe.py --real --iterations 100
```
Expected: pytest PASS；smoke 5 表；perf 无回归超过 10%。

- [ ] **Step 2: 代码审查与 PR**

推送到 `perf/memory-optimization`，开 PR 到 `master`；CI `uv sync and smoke check` 必须 SUCCESS。合并前用新分支做一次空 DB 启动验证（`DATABASE_URL=sqlite:////tmp/fresh.sqlite3 uv run python -m interactive-bot` 启动后停掉，检查 5 表）。

- [ ] **Step 3: 生产切换**

按 `deploy/README.md`：停服 → 备份数据库/资产/代码 → `python -m db.database` → `perf_indexes` → `user_state` → `import_pickle_state.py` → 启动。记录迁移返回值和 `PRAGMA integrity_check`。

- [ ] **Step 4: 内存验收**

用 `scripts/memory_probe.py --pid ... --interval 60 --duration 1800` 采 30 分钟；RSS 必须 ≤55,000 kB。再用 5 分钟间隔采 24 小时，增长 ≤3,000 kB；将结果写入 `docs/perf/memory-after-2026-11-27.json`。

- [ ] **Step 5: 失败处理**

若 30 分钟 RSS >55 MB 或 24 小时增长 >3 MB：停止继续优化，保留采样与 `perf` 数据，按设计 §4 方案 C 单独立项评估；若出现功能错误，按 `deploy/README.md` 回滚数据库与代码。

- [ ] **Step 6: Commit 采样结果（若生产完成）**

```bash
git add docs/perf/memory-after-2026-11-27.json
git commit -F - <<'MSG'
docs(perf): 记录内存优化后生产采样

协作模型: deepseek-v4.1-flash
MSG
```
