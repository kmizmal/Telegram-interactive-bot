# Telegram-interactive-bot 内存优化（Worker 式薄存储）设计

- 日期：2026-11-27
- 状态：待评审
- 分支：`perf/memory-optimization`（从 `master` `969defa` 新建）
- 参考：`https://github.com/jokjit/tg-bot-and-panel-manager`、`https://github.com/srlihg66/tgbot-worker`

## 1. 意图与为什么现在做

生产机器人当前常驻 RSS 约 **66.8 MB**（`uk.zmal.top:7422`，2026-11-27 15:04 采样：`VmRSS=66816 kB`、`VmData=68556 kB`、`Threads=3`、`VmPeak=290140 kB`；连续 6 分钟稳定在 66.8 MB）。系统 `MemoryMax=400M`，目前没有 OOM 风险，但希望继续降低常驻内存，并避免用户量增长后内存随用户数无界上涨。

两个参考仓库都是 Cloudflare Worker 架构：`worker.js` 以 webhook 接收更新、请求结束即释放；KV/D1/R2 承载用户状态与历史；本地热缓存有明确上限（ref1 `worker-src/storage/cache.js` 的 `LOCAL_CACHE_MAX_ENTRIES = 2048`，并带 TTL）；清理由 cron trigger 承担。它们的内存优势来自**进程不持有长期用户状态、所有缓存有界**，不是来自某个具体的 JS 库。

本项目的目标是保留 Python + systemd 单进程和一个 SQLite 文件，但把内部按同样的原则改造：**无状态更新、薄存储、有界缓存**。

## 2. 当前测量与热点

### 2.1 依赖导入基线（uk 上同一 venv，`ru_maxrss` 峰值）

| 场景 | 峰值 RSS |
| --- | ---: |
| 裸 CPython 3.11 | 12 MB |
| `import telegram` | 33 MB |
| `import telegram.ext` | 37 MB |
| `import sqlalchemy` | 37 MB（独立进程） |
| `telegram, telegram.ext, dotenv, httpx` | 37 MB |
| 上述 + `sqlalchemy` | 49 MB |
| 上述 + `sqlalchemy, apscheduler, pytz` | 50 MB |
| `interactive-bot.__main__`（应用导入） | 58 MB |
| 生产常驻进程（运行约 15 分钟） | 66.8 MB |

结论：SQLAlchemy 是当前可移除的最大单项，累计约 **12 MB**；PTB 固定成本约 **25 MB**；裸解释器 12 MB。

### 2.2 代码中的无界内存点

- `interactive-bot/services/update_processor.py:13`：`_user_locks: dict[int, asyncio.Lock] = {}`，每个用户一把锁，永不清理。
- `interactive-bot/handlers/user.py:47-53`：`context.bot_data["avatar|{user.id}"]` 每个用户一条，永不清理。
- `interactive-bot/handlers/user.py:118-132`：`context.bot_data["image|{code}"]` 每个验证码图片 file_id 一条。
- `interactive-bot/__main__.py:471-474`：`PicklePersistence` 启动时把整个 pickle 载入内存；生产 `assets/zmalChat-bot.pickle` 当前含 **291 个 `user_data`、292 个 `chat_data`、99 个 `bot_data`**（19 KB）。
- `interactive-bot/__main__.py:144-147`、`:204-207`、`:313-317`：`context.user_data` 保存 `last_message_time`、`current_media_group_id` 等过程状态。
- `db/database.py:18-24`：SQLAlchemy `pool_size=5, max_overflow=5`；SQLite 连接池虽然通常不会全部打开，但 ORM 引擎和会话对象仍常驻。

### 2.3 需要保留的行为

- 私聊消息 → 管理员群话题；管理员回复 → 用户私聊。
- 首条相册先建话题并持久化 `message_thread_id`，后续相册消息复用同一话题。
- 回复映射按 `(user_id, user_chat_message_id)` 精确定位，不能跨用户串号。
- `/clear` 部分 chunk 失败时保留失败 chunk 的映射。
- 验证码通过/失败状态、2 分钟禁言、消息频率限制、媒体组 5 秒聚合、每日清理开关。
- 生产数据库为增量 schema；任何迁移必须幂等、先备份、可回滚。

## 3. 目标、非目标与约束

### 3.1 目标

- **G1 数据层**：运行时不导入 SQLAlchemy；`db/repository.py` 全部改用 Python 标准库 `sqlite3`。现有函数名与参数保持稳定；`UserDTO` 保持现状，新增独立的 `UserStateDTO` 承载验证码状态。
- **G2 状态外置**：验证码状态（`is_human`、`vcode`、`is_human_error_time`）持久化到独立 SQLite `user_state` 表（不污染 `user` 业务表）；移除 `PicklePersistence` 及启动时的全量 pickle 载入。
- **G3 有界内存**：所有进程内按用户/按验证码的缓存都有 `maxsize` 与 TTL；每用户锁使用弱引用，不随用户数累积。
- **G4 可观测**：提供可重复的内存采样脚本与基线数据；上线后 30 分钟 idle RSS **≤55 MB**，24 小时增长 **≤3 MB**（以 5 分钟样本为基准）。
- **G5 功能不回退**：现有 40 个测试全部通过；新增数据层、迁移、缓存、锁、内存断言测试；生产切换后 smoke、`getMe`、管理群可用。

### 3.2 非目标

- 不迁移到 Cloudflare Worker / JavaScript，不引入 KV/D1/R2。
- 不切换 webhook 模式（仍使用 `run_polling`）；不改变 Telegram API 行为。
- 不重写 PTB 框架、不替换 `python-telegram-bot`。
- 不改变 `PERF_CONCURRENT_UPDATES=0` 等生产默认开关；不扩大现有并发。
- 不做用户可见功能改动（欢迎语、验证码 UI、管理命令保持不变）。

### 3.3 约束

- Python 3.11，依赖由 `uv` 锁定；生产 systemd 服务名 `telegram-bot.service`，`MemoryMax=400M`。
- 单机 SQLite 文件 `assets/db.sqlite3`，现有 236 users / 6 message_map / 0 formn_status / 0 media_group_message；WAL 模式。
- `DATABASE_URL` 继续支持 `sqlite:///./assets/db.sqlite3` 形式。
- 现有 `db/migrations/perf_indexes.py` 与 `001_perf_indexes.sql` 必须保留可用；新增迁移独立、幂等。
- 生产切换允许短暂停机（上次实测 4 秒），但必须先备份数据库与代码。

## 4. 方案比较

### 方案 A：只替换 ORM（SQLAlchemy → sqlite3），其余不动

- 收益：约 12 MB RSS；改动集中在 `db/*`，风险最低。
- 不足：`_user_locks`、`bot_data`、`PicklePersistence` 仍随用户增长；没有解决“进程持有用户状态”的架构问题。
- 结论：适合作为保守补丁，不选为本轮主方案。

### 方案 B：Worker 式薄存储（选定的方案）

- 内容：替换 ORM；把验证码等持久状态放进 SQLite；移除 PicklePersistence；缓存全部有界 TTL；锁改弱引用；媒体组首条判断改为数据库查询。
- 收益：约 12–15 MB RSS，且用户状态不再常驻内存；行为可通过现有测试和新增迁移测试验证。
- 代价：改动跨 `db/*`、`interactive-bot/*`、`tests/*`，需要一次生产迁移；中等风险，可按任务分步提交。
- 结论：**采用**。

### 方案 C：用原生 httpx + 自写 dispatcher 替换 PTB

- 收益：理论上可把 PTB 的约 25 MB 固定成本降到约 10–15 MB，RSS 可能到 35–40 MB。
- 代价：等于重写更新分发、过滤器、job queue、异常处理、测试桩；业务回归风险高，和“保持现有行为”冲突。
- 结论：本轮不做；只有方案 B 实测未达到 G4 且用户明确要求更低内存时，再单独立项。

## 5. 选定设计的详细说明

### 5.1 数据层：单连接、标准库 sqlite3

新增/修改：

- `db/schema.py`（新建）：集中放四张业务表（`user`、`message_map`、`formn_status`、`media_group_message`）加 `user_state` 表的 `CREATE TABLE IF NOT EXISTS` 与索引 DDL；字段与当前生产 schema 一致，并给 `message_map.created_at DATETIME DEFAULT CURRENT_TIMESTAMP`。`user_state` 表：
  - `user_id INTEGER PRIMARY KEY`
  - `is_human INTEGER NOT NULL DEFAULT 0`
  - `vcode TEXT`
  - `is_human_error_time REAL`
- `db/database.py`（重写）：
  - `database_path() -> Path`：解析 `DATABASE_URL`，兼容 `sqlite:///` 前缀。
  - `connect() -> sqlite3.Connection`：进程内**单个**连接，`check_same_thread=False`，`sqlite3.Row` 行工厂；首次创建时执行 PRAGMA：
    - `busy_timeout=5000`
    - `journal_mode=WAL`
    - `synchronous=NORMAL`
    - `cache_size=-512`（512 KB，限制单连接页缓存）
    - `temp_store=FILE`
    - `foreign_keys=ON`
  - `transaction() -> contextmanager[sqlite3.Connection]`：用 `threading.RLock` 串行化，`BEGIN` 后 `commit`，异常 `rollback`。
  - `read_connection() -> contextmanager[sqlite3.Connection]`：只读查询，使用同一把锁；不提交。
  - `init_db() -> None`：执行 `db/schema.py` 的 DDL；用于 fresh DB、应用启动和测试。`db/database.py` 同时支持 `python -m db.database` 作为部署前的显式初始化命令。
  - `close() -> None`：测试/退出时关闭连接。
- `db/executor.py`（保留）：`ThreadPoolExecutor(max_workers=4)` 与 `run_db()` 不变；所有 repository 调用仍在工作线程执行，事件循环不直接做 SQLite I/O。
- `db/repository.py`（重写）：保持所有现有函数名、参数和 `UserDTO`/`MediaGroupDTO` 返回类型；新增 `UserStateDTO`。SQL 使用参数化 `?`，不拼接字符串。关键行为：
  - `get_or_create_user()`：`INSERT OR IGNORE` + `SELECT`，只返回业务用户字段，不承担验证码状态。
  - `get_user_by_user_id()`、`get_user_by_thread()`：返回 `UserDTO | None`；多行时取 `ORDER BY id DESC LIMIT 1`，不抛 `MultipleResultsFound`。
  - `get_user_state(user_id) -> UserStateDTO`：无记录时返回默认值（未验证、无 vcode），不创建 `user` 行。
  - `set_user_vcode(user_id, vcode)`、`mark_user_human(user_id)`、`set_user_captcha_error(user_id, timestamp)`：对 `user_state` 做 upsert。
  - `find_message_map_by_user_chat()`、`find_message_map_by_group_chat()`：`ORDER BY id DESC LIMIT 1`。
  - `create_message_map()`、`create_message_maps_batch()`、`set_user_thread_and_create_map()`：INSERT 显式带 `created_at=CURRENT_TIMESTAMP`（UTC，19 字符格式）；`delete_message_maps()` 空列表直接 no-op。
  - `count_old_message_maps()` / `delete_old_message_maps()`：cutoff 统一按 UTC naive 格式化为 `YYYY-MM-DD HH:MM:SS` 后与 `created_at` 比较。
  - `add_media_group_message()`：在同一个事务里先 `SELECT 1 ... LIMIT 1` 判断该 `(media_group_id, chat_id)` 是否已有行，再 `INSERT`，返回 `bool is_first`（原先由 `context.user_data["current_media_group_id"]` 承担的“首条”判断）。
  - `get_users_page(limit, offset)`：广播按页读取；`_broadcast` 必须逐页消费（默认页大小 `BROADCAST_PAGE_SIZE = 500`），每页发送完再取下一页，不得先把全部用户/协程物化到内存。保留 `get_all_users()` 但只作为小数据量便利函数，不得用于广播热路径。
- `db/model.py`：删除（不再有 ORM 模型）。
- `db/migrations/perf_indexes.py` 与 `001_perf_indexes.sql`：保留不动；新增 `db/migrations/user_state.py`：
  - `migrate(database_path) -> dict`：若 `user_state` 表不存在则 `CREATE TABLE IF NOT EXISTS`；不修改 `user` 表；`PRAGMA integrity_check`；返回 `{"ok": bool, "integrity": str, "created_table": bool}`；重复执行为空操作。

### 5.2 验证码状态与 PicklePersistence

- `interactive-bot/handlers/user.py`：
  - `check_human(update, context, user_state: UserStateDTO)`：从 `UserStateDTO` 读取 `is_human`、`is_human_error_time`；不再读写 `context.user_data`。
  - 生成验证码后调用 `run_db(repository.set_user_vcode, user.id, code)`。
  - `callback_query_vcode`：用 `run_db(repository.get_user_state, user.id)` 取 `vcode`，正确时 `mark_user_human`，错误时 `set_user_captcha_error`；不再依赖 `context.user_data`。
- `interactive-bot/__main__.py`：
  - 在模块加载处用 `db.database.init_db()` 替代 `Base.metadata.create_all(bind=engine)`（对应 `interactive-bot/__main__.py:21/51`），保证 fresh install 启动时先建表；同时保留 `python -m db.database` 供部署脚本显式调用。
  - 私聊 handler 保持现有顺序：先 `get_user_state(user.id)` 做验证码检查，通过后再 `get_or_create_user`；不提前创建 `user` 业务行，避免未验证用户进入广播名单。
  - 删除 `PicklePersistence` 的 import、`.persistence(...)`、`persistence_interval()` 及 `if __name__ == "__main__"` 里的实例化。
  - 删除 `ConversationHandler` import 以及未注册的 `error_in_send_media_group` 死函数（其函数体仍引用 `ConversationHandler.END`）。
  - 媒体组处理：`first = await run_db(repository.add_media_group_message, ...)`，仅当 `first` 为真时调用 `send_media_group_later`；a2u 同样处理，不再访问 `context.application.user_data`。
  - `last_message_time`：改用 `services/memory_cache.rate_limit_ok(user_id, message_interval)`；进程重启后频率限制重置，这是可接受的非持久状态。
- `scripts/import_pickle_state.py`（新增；生产升级**必做**步骤）：
  - 读取旧 `assets/<APP_NAME>.pickle` 的 `user_data`，把 `is_human`、`vcode`、`is_human_error_time` 写入 `user_state` 表（`ON CONFLICT(user_id) DO UPDATE`）；不创建/修改 `user` 业务行，因此 ~55 个仅存在于 pickle 的用户不会进入广播名单。
  - 脚本接受 `--pickle`、`--database`；默认在 pickle 不存在时返回非零并中止，`--allow-missing` 可显式跳过（fresh install 用）。

### 5.3 有界缓存与锁

新增 `interactive-bot/services/memory_cache.py`：

- `TTLCache(maxsize: int, ttl_seconds: float)`：`OrderedDict` 实现；`get(key)` 命中后移到队尾并清理过期项；`set(key, value)` 超上限先删过期再删最旧；提供 `__len__` 和 `clear()` 供测试。
- 模块级缓存：
  - `_avatar_cache = TTLCache(maxsize=2048, ttl_seconds=7*24*3600)`，键 `user_id`，值 `file_id`。
  - `_captcha_image_cache = TTLCache(maxsize=128, ttl_seconds=7*24*3600)`，键 `code`，值 `file_id`。
  - `_message_times = TTLCache(maxsize=4096, ttl_seconds=max(300, message_interval*2))`；`rate_limit_ok(user_id, interval)` 用 `time.monotonic()` 判断。
- `interactive-bot/handlers/user.py` 的 avatar/image 读写全部改走这两个缓存；不再写 `context.bot_data`。
- `interactive-bot/services/update_processor.py`：
  - `_user_locks` 从普通 dict 改为 `weakref.WeakValueDictionary[int, asyncio.Lock]`。
  - `user_lock(context, user_id)` 保持签名不变；锁对象在调用方 `async with` 期间有强引用，释放后由 GC 回收。
  - 增加 `lock_count() -> int` 仅用于测试/诊断。

### 5.4 依赖与文档

- `pyproject.toml`：删除 `sqlalchemy`、`pytz`；保留 `python-telegram-bot[job-queue]`、`python-dotenv`、`httpx`、`apscheduler`。
- `interactive-bot/utils.py`：`pytz.utc` 改为 `datetime.timezone.utc`。
- `uv.lock`：`uv lock` 重算；CI 的 `uv sync --frozen` 必须通过。
- `README.md` / `deploy/README.md`：
  - 数据层说明改为“stdlib sqlite3，无 ORM”；删除 `PERSISTENCE_INTERVAL` 说明，以及旧的 `python -c "import ... sqlalchemy, pytz ..."` 验证命令，替换为 `python -c "import interactive-bot.__main__, sys; assert 'sqlalchemy' not in sys.modules"`。
  - 部署步骤增加“停服 → 备份 → `python -m db.database`（初始化/校验表） → `python -m db.migrations.perf_indexes` → `python -m db.migrations.user_state` → `python scripts/import_pickle_state.py --pickle assets/<APP_NAME>.pickle --database assets/db.sqlite3` → 启动”。fresh install 可对导入脚本传 `--allow-missing`。
  - 注明回滚时使用上线前数据库备份；代码回滚可用上一 release。
- `docs/perf/README.md`：增加内存验收命令与基线文件说明。

### 5.5 内存观测与验收

新增 `scripts/memory_probe.py`：

- `--pid <pid>`：每 `--interval` 秒读取 `/proc/<pid>/status` 的 `VmRSS`、`VmHWM`、`VmData`、`Threads`，输出 JSON Lines；`--duration` 到点退出。
- `--self-check`：在当前进程输出一次快照，便于 CI 验证脚本可运行。
- 验收步骤：
  1. 上线前记录 5 分钟 idle 基线（RSS、VmData、Threads）。
  2. 上线后 30 分钟采样：RSS ≤55,000 KB。
  3. 连续 24 小时每 5 分钟采样：从 30 分钟值到最后值增长 ≤3,000 KB；无 OOM、无 NRestarts 增长。
  4. `python -c` 子进程导入 `interactive-bot.__main__`，断言 `sqlalchemy` 不在 `sys.modules`，且 `ru_maxrss` 比改造前 58 MB 低至少 8 MB。

## 6. 测试与验收清单

### 6.1 数据层

- `tests/test_sqlite_performance.py`（重写）：
  - `test_schema_has_expected_tables_and_indexes`：`init_db()` 后 `sqlite_master` 含 4 张业务表 + `user_state` 与 6 个性能索引。
  - `test_pragmas`：`connect()` 后 `PRAGMA journal_mode == "wal"`、`synchronous == 1`、`busy_timeout == 5000`、`cache_size == -512`。
  - `test_transaction_commits` / `test_transaction_rolls_back`：用 `db.database.transaction()` 直接插入/回滚。
  - `test_message_map_created_at_default`：`PRAGMA table_info` 含 `created_at`，未显式传入时由 `CURRENT_TIMESTAMP` 填充。
  - `test_user_state_migration_is_idempotent`：旧 schema 建 `user_state`、重复执行 `created_table` 为 false、行数不变。
- `tests/test_forwarding.py`：
  - 用 `db.database.set_trace_callback()` 收集 SQL；`test_forwarding_queries_user_once` 仍断言 SELECT user ≤1。
  - `test_new_user_first_message_commits_at_most_twice` 改为断言写语句 ≤2（用 trace 的 `INSERT`/`UPDATE` 计数）。
  - `_seed_user` 改走 `repository.get_or_create_user`。
  - 保留首条相册复用、回复串号两个回归测试。
- `tests/test_p2_improvements.py`：
  - 删除 `test_persistence_interval_reads_env`（函数已移除），新增 `test_pickle_persistence_is_gone`（`interactive-bot.__main__` 无 `PicklePersistence` 引用 / `inspect.getsource` 不含）。
  - `test_cleanup_*` 通过 repository/raw SQL 种数据。
- `tests/test_concurrency_and_broadcast.py`：
  - `_seed_user` 改 repository；`test_clear_*` 用 repository。
  - 新增 `test_user_locks_are_weak_and_evicted`：循环创建 2000 把锁，释放并 `gc.collect()` 后 `lock_count()` 显著下降。
  - `test_broadcast_*` 改为 monkeypatch `repository.get_users_page`，保留并发/重试断言。
- 新增 `tests/test_memory_architecture.py`：
  - `test_ttl_cache_maxsize_and_expiry`。
  - `test_captcha_state_survives_new_connection`：设置 `is_human/vcode`，`close()` 后重新 `connect()` 仍读到。
  - `test_media_group_first_flag`：同组第一条 True、第二条 False，删除后再次 True。
  - `test_app_import_does_not_import_sqlalchemy`：subprocess + `sys.modules` 断言。
- `tests/conftest.py` 与 `scripts/smoke_test.py`：用 `db.database.init_db()` 替代 `db.model.Base.metadata.create_all()`；`smoke_test` 期望表集合改为 4 张业务表 + `user_state`。

### 6.2 功能回归

- `uv run --frozen pytest -q`：现有 40 个测试 + 新增测试全部通过。
- `uv run --frozen python scripts/smoke_test.py`：创建 4 张业务表 + `user_state`，输出 `smoke ok`。
- `uv run --frozen python scripts/perf_probe.py --self-test` 与 `--real --iterations 100`：无异常；真实模式 mean/p50/p95/p99 与改造前基线对比不得退化超过 `scripts/perf_probe.compare()` 默认的 **10%**（与 CI gate 一致；如需放宽只能显式传 `--threshold` 并记录理由）。
- 生产切换前用 DB 副本执行 `python -m db.database`、两个迁移和导入脚本 + `PRAGMA integrity_check`，再启动。

## 7. 上线与回滚

1. 分支按任务提交，全部测试通过后开 PR；生产变更前再跑一次 `uv lock --check`、`pytest`、`smoke_test`。
2. 生产备份：数据库 `db-before.sqlite3`、运行资产（`.env`、assets）、当前代码 tar；记录 sha256。
3. 停服后按顺序执行：
   - `python -m db.database`（创建/校验基础表；fresh DB 必须先跑，已有 DB 幂等）
   - `python -m db.migrations.perf_indexes --database assets/db.sqlite3`
   - `python -m db.migrations.user_state --database assets/db.sqlite3`
   - `python scripts/import_pickle_state.py --pickle assets/<APP_NAME>.pickle --database assets/db.sqlite3`（生产必做；fresh install 用 `--allow-missing`）
   - 启动 `telegram-bot.service`
4. 验证：service active、NRestarts=0、`getMe` ok、管理群 `getChat` ok、日志无 ERROR/Traceback/Conflict；`memory_probe.py` 采样 30 分钟。
5. 回滚：停服 → 恢复 `db-before.sqlite3` 与上一版代码 → 启动；因新 schema 仅增加列，旧代码仍可读，但按保守流程恢复备份。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| sqlite3 单连接锁导致延迟上升 | 现有流量很低；`run_db` 仍在线程池执行；基准 p95 退化 >10% 时改为每线程连接或分片 |
| 手写 SQL 行为偏差 | 逐函数对照现有 repository；增加 CRUD/边界测试；生产 DB 副本迁移验证 |
| 移除 PicklePersistence 后验证码状态丢失 | `import_pickle_state.py` 一次性导入；迁移前备份；失败可回滚 |
| 弱引用锁在等待期间被回收 | `async with` 表达式持有强引用；并发测试覆盖 |
| 缓存 TTL 导致头像/验证码 file_id 重新获取 | 只是多一次 Telegram API 调用；TTL 7 天，容量上限 2048/128 |
| 内存目标未达成 | 先上线方案 B；若 30 分钟 RSS >55 MB 或 24 小时增长 >3 MB，记录数据后单独立项评估方案 C |
| fresh install 缺表 / 迁移顺序错误 | `interactive-bot.__main__` 启动时调用 `init_db()`；部署 runbook 先跑 `python -m db.database`；`tests/conftest.py` 与 `scripts/smoke_test.py` 同步改用 `init_db()` |
| `user_state` 迁移失败 | 迁移前备份；`CREATE TABLE IF NOT EXISTS` 幂等；失败即停服回滚 |

## 9. 留给实现者的决定

- `db/schema.py` 的 DDL 字符串具体换行与常量命名。
- `TTLCache` 内部使用 `OrderedDict` 还是 `dict` + 双向链表；以测试和内存为准。
- `memory_probe.py` 的 JSON 字段命名。
- 测试中 SQL trace hook 的具体实现（`sqlite3.Connection.set_trace_callback` 或 repository 包装器）。
- `db/migrations/user_state.py` 的 CLI 输出格式。
- 是否把 `get_all_users()` 保留为便利函数；若保留，不得在广播热路径使用。

## 10. 验收定义（Definition of Done）

- `sqlalchemy` 不在运行时依赖和 `sys.modules` 中。
- `PicklePersistence` 在生产启动路径不存在；验证码状态由 SQLite `user_state` 表提供，且 `user` 业务表行数不因 pickle 导入而增加。
- 所有按用户缓存/锁有明确上限，且测试能证明过期/回收。
- 40 个既有测试 + 新增测试全绿；CI `uv sync and smoke check` 成功。
- 生产 30 分钟 idle RSS ≤55 MB，24 小时增长 ≤3 MB，功能 smoke 全过。
- 文档、迁移命令、回滚步骤在 `deploy/README.md` 可执行。
