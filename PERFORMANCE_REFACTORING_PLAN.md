# 重构性能优化计划

| 项目 | 内容 |
| --- | --- |
| 项目 | Telegram-interactive-bot |
| 代码基线 | `master @ 1e1ed5d0194aa73882830c02e73c256ec4a85d7e` |
| 计划编制日期 | 2026-11-27 |
| 计划周期 | 2026-11-30 ～ 2027-01-08 |
| 适用环境 | `uk.zmal.top:7422` 生产单实例（1 vCPU / 929 MB，SQLite，systemd） |
| 文档状态 | 待评审 |

## 0. 执行摘要

当前机器人不是 CPU 或内存密集型服务：生产空闲时单核 CPU 约 `0.07%`，常驻内存约 `72.7 MB`，SQLite 数据库仅 `52 KB`、`236` 个用户、`6` 条消息映射。当前最值得优化的不是“算得更快”，而是：

1. **减少每条消息的 Telegram API 往返次数**：普通消息当前固定执行 `get_chat` + `send_copy` 两次 API 调用，`get_chat` 的 p50 实测约 `15.5 ms`，可直接用 `Bot.copy_message` 省掉。
2. **消除同步 SQLAlchemy 对事件循环的阻塞**：所有查询和提交都在 async handler 中同步执行，并且复用一个全局 `Session`；当前数据量小，单次约 `0.5 ms`，但会随并发和数据库增长放大，并存在跨请求事务互相干扰的风险。
3. **把串行更新处理改为“可回退的受控并发”**：`ApplicationBuilder` 未开启 `concurrent_updates`，一个慢的 Telegram API 调用会阻塞后续所有用户；这是吞吐量的上限，而不是 CPU。
4. **用索引、WAL 和批量提交支撑数据增长**：当前表只有主键和 `user_id` 索引，消息映射/媒体组/表单状态的热字段无二级索引；回访消息通常 1 次 `commit`，新用户首条最多 3 次，媒体组每项约 2 次（保存媒体项 + 发送后映射）。
5. **先建立基线再改代码**：项目目前没有 APM、结构化耗时日志或压测脚本，P95/P99、QPS、GC、事件循环延迟均为“待补充实测”，不能凭感觉宣称优化收益。

本计划不建议重写框架、引入 Redis/PostgreSQL 或更换 Telegram 库。所有优化都在现有 `python-telegram-bot 22.8`、`SQLAlchemy 2.1.9`、SQLite 和 systemd 部署边界内完成，按“低风险先行、数据库次之、并发最后”的顺序交付。

## 1. 优化对象与现状

### 1.1 系统边界与运行方式

| 项 | 现状 |
| --- | --- |
| 运行入口 | `uv run --frozen --no-dev python -m interactive-bot`（仓库部署单元写法）；生产当前 unit 实际为 `.venv/bin/python -m interactive-bot` |
| 运行时 | Python `3.11.18`（项目约束 `requires-python = ">=3.11"`） |
| 主要依赖 | `python-telegram-bot[job-queue] 22.8`、`SQLAlchemy 2.1.9`、`APScheduler 3.11.3`、`python-dotenv 1.2.4`、`httpx 0.28.1`、`pytz 2026.6` |
| 外部接口 | Telegram Bot API（long polling，无 HTTP 服务端接口） |
| 持久化 | 本地 SQLite `assets/db.sqlite3`；`PicklePersistence` 文件 `assets/{APP_NAME}.pickle`；`log.txt` |
| 部署 | systemd `telegram-bot.service`；内存上限 `MemoryMax=400M`、`MemorySwapMax=600M`；当前 `NRestarts=0` |
| 流量特征 | 低频客服型机器人；当前 `message_map=6` 行，5 分钟 journal 无日志，无法据此推算生产 QPS |

### 1.2 代码规模与复杂度

采集命令：`uvx radon raw -s interactive-bot db scripts`、`uvx radon cc -s -a interactive-bot db scripts`、`uvx radon mi -s interactive-bot db scripts`。

| 文件 | 角色 | LOC | SLOC | 主要接口/函数 | 最大圈复杂度 |
| --- | --- | ---: | ---: | --- | ---: |
| `interactive-bot/__main__.py` | 全部 Telegram handler、入口、调度 | 565 | 499 | `start`、`forwarding_message_u2a`、`forwarding_message_a2u`、`clear`、`broadcast`、`callback_query_vcode` | 16（C） |
| `interactive-bot/utils.py` | 延时删除/封禁 job | 48 | 37 | `delete_message_later`、`ban_user_later`、`remove_job_if_exists` | 3（A） |
| `interactive-bot/__init__.py` | 环境变量、日志初始化 | 35 | 28 | 配置常量、`logger` | — |
| `db/database.py` | SQLAlchemy engine/session | 9 | 6 | `engine`、`SessionMaker`、`Base` | — |
| `db/model.py` | ORM 模型 | 41 | 33 | 4 个模型类 | 1（A） |
| `scripts/smoke_test.py` | 启动/建表冒烟测试 | 40 | 23 | `main` | — |
| **业务代码小计** | 5 个文件 | **698** | **603** | — | — |
| **合计（含测试）** | 6 个文件 | **738** | **626** | 23 个函数/类/方法 | 平均 **3.70（A）** |

补充：

- 全仓库 126 个 tracked 文件，其中 100 张 `assets/imgs/*.png` 验证码图片；仓库大小约 3.1 MB（不含 `.git`、`.venv`）。
- `interactive-bot/__main__.py` 占业务代码 LOC 的约 **81%**，是典型的单文件“上帝模块”；其 Radon 可维护性指数为 `29.29`（A 级但偏低）。
- 复杂度最高的函数：
  - `forwarding_message_a2u`：`interactive-bot/__main__.py:353-459`，LOC 107，CC 16（C）
  - `forwarding_message_u2a`：`interactive-bot/__main__.py:246-349`，LOC 104，CC 15（C）
  - `check_human`：`interactive-bot/__main__.py:187-221`，LOC 35，CC 7（B）
  - `_send_media_group_later`：`interactive-bot/__main__.py:46-90`，LOC 45，CC 6（B）
- 静态检查基线：`uvx ruff check interactive-bot db scripts` 报告 46 项问题（10 个 `F401` 未使用导入、5 个 `F841` 未使用变量、6 个 `F541` 无占位符 f-string、5 个 `BLE001` 盲捕获等），其中 24 项可自动修复。该项不是性能指标，但会降低重构时的可读性和可测试性。

### 1.3 模块、文件与接口清单

| 入口/接口 | 位置 | 触发方式 | 当前职责 |
| --- | --- | --- | --- |
| `start` | `interactive-bot/__main__.py:162-183` | `/start`（私聊） | 欢迎语、读取管理群、引导发消息 |
| `forwarding_message_u2a` | `interactive-bot/__main__.py:246-349` | 用户私聊非命令消息 | 验证码、限频、建 topic、转发给管理群、消息映射 |
| `forwarding_message_a2u` | `interactive-bot/__main__.py:353-459` | 管理群非命令消息 | 找到用户、转发给用户、维护 topic 状态、消息映射 |
| `clear` | `interactive-bot/__main__.py:463-483` | `/clear`（管理群） | 删除 topic、按配置删用户消息 |
| `broadcast` / `_broadcast` | `interactive-bot/__main__.py:487-516` | `/broadcast`（管理群） | 遍历全部用户，复制一条消息 |
| `callback_query_vcode` | `interactive-bot/__main__.py:224-243` | 验证码回调按钮 | 校验验证码、封禁/放行 |
| `_send_media_group_later` / `send_media_group_later` | `interactive-bot/__main__.py:46-106` | 媒体组延时任务 | 聚合媒体组、复制到目标会话、写映射 |
| `update_user_db` | `interactive-bot/__main__.py:109-119` | 每条用户/管理员消息 | 首次见到用户时插入 `User` |
| `send_contact_card` | `interactive-bot/__main__.py:122-158` | 新 topic 创建后 | 拉取头像、发送联系人卡片 |
| `delete_message_later` / `ban_user_later` | `interactive-bot/utils.py:11-47` | 定时任务 | 延时删除/封禁 |
| `engine` / `SessionMaker` | `db/database.py:4-9` | 全局 | SQLite 连接池、ORM Session |
| `MediaGroupMesssage` / `FormnStatus` / `MessageMap` / `User` | `db/model.py:6-41` | ORM | 四张业务表 |

### 1.4 关键热路径与当前调用成本

| 热路径 | 当前调用序列（返回用户，普通文本消息） | 当前成本 |
| --- | --- | --- |
| `forwarding_message_u2a` | `update_user_db`：SELECT `User` → 再次 SELECT `User` → SELECT `FormnStatus` → `bot.get_chat` → `chat.send_copy` → INSERT `MessageMap` + COMMIT | 回访：3 次 DB 查询、2 次 Telegram API、1 次 commit；新用户首条额外写用户/topic，最多 3 次 commit |
| `forwarding_message_a2u` | `update_user_db` SELECT → SELECT `User` by `message_thread_id` → SELECT `FormnStatus` → `bot.get_chat` → `chat.send_copy` → INSERT `MessageMap` + COMMIT | 3 次 DB 往返、2 次 Telegram API |
| 新用户首条消息 | 上述路径 + `create_forum_topic` + `send_message` + `get_user_profile_photos` + `send_photo`/`send_contact` | 首次路径多 4 个左右 Telegram API，首响明显慢于普通路径 |
| 媒体组（N 条） | 每条消息 INSERT + COMMIT；5 秒后任务 SELECT 媒体组 → `get_chat` → `send_copies` → 每条 `MessageMap` INSERT + COMMIT | 每个媒体项约 2 次 commit（保存媒体项 + 发送后映射），N 条约 2N 次 |
| `/broadcast`（N 个用户） | 对每个用户 `get_chat` + `send_copy`，完全串行，异常仅计数 | 2N 次 Telegram API，无并发、无限流、无进度游标 |
| `/clear` | `delete_forum_topic` + 查询全部 `MessageMap` + 一次 `delete_messages` | `delete_messages` 未按 Bot API 上限分片；映射记录不清理 |

### 1.5 现状基线数据

采集时间：2026-11-27 09:35～09:50（Asia/Shanghai），环境 `uk.zmal.top:7422`，代码 `1e1ed5d`。

| 指标 | 现状值 | 数据来源/说明 |
| --- | --- | --- |
| 生产服务状态 | `active (running)`、`NRestarts=0`、`TasksCurrent=2` | `systemctl show telegram-bot` |
| 常驻内存 | `VmRSS=72,740 kB`（约 72.7 MB）；systemd `MemoryCurrent=74.7 MB`、`MemoryPeak=74.9 MB`、`VmSwap=0` | `/proc/<pid>/status`、`systemctl show` |
| 空闲 CPU | 30 秒内 `CPUUsageNSec` 增量 `21.53 ms`，约 **0.07% 单核** | `systemctl show -p CPUUsageNSec` 前后采样 |
| 累计 CPU | `30.10 s`（服务启动至采样时约 10 小时，含启动） | `systemctl show -p CPUUsageNSec` |
| CPU 热点函数 | **待补充：CPU profiling（py-spy/cProfile）结果；当前空闲无可识别热点** | 当前无 profiling 数据；Phase 0 采集 |
| 启动耗时 | `Started` 23:15:46 → `Application started` 23:15:49，约 **3.3 s** | `journalctl -u telegram-bot` |
| SQLite 文件 | `52 KB`（`page_count=13`、`page_size=4096`） | `PRAGMA page_count/page_size` |
| SQLite journal 模式 | `delete`（未启用 WAL） | `PRAGMA journal_mode` |
| 表行数 | `user=236`、`message_map=6`、`formn_status=0`、`media_group_message=0` | `SELECT COUNT(*)`（只读副本/只读连接） |
| 现有索引 | 每表主键 `ix_*_id` + `ix_user_user_id`（unique）；其余热字段无二级索引 | `PRAGMA index_list` |
| 单条查询延迟 | 按 `user_id` 索引查询 `0.543 ms/op`；按未索引 `message_thread_id` `0.539 ms/op`；`FormnStatus.message_thread_id` `0.461 ms/op`；`MessageMap.user_chat_message_id` `0.473 ms/op`（各 2000 次） | 生产 DB 副本微基准（SQLAlchemy 2.1.9） |
| 单次写入延迟 | `MessageMap` INSERT + COMMIT `0.778 ms/op`（200 次） | 同上；当前 SQLite `synchronous=FULL`、`journal_mode=delete` |
| 数据库慢查询 | 当前无慢查询日志；上述查询均 <1 ms；**待补充：慢查询阈值与 `EXPLAIN QUERY PLAN`** | 当前未开启慢查询日志；Phase 0 采集 |
| Telegram `getMe` | mean `22.6 ms`、p50 `18.1 ms`、p95 `22.9 ms`、max `65.4 ms`（10 次） | `httpx` 从 uk 直连 `api.telegram.org` |
| Telegram `getChat`（管理群） | mean `38.4 ms`、p50 `15.5 ms`、p95 `16.8 ms`、max `247.0 ms`（10 次，含一次冷请求） | 同上；说明 `get_chat` 是实打实的单次往返成本 |
| 日志文件 | `log.txt=38 KB`，无轮转；5 分钟 journal 0 行（空闲） | `ls -lh`、`journalctl --since "5 minutes ago"` |
| P95/P99 消息处理时延 | **待补充：P95/P99 handler 处理时延（需 Phase 0 埋点）** | 当前无 APM、无结构化耗时日志 |
| 生产 QPS/消息量 | **待补充：生产 QPS 与消息类型分布（需 Telegram update 计数或入口埋点）** | 当前只有 `message_map=6` 行，不能代表峰值 |
| GC 次数/停顿 | **待补充：GC 分代次数、P99 停顿（需 `gc.callbacks` + 压测）** | 当前无 profiling 数据 |
| 事件循环阻塞 | **待补充：事件循环 lag P99（需 `asyncio` debug/探针）** | 当前未采集 |
| 压测吞吐/错误率 | **待补充：mock Bot API 下 10 updates/s 的 P95、错误率（需 Phase 0 压测脚本）** | 当前无压测基线 |

### 1.6 已识别性能问题清单

| ID | 优先级 | 问题 | 证据 | 影响 |
| --- | --- | --- | --- | --- |
| P0-01 | P0 | async handler 内使用同步 SQLAlchemy，且全局复用一个 `Session` | `interactive-bot/__main__.py:42`、`db/database.py:6-7`；所有 `db.query/commit` 无 `await` | 阻塞事件循环；并发时多个任务共享同一 Session，可能出现事务串扰、对象状态冲突、提交顺序不可控 |
| P0-02 | P0 | 普通消息固定 2 次 Telegram API（`get_chat` + `send_copy`）；同一用户被重复查询 | `__main__.py:322-325`、`445-448`；`getChat` p50 实测 15.5 ms；`__main__.py:109-119` 与 `260` 对同一 `user_id` 查询 | 每条消息多一次网络往返；API 429/超时概率翻倍；广播时变成 2N 次调用；多余 SELECT |
| P0-03 | P0 | SQLite 未启用 WAL；热字段无二级索引；连接池 `pool_size=100, max_overflow=200` | `db/database.py:6`；`db/model.py`；`PRAGMA journal_mode=delete`；`PRAGMA index_list` 仅主键/`user_id` | 数据增长后全表扫描；读写互相阻塞；SQLite 不适合 100+ 连接，浪费 fd/内存 |
| P1-01 | P1 | 新用户首条最多 3 次 commit；媒体组每项约 2 次 commit | `__main__.py:327-333`、`449-455`、`70-90`；实测单次 commit 0.778 ms | 写放大、锁竞争、媒体组 N 条时 commit 次数线性增长 |
| P1-02 | P1 | 未开启 `concurrent_updates`，所有 update 串行处理 | `__main__.py:536-541`、`565` | 一个慢 API 阻塞所有用户；吞吐上限低；无法利用等待网络的时间 |
| P1-03 | P1 | 广播按用户串行 `get_chat` + `send_copy`，无并发/限流/重试 | `__main__.py:487-498` | 236 用户 = 472 次 API 调用；耗时长；易触发 429；失败无原因记录 |
| P1-04 | P1 | `/clear` 一次性把所有 `user_chat_message_id` 传给 `delete_messages` | `__main__.py:478-484` | Telegram Bot API 单次最多 100 条；超过后整批失败，且 `MessageMap` 不清理 |
| P2-01 | P2 | `logging.FileHandler("log.txt")` 同步写文件且无轮转 | `interactive-bot/__init__.py:9-13`；生产 `log.txt=38 KB` | 每条日志同步磁盘 I/O；长期无上限增长；systemd 已有 journal，重复写入 |
| P2-02 | P2 | `PicklePersistence` 默认 60 秒 flush；`check_human` 每次 `os.listdir` 扫描 100 张图 | `__main__.py:535`、`check_human` 内文件扫描 | 用户数据/状态增长后序列化变慢；验证码路径有可避免的磁盘扫描 |
| P2-03 | P2 | `__main__.py` 565 行、CC 15/16 的两个大 handler；无单元测试 | Radon/ruff 结果；`scripts/smoke_test.py` 仅建表冒烟 | 改动风险高、难以做细粒度性能回归；不是直接性能瓶颈，但会放大重构风险 |
| P2-04 | P2 | `MessageMap`/`MediaGroupMesssage` 无生命周期清理；`clear` 不删映射 | `db/model.py:24-29`；`clear` 只调用 Telegram 删除 | 表随消息量无限增长，最终拖慢查询和备份 |
| P2-05 | P2 | 所有主键 `id = Column(..., index=True)` 产生冗余索引 | `db/model.py:8,18,26,34`；`PRAGMA index_list` 可见 `ix_*_id` | 每次写入多维护一份索引；小表无感，大表写放大 |

## 2. 目标与验收标准

### 2.1 量化目标

> 原则：目标值先按当前实测的 DB/API 成本推导；无法从现有数据推导的基线明确标为“待补充实测”，Phase 0 完成后再冻结数值，不允许用未测数据宣称达标。

| ID | 指标 | 现状基线 | 目标（验收） | 采集方式 |
| --- | --- | --- | --- | --- |
| M-01 | `forwarding_message_u2a` 应用内处理耗时 P95（不含 Telegram 网络） | 待补充：当前 P95 未埋点 | ≤ **20 ms** | handler 内 `perf_counter` 分段 |
| M-02 | `forwarding_message_u2a` 端到端 P95（update 进入到 `copy_message` 返回） | 待补充：当前 P95 未埋点 | ≤ **250 ms** | 结构化日志 `update_id` 关联 |
| M-03 | 新用户首条消息端到端 P95（含建 topic + 名片） | 待补充：当前 P95 未埋点 | ≤ **1.5 s** | 结构化日志 |
| M-04 | mock Bot API 压测吞吐 | 待补充：当前无压测 | **≥ 10 updates/s**，持续 10 分钟，错误率 `< 0.1%` | `scripts/perf_probe.py` |
| M-05 | 10 倍数据量下 DB 查询 P95 | 当前 236 用户、查询 0.46～0.54 ms | ≤ **2 ms**（2,360 用户 / 10,000 条 `message_map`，按 10× 用户与更高消息量合成） | 基准脚本 + 测试库 |
| M-06 | commit 次数 | 新用户首条最多 3 次；回访 1 次；媒体组映射每项 1 次 | **新用户首条 ≤2 次；回访保持 1 次；媒体组映射 N→1 次** | 代码统计 + SQLAlchemy event 计数 |
| M-07 | 普通消息 Telegram API 调用次数 | 2 次（`get_chat` + `send_copy`） | **1 次（`copy_message`）** | mock Bot 方法调用计数 |
| M-08 | `/broadcast` 236 个用户耗时 | 待补充：当前未压测 | ≤ **15 s**，429 错误 `0`，成功/失败可核对 | 测试群 + mock/真实 Bot |
| M-09 | 空闲 RSS | 72.7 MB | ≤ **80 MB**（无回退） | systemd `/proc` 采样 |
| M-10 | 压测峰值 RSS | 待补充：当前未压测 | ≤ **300 MB**（低于 `MemoryMax=400M`） | systemd `MemoryPeak` |
| M-11 | 空闲 CPU | 0.07% 单核 | ≤ **1%** 单核 | `CPUUsageNSec` 30 秒采样 |
| M-12 | 10 updates/s 时 CPU | 待补充：当前未压测 | ≤ **40%** 单核 | 压测 + `CPUUsageNSec` |
| M-13 | 事件循环最大阻塞 | 待补充：当前未采集 | P99 `< 10 ms`，无 `> 100 ms` 的尖峰 | `asyncio` debug/探针 |
| M-14 | GC P99 停顿 | 待补充：当前未采集 | `< 20 ms`，且无频繁 full GC | `gc.callbacks` |
| M-15 | 生产错误率（handler 异常/失败回复） | 待补充：当前无统计 | `< 0.1%`（7 天窗口） | 结构化日志/`journalctl` 统计 |
| M-16 | 消息顺序违规 | 无并发，当前为 0 | **0 次**（同一用户消息顺序保持） | 压测脚本按 user 校验 `message_map` 顺序 |

### 2.2 约束条件

| 约束 | 要求 |
| --- | --- |
| Python 版本 | 保持 `requires-python >= 3.11`；生产验证版本 `3.11.18`，不在本计划中升级 Python 主/次版本 |
| 依赖边界 | 保持 `python-telegram-bot 22.x`、`SQLAlchemy 2.1.x`；新增 `aiosqlite`/`pytest-benchmark` 等必须写入 `pyproject.toml` 并更新 `uv.lock`，走 `uv sync --frozen` 验证 |
| 外部接口兼容 | Telegram 命令 `/start`、`/clear`、`/broadcast`、回调 `vcode_*`、消息转发/回复/topic 语义保持不变；不改变用户可见文案（除错误提示补充可观测信息外） |
| 数据库兼容 | 只做**增量索引/PRAGMA/可选新增时间列**；不删除/重命名现有列，不重建业务表；迁移脚本必须可重复执行（`IF NOT EXISTS`） |
| 单实例约束 | 不引入 Redis/PostgreSQL/消息队列；本期继续使用本地 SQLite 和 systemd 单实例 |
| 资源上限 | 生产 `MemoryMax=400M`；压测峰值目标 ≤300 MB；不得通过增加大量线程/连接绕过限制 |
| 发布窗口 | 生产变更窗口 `02:00–05:00 Asia/Shanghai`；单次停机 ≤5 分钟，回滚 ≤10 分钟 |
| 回归风险等级 | Phase 1 低；Phase 2 中；Phase 3 中高；Phase 3 的并发能力默认关闭，必须经压测和 72 小时观察后才允许全量 |
| 数据安全 | 任何 DDL/迁移前必须做 SQLite 一致性备份（含 `-wal`/`-shm` 处理），迁移后执行 `PRAGMA integrity_check` |

### 2.3 验收门禁

每个阶段合并前必须同时满足：

1. `uv sync --frozen --no-dev` 成功；`uv run python -m compileall interactive-bot db scripts` 无错误。
2. `uv run python scripts/smoke_test.py` 通过；新增单元测试全部通过。
3. `uv lock --check` 通过；CI 的 `uv sync and smoke check` 为绿色。
4. 对应阶段的基准/压测报告已附在 PR 描述中，包含“改动前/改动后”同环境数据。
5. 没有未解释的 P0/P1 问题；性能回退超过目标值 10% 必须阻塞合并。

## 3. 重构方案

### 3.1 优化项总表

| ID | 优先级 | 重构项 | 预期收益 | 复杂度 | 风险 | 主要代码路径/依赖 |
| --- | --- | --- | --- | --- | --- | --- |
| P0-00 | P0 | 基线观测与压测探针 | 让后续收益可量化；避免盲改 | S | 低 | 新增 `scripts/perf_probe.py`、日志字段；依赖无 |
| P0-01 | P0 | 数据访问层：按请求 Session + 线程池执行同步 ORM | 消除事件循环阻塞/跨请求 Session 串扰 | M | 中 | `db/database.py`、`__main__.py:42,109-119,260-266,353-455`、`utils.py` |
| P0-02 | P0 | 去掉 `get_chat`，改用 `Bot.copy_message/copy_messages`；合并重复用户查询 | 普通消息 2→1 次 API；3～4→2 次 DB；约省 15～20 ms p50 | S | 低 | `__main__.py:109-119,260,322-325,360,445-448` |
| P0-03 | P0 | SQLite 索引、WAL、busy_timeout、连接池收敛；删除主键冗余索引 | 10× 数据下查询 P95 ≤2 ms；读写不互相阻塞 | S | 低 | `db/model.py`、`db/database.py`、新增迁移脚本 |
| P1-01 | P1 | 事务合并/批量 `add_all`，同一短事务只 commit 一次，媒体组映射批量提交 | 新用户首条 3→2；回访保持 1；媒体组映射 N→1；减少锁竞争 | S～M | 低 | `__main__.py:70-90,327-333,449-455` |
| P1-02 | P1 | 受控并发：`concurrent_updates(8)` + 按用户串行锁 | 吞吐 ≥10 updates/s；消除跨用户头阻塞 | M～H | 中高 | `__main__.py:534-565`、新增 `services/update_processor.py` |
| P1-03 | P1 | 广播 `copy_message` + 信号量 + 令牌桶 + 429 重试 | 472→236 次 API；236 用户 ≤15 s | M | 中 | `__main__.py:487-498` |
| P1-04 | P1 | `/clear` 按 100 条分片删除，清理映射 | 大于 100 条不再整批失败 | S | 低 | `__main__.py:463-484` |
| P2-01 | P2 | 日志改 journal-only 或 `RotatingFileHandler`；`PicklePersistence` 显式 interval | 去掉同步文件 I/O 与无界日志 | S | 低 | `interactive-bot/__init__.py:9-13`、`__main__.py:535` |
| P2-02 | P2 | HTTP 连接池/超时、`allowed_updates`、头像 file_id 缓存、验证码图片列表缓存 | 降低连接建立/解析与重复 API 成本 | S | 低 | `__main__.py:536-565`、`check_human`、`send_contact_card` |
| P2-03 | P2 | 拆分 `__main__.py` 为 handlers/services/repository；补测试 | 降低 CC、提升可测性；便于后续 profiling | M | 低 | `interactive-bot/` 目录结构；依赖 P0/P1 |
| P2-04 | P2 | `MessageMap`/`MediaGroupMesssage` TTL/清理任务 | 控制表增长与备份体积 | M | 中 | `db/model.py`、`clear`、新增 job |
| P2-05 | P2 | 移除主键 `index=True` 冗余索引 | 减少写入索引维护 | S | 低 | `db/model.py:8,18,26,34` |

### 3.2 P0-00 基线观测与性能探针

**问题根因**：当前只有 Python 默认日志，无法回答“每条消息慢在 DB、Telegram API 还是事件循环”；没有基线就无法验收优化。

**重构手段**：

1. 新增 `scripts/perf_probe.py`：
   - 使用 `unittest.mock.AsyncMock` 替换 `Bot.get_chat`、`Bot.copy_message`、`Bot.send_message`、`Bot.get_user_profile_photos` 等外部调用；
   - 构造文本、回复、媒体组、新用户、`/clear`、`/broadcast` 六类合成 Update；
   - 每类跑 1,000 次，输出 `count/min/mean/p50/p95/p99/max`，失败即非 0 退出；
   - 支持 `--baseline` 写 JSON、`--compare baseline.json` 做回归门禁。
2. 在 handler 入口/出口加轻量 `perf_counter` 计时，只有 `PERF_LOG=1` 时输出 JSON 到 stdout（journal 收集），字段：`event`、`handler`、`update_id`、`user_id`、`duration_ms`、`db_ms`、`telegram_ms`、`ok`；默认关闭，生产采样率可设 `PERF_SAMPLE_RATE=0.1`。
3. 增加 `event_loop_lag` 探针：在 `Application` 启动后创建 1 秒周期的 `asyncio` 任务，记录实际唤醒延迟；`PERF_LOG=1` 时输出 p95。
4. 在测试环境用 `gc.callbacks` 记录 gen0/gen1/gen2 次数和停顿，写入压测报告。

**预期收益**：形成可复现的基线；后续每项改动都有同环境前后对比，避免“优化了但不知道有没有效果”。

**复杂度/风险**：S / 低。探针默认关闭，不影响生产；日志量通过采样控制。

**代码路径/依赖/影响面**：新增 `scripts/perf_probe.py`、`interactive-bot/observability.py`；修改 `__main__.py` 的 `main` 入口与 handler 边界；不改变业务逻辑。

### 3.3 P0-01 数据访问层：按请求 Session + 线程池执行

**问题根因**：

- `db = SessionMaker()` 在模块级创建并被所有 handler 共享（`interactive-bot/__main__.py:42`）。SQLAlchemy `Session` 不是线程安全/任务安全的；`forwarding_message_u2a` 在 `await create_forum_topic` / `await send_contact_card` 期间一直持有同一个 Session，其他 update 可能在同一 Session 上查询/提交。
- 所有 `db.query()`、`db.commit()` 都是同步调用，直接跑在 asyncio 事件循环线程上；单次 0.5～0.8 ms 虽小，但每次消息多次叠加，且 SQLite 锁等待时会直接阻塞整个 bot。

**重构手段**：

1. 在 `db/database.py` 增加 `session_scope()` context manager：
   ```python
   @contextmanager
   def session_scope():
       session = SessionMaker()
       try:
           yield session
           session.commit()
       except Exception:
           session.rollback()
           raise
       finally:
           session.close()
   ```
2. 将每个 handler 的 DB 操作收拢到 `db/repository.py` 的同步函数（如 `get_or_create_user`、`get_user_by_thread`、`create_message_map`），handler 通过 `await asyncio.to_thread(repo_fn, ...)` 调用；**返回 DTO/dataclass，不跨线程返回 ORM 实例**，避免 detached/lazy load 问题。
3. 设置专用 `ThreadPoolExecutor(max_workers=4)`（1 vCPU + SQLite 写串行，4 个足够；不要用默认 32 线程）；所有 DB 调用走该 executor。
4. 明确“短事务”边界：**不要在网络 `await` 期间持有写事务**；先完成 Telegram API 调用，再用一个短事务写入映射；新用户插入/更新与 topic 创建分离。
5. 如果后续压测显示 SQLite 写竞争仍是瓶颈，再评估 `sqlalchemy.ext.asyncio + aiosqlite`；本期不做，避免大范围改写。

**预期收益**：

- 事件循环不再被 DB I/O 阻塞；M-13 目标 P99 <10 ms；
- 消除全局 Session 的事务串扰，降低“消息映射错乱/提交覆盖”的隐性风险；
- 单次 DB 调用多约 0.1～0.3 ms 线程调度开销，但换取并发下的稳定性，符合目标。

**复杂度/风险**：M / 中。风险点是 Session 生命周期、ORM 对象跨线程、线程池与 SQLite 连接数；用 repository + DTO + 单测覆盖。

**代码路径/依赖/影响面**：`db/database.py:1-9`、`db/model.py`、`interactive-bot/__main__.py:42,109-119,260-266,353-455`、`interactive-bot/utils.py:11-47`、`_send_media_group_later`；依赖 P0-00 基线。

### 3.4 P0-02 去掉冗余 `get_chat`，合并重复查询

**问题根因**：

- `chat = await context.bot.get_chat(chat_id)` 只是为了拿 `Chat` 对象调用 `Chat.send_copy`；PTB 的 `Bot.copy_message(chat_id=..., from_chat_id=..., message_id=..., message_thread_id=...)` 可直接完成，无需先 `get_chat`。
- `update_user_db(user)` 已查询 `User`，调用方马上又 `db.query(User).filter(...).first()`，同一条记录查两次。
- 媒体组路径同样先 `get_chat(target_id)` 再 `chat.send_copies(...)`；`Bot.copy_messages` 可直接按 `chat_id` 调用。

**重构手段**：

1. `update_user_db` 改为 `get_or_create_user(session, user) -> User`，返回已有或新建对象；删除 `__main__.py:260` 的第二次查询。
2. 普通用户→管理群：`await context.bot.copy_message(chat_id=admin_group_id, from_chat_id=user.id, message_id=update.message.id, message_thread_id=message_thread_id, reply_parameters=...)`。
3. 管理群→用户：同样用 `copy_message`，`chat_id=user_id`。
4. 媒体组：`Bot.copy_messages(chat_id=target, from_chat_id=..., message_ids=[...], message_thread_id=...)`，去掉 `get_chat`。
5. 广播：`Bot.copy_message` 直接发，不再 `get_chat`。
6. 保留 `Chat.send_copy` 仅用于确实需要 `Chat` 对象的场景；本仓库没有。

**预期收益**：

- 普通消息 Telegram API 调用 2→1；`getChat` p50 15.5 ms、mean 38.4 ms，预计每条消息省 15～20 ms（p50 口径）；
- 广播 236 用户从 472 次降为 236 次；
- 减少 429/超时概率与 API 限流压力。

**复杂度/风险**：S / 低。需确认 `copy_message` 对 `message_thread_id`、`reply_parameters`、媒体组 `copy_messages` 的语义与 `Chat.send_copy` 一致；用合成 Update 单测 + 测试群回归。

**代码路径/依赖/影响面**：`interactive-bot/__main__.py:109-119,260,322-325,360,445-448,487-498`；依赖 P0-00。

### 3.5 P0-03 SQLite 索引、WAL、连接池

**问题根因**：

- `PRAGMA journal_mode=delete`：写事务会阻塞读，读也会阻塞写；单文件 SQLite 在消息写入时可能让查询等待。
- 现有索引只有每表主键和 `User.user_id` unique；热查询字段没有索引：
  - `MessageMap.user_chat_message_id`（用户回复映射）
  - `MessageMap.group_chat_message_id`（管理群回复映射）
  - `MessageMap.user_id`（`/clear` 批量查询）
  - `FormnStatus.message_thread_id`（每条消息检查 topic 状态）
  - `MediaGroupMesssage(media_group_id, chat_id)`（媒体组聚合）
  - `User.message_thread_id`（管理群消息找用户）
- `create_engine(..., pool_size=100, max_overflow=200)` 对 SQLite 无意义：SQLite 是文件级写锁，100 个连接只会增加 fd/内存和锁竞争；默认 `timeout=5s` 也可能过早抛 `database is locked`。
- 所有主键 `id` 同时 `primary_key=True, index=True`，产生冗余索引（`ix_*_id`），增加写放大。

**重构手段**：

1. `db/model.py` 增加 `Index`：
   - `Index("ix_message_map_user_chat", MessageMap.user_chat_message_id)`
   - `Index("ix_message_map_group_chat", MessageMap.group_chat_message_id)`
   - `Index("ix_message_map_user", MessageMap.user_id)`
   - `Index("ix_formn_status_thread", FormnStatus.message_thread_id)`
   - `Index("ix_media_group_lookup", MediaGroupMesssage.media_group_id, MediaGroupMesssage.chat_id)`
   - `Index("ix_user_thread", User.message_thread_id)`
2. 移除所有主键上的 `index=True`（主键本身已有 B-tree）。
3. `db/database.py`：
   ```python
   engine = create_engine(
       SQLALCHEMY_DATABASE_URL,
       pool_size=5,
       max_overflow=5,
       pool_timeout=30,
       connect_args={"timeout": 30},
   )
   ```
   并通过 `event.listens_for(engine, "connect")` 设置：
   ```sql
   PRAGMA journal_mode=WAL;
   PRAGMA synchronous=NORMAL;
   PRAGMA busy_timeout=5000;
   PRAGMA foreign_keys=ON;
   ```
4. 新增 `db/migrations/001_perf_indexes.sql`（或 `scripts/migrate_perf_indexes.py`），用 `CREATE INDEX IF NOT EXISTS` 对现有库补索引，并用 `DROP INDEX IF EXISTS` 删除冗余的 `ix_user_id`、`ix_message_map_id`、`ix_formn_status_id`、`ix_media_group_message_id`；在发布步骤中执行，不依赖 `create_all`（`create_all` 不会给已有表补索引）。
5. 迁移前执行 `PRAGMA wal_checkpoint(TRUNCATE)`，备份 `db.sqlite3` 及 `-wal`/`-shm`；迁移后 `PRAGMA integrity_check`。

**预期收益**：

- 当前 236 行下查询已在 0.5 ms 左右，索引收益不明显；目标是在 10 倍数据量下仍 ≤2 ms，避免全表扫描；
- WAL 降低读写互相阻塞；`busy_timeout` 减少瞬时锁错误；
- 连接池从 300 降到 10，减少资源占用；移除冗余主键索引降低写放大。

**复杂度/风险**：S / 低。索引会增加少量写成本和磁盘；WAL 会新增 `-wal`/`-shm` 文件，备份/恢复流程必须包含；需在低峰窗口执行。

**代码路径/依赖/影响面**：`db/model.py`、`db/database.py`、新增迁移脚本；影响所有查询/写入路径；依赖 P0-00 基线；可与 P0-01 并行开发，但迁移应在低峰窗口单独执行。

### 3.6 P1-01 事务合并与批量写入

**问题根因**：

- `forwarding_message_u2a` 在 `db.add(msg_map); db.commit()` 前已经做过用户/topic 写入；新用户首条最多 3 次 commit（用户插入、topic 更新、消息映射），回访用户 1 次；
- `_send_media_group_later` 对每个 `sent` 循环 `db.add(msg_map); db.commit()`（`__main__.py:70-90`），N 条媒体消息 N 次映射 commit；此前每个媒体项还各有一次保存 commit，合计约 2N 次；
- 实测单次 `MessageMap INSERT + COMMIT` 0.778 ms；新用户首条 3 次约 2.3 ms，媒体组 10 条约 15.6 ms 纯 commit（不含保存媒体项的 commit），还不含锁等待。

**重构手段**：

1. 每个 handler 用一个 `session_scope()`，同一 handler 内可合并的 INSERT/UPDATE 只 `flush()`，结束前一次 `commit()`；不要在 `await` 网络调用期间持有写事务。
2. 媒体组：`session.add_all([...])` 后一次 `commit()`；`MessageMap` 批量插入，把 N 次映射 commit 降为 1 次。
3. 用户 + 消息映射在可合并的短事务内提交（先 `get_or_create_user`，再 `add(msg_map)`）；新用户首条从 3 次 commit 降到 2 次，回访用户保持 1 次。
4. 增加 SQLAlchemy `before_cursor_execute`/`commit` 计数器（仅测试环境），断言回访消息 commit=1、新用户首条 ≤2、媒体组映射批量 commit=1。

**预期收益**：新用户首条 commit 3→2；回访用户保持 1；媒体组映射 N→1；按实测 0.778 ms/commit，新用户首条节省约 0.8 ms，媒体组线性收益更明显；降低 SQLite 写锁竞争。

**复杂度/风险**：S～M / 低。主要风险是事务边界变大导致锁持有时间变长；通过“网络调用后再开短事务”控制。

**代码路径/依赖/影响面**：`interactive-bot/__main__.py:70-90,327-333,449-455`、`db/repository.py`；依赖 P0-01/P0-03。

### 3.7 P1-02 受控并发：`concurrent_updates` + 按用户串行

**问题根因**：

- `ApplicationBuilder()` 未调用 `concurrent_updates()`，PTB 默认逐条处理 update；一个 `copy_message`/`create_forum_topic` 的 15～250 ms 网络等待会阻塞其他所有用户。
- 但直接开启全局并发会让同一用户的多条消息乱序，并可能让 `context.user_data`、`message_map`、topic 状态出现竞态；必须先做 P0-01 的 Session 隔离。

**重构手段**：

1. 在 `ApplicationBuilder` 中增加：
   ```python
   limit = int(os.getenv("PERF_CONCURRENT_UPDATES", "0"))
   builder = ApplicationBuilder().token(bot_token)
   if limit > 0:
       builder = builder.concurrent_updates(limit)
   application = builder.build()
   ```
   默认 `0`（不调用 `concurrent_updates`，保持现状）；测试通过后按 `2 → 4 → 8` 逐级开启。
2. 为同一用户加串行锁：`context.application.bot_data` 中维护 `defaultdict(asyncio.Lock)`，以 `effective_user.id` 为 key；`forwarding_message_u2a`/`forwarding_message_a2u` 的入口包在 `async with user_lock:` 内，保证同一用户消息顺序。
3. 对 `context.user_data` 的读写只在锁内进行；避免跨用户共享可变状态。
4. 压测需要验证：10 updates/s 持续 10 分钟、同一用户 100 条消息顺序、不同用户并发无交叉污染。
5. 将并发开关纳入环境变量和 systemd `EnvironmentFile`，便于不回滚代码直接降级。

**预期收益**：目标吞吐 ≥10 updates/s（mock Bot API），P95 ≤250 ms；消除一个慢用户阻塞全部用户的问题。实际生产 QPS 受 Telegram 限流约束，待 Phase 0 基线确认。

**复杂度/风险**：M～H / 中高。最大风险是消息顺序和共享状态；必须默认关闭、逐级放量、每级观察 24 小时；出现顺序违规立即设 `PERF_CONCURRENT_UPDATES=0` 并重启。

**代码路径/依赖/影响面**：`interactive-bot/__main__.py:534-565`、新增 `services/update_processor.py`、`context.user_data` 读写路径；依赖 P0-01、P1-01。

### 3.8 P1-03 广播并发、限流与重试

**问题根因**：

- `_broadcast` 对每个用户执行 `get_chat` + `send_copy`，完全串行；236 用户等于 472 次 API；无并发、无 20～30 msg/s 的全局限流、无 `RetryAfter` 处理、失败只加计数不记录原因。
- `failed` 变量计算后未使用，无法知道哪些用户失败。

**重构手段**：

1. 使用 `Bot.copy_message` 直接按 `chat_id` 发送，去掉 `get_chat`（236 次调用）。
2. 使用 `asyncio.Semaphore(5)` 控制同时在途请求；令牌桶限制全局 `20 msg/s`（低于 Telegram 约 30 msg/s 上限，留余量）。
3. 捕获 `telegram.error.RetryAfter`，按 `retry_after` 睡眠后重试（最多 2 次）；记录失败 `user_id`/`error`，写入 job 结果。
4. 分批查询用户（如 500 条/批），避免一次性加载全部用户；支持从 `last_user_id` 游标恢复。
5. 广播结束后向管理群发送汇总：`success`、`failed`、`duration_s`、`rate_limit_count`。

**预期收益**：236 用户广播从 472 次 API 降到 236 次；目标 ≤15 s、429 错误 0；失败可追踪、可重试。

**复杂度/风险**：M / 中。风险是 Telegram 限流、并发过高导致 429/封禁；通过 20 msg/s、semaphore 5、RetryAfter 退避、测试群先验控制。

**代码路径/依赖/影响面**：`interactive-bot/__main__.py:487-498`、`broadcast` 入口 501-516；依赖 P0-02（copy_message）。

### 3.9 P1-04 `/clear` 分片删除

**问题根因**：

- `delete_messages` 一次传入用户全部 `user_chat_message_id`；Telegram Bot API 单次最多 100 条，超过会整批 `BadRequest`，导致清理失败。
- `clear` 只删 Telegram 消息，不删除 `MessageMap` 行，映射表持续增长（P2-04）。

**重构手段**：

1. 将消息 ID 列表按 100 条分片；逐片 `await context.bot.delete_messages(chat_id, chunk)`；对 `RetryAfter` 做退避。
2. 删除成功后按同一批 ID 删除 `MessageMap` 记录（或标记 `deleted_at`，二选一；优先物理删除，保持表小）。
3. 增加 `--dry-run` 测试路径；用 250 条合成映射做单测，断言 3 次调用、无异常。

**预期收益**：消除 >100 条时的整批失败；控制映射表增长；单次 clear 的 Telegram API 调用从 1 次变 `ceil(N/100)` 次（可接受，换取正确性）。

**复杂度/风险**：S / 低。删除是不可逆操作，必须在测试群先验证；生产保留备份。

**代码路径/依赖/影响面**：`interactive-bot/__main__.py:463-484`、`db/model.py`；依赖 P0-01。

### 3.10 P2-01 日志与持久化 I/O

**问题根因**：

- `logging.basicConfig(handlers=[StreamHandler(), FileHandler("log.txt")])` 每条日志同步写文件；systemd 已把 stdout 收进 journal，文件是重复写入且无轮转（生产 `log.txt=38 KB`，长期会增长）。
- `PicklePersistence` 默认 `update_interval=60s`，每次 flush 序列化全部 `user_data`/`chat_data`/`bot_data`；当前 19 KB 很小，但用户状态增长后是 O(n) I/O。

**重构手段**：

1. 默认只保留 `StreamHandler`（journal）；如必须保留文件，改为 `RotatingFileHandler(maxBytes=10*1024*1024, backupCount=5)`，并在 README 说明 journal 是首选。
2. 将 `telegram.ext` 日志级别设为 `INFO`（启动/错误），业务日志按需 `DEBUG`；避免每条 update 的冗余日志。
3. `PicklePersistence(filepath=..., update_interval=300)` 显式设置 5 分钟；压测观察 flush 耗时；如果 `user_data` 持续增长，再评估拆分/清理。
4. `check_human` 的 `os.listdir("./assets/imgs")` 改为模块加载时缓存 tuple（100 张图），避免每次验证码扫描目录。

**预期收益**：去掉每行日志的同步文件 I/O；日志有上限；持久化 flush 频率降低；验证码路径少一次目录扫描。

**复杂度/风险**：S / 低。风险是日志落盘策略变化影响排障；保留 journal + 轮转文件双路径可选。

**代码路径/依赖/影响面**：`interactive-bot/__init__.py:9-13`、`__main__.py:535`、`check_human` 187-221。

### 3.11 P2-02 HTTP 连接池、超时与缓存

**问题根因**：

- PTB 默认 HTTPX 连接池和超时未针对本机器人调优；网络抖动时可能出现连接重建/默认超时等待。
- `run_polling()` 未限制 `allowed_updates`，Telegram 会推送所有类型，PTB 需要解析/过滤本 bot 不处理的 update。
- `send_contact_card` 每次新 topic 都 `get_user_profile_photos` 并上传头像；同一用户短期内重复建 topic 会重复成本（低频，收益有限）。
- `check_human` 每次扫描 `assets/imgs`。

**重构手段**：

1. `ApplicationBuilder` 显式设置：
   ```python
   .connection_pool_size(8)
   .pool_timeout(5)
   .connect_timeout(5)
   .read_timeout(10)
   .write_timeout(20)
   ```
2. `application.run_polling(allowed_updates=["message", "callback_query"])`（先确认没有其他 handler 依赖 `edited_message`/`chat_member`）。
3. `send_contact_card` 按 `user.id` 缓存 `file_id`（TTL 24 小时，存 `bot_data`），命中时直接 `send_photo(file_id)`；未命中再 `get_user_profile_photos`。
4. 验证码图片列表在模块加载时缓存；如运营会热更新图片，提供显式 reload。

**预期收益**：降低连接建立/解析开销，减少网络抖动导致的超时；头像/验证码路径减少重复 API/磁盘操作。当前流量低，收益以稳定性为主。

**复杂度/风险**：S / 低。`allowed_updates` 如漏配会导致功能静默丢失，必须逐 handler 核对并用测试群验证。

**代码路径/依赖/影响面**：`interactive-bot/__main__.py:534-565`、`send_contact_card`、`check_human`；依赖 P0-00。

### 3.12 P2-03 拆分 `__main__.py` 与测试基线

**问题根因**：

- 565 行单文件，`forwarding_message_a2u`/`u2a` 的 CC 为 16/15；业务逻辑、Telegram API、DB 访问、格式化全部混在一起，无法单独压测或复用。
- 当前只有 `scripts/smoke_test.py` 建表冒烟，没有 handler 单测和基准测试，任何重构都缺少回归保护。

**重构手段**：

1. 目录建议：
   ```
   interactive-bot/
     __main__.py            # 仅 Application 组装、handler 注册、run_polling
     handlers/user.py       # start、forwarding_message_u2a、check_human
     handlers/admin.py      # forwarding_message_a2u、clear、broadcast
     handlers/media.py      # media group 相关
     services/forwarding.py # copy_message/copy_messages 封装、reply 参数构造
     services/broadcast.py  # 广播并发/限流/重试
     observability.py       # perf 计时/JSON 日志
   db/repository.py         # 同步 repository 函数
   ```
2. 每个 handler 只做“解析 Update → 调 service → 回复”，把条件分支抽成小函数；目标单函数 CC ≤10、`__main__.py` ≤150 行。
3. 增加 `pytest`/`pytest-asyncio`/`pytest-benchmark` 到 `[dependency-groups] dev`，补：
   - 文本/回复/媒体组/新用户/`/clear`/`/broadcast` 六类 handler 单测；
   - `scripts/perf_probe.py` 基准；
   - 迁移脚本测试（临时 SQLite）。
4. 启用 ruff 的 `E,F,I,B` 规则并修复 46 项基线；格式化 `ruff format`。

**预期收益**：不直接改变运行时性能，但把 CC 15/16 的路径拆成可测单元，降低后续优化回归风险；使 P0/P1 的收益可稳定验证。

**复杂度/风险**：M / 低。风险是 import cycle 和入口路径变化；用 `python -m interactive-bot` 冒烟 + CI 保护。

**代码路径/依赖/影响面**：`interactive-bot/__main__.py:1-565` 全文件；依赖 P0/P1 完成后进行，避免边拆边改行为。

### 3.13 P2-04 数据生命周期清理

**问题根因**：

- `MessageMap` 每转发一条消息新增一行，`/clear` 不删除，永久增长；
- `MediaGroupMesssage` 在媒体组发送后不删除，永久增长；
- 当前数据量极小（6 行/0 行），但这是“未来慢查询/备份膨胀”的确定来源。

**重构手段**：

1. `MessageMap` 增加可空 `created_at`（SQLite 不支持 `ADD COLUMN ... DEFAULT CURRENT_TIMESTAMP` 这类非常量默认；迁移时分批回填）；新增每日低峰 job：删除 `created_at < now - 90 days` 的映射；保留策略可配置 `MESSAGE_MAP_RETENTION_DAYS=90`。
2. `_send_media_group_later` 成功发送后删除对应 `MediaGroupMesssage` 行（或打 `sent_at` 标记，7 天后清理）。
3. `/clear` 成功删除用户消息后同步删除该用户 `MessageMap` 行（见 P1-04）。
4. 清理任务本身分页执行（每批 500 行），避免长事务锁库。

**预期收益**：表大小与消息量解耦；查询/备份保持稳定；不改变用户可见功能。

**复杂度/风险**：M / 中。删除历史映射可能导致“引用旧消息回复”无法映射；保留 90 天并记录策略，产品需确认；先 dry-run 统计。

**代码路径/依赖/影响面**：`db/model.py`、`interactive-bot/__main__.py:70-90,463-484`、新增 cleanup job；依赖 P1-04。

## 4. 实施步骤与排期

### 4.1 阶段计划

| 阶段 | 日期（2026～2027） | 任务 | 负责人 | 前置依赖 | 里程碑产出 |
| --- | --- | --- | --- | --- | --- |
| Phase 0 基线 | 11-30 ～ 12-04 | P0-00：性能探针、结构化日志、压测脚本；采集 P95/QPS/GC/事件循环基线 | 待指定：后端负责人 | 无 | `scripts/perf_probe.py`、`docs/perf/baseline-2026-11-27.md`、基线 JSON |
| Phase 1 低风险 | 12-07 ～ 12-11 | P0-02 去 `get_chat`/合并查询；P0-03 索引/WAL/连接池；P2-01 日志；P2-05 冗余索引 | 待指定：后端负责人 | Phase 0 | PR-1、迁移脚本、24 小时 staging/生产观察 |
| Phase 2 数据层 | 12-14 ～ 12-18 | P0-01 按请求 Session + 线程池；P1-01 事务合并/批量写入 | 待指定：后端负责人 | Phase 1 | PR-2、DB 迁移验证、10× 数据基准报告 |
| Phase 3 并发 | 12-21 ～ 12-25 | P1-02 受控并发；P1-03 广播；P1-04 clear 分片 | 待指定：后端 + 运维 | Phase 2 | PR-3、10 updates/s 压测报告、72 小时 canary |
| Phase 4 收口 | 12-28 ～ 12-31 | P2-02 HTTP/缓存；P2-03 拆分模块；P2-04 数据清理；文档/回滚演练 | 待指定：后端 + 测试 | Phase 3 | RC 分支、完整回归报告、回滚演练记录 |
| Phase 5 放量 | 01-04 ～ 01-08 | 生产全量、7 天稳定观察、性能复盘 | 待指定：运维/负责人 | Phase 4 | 上线报告、指标对比、遗留项清单 |

> 时间为计划排期，不含法定节假日；负责人未指定处保留“待指定：后端负责人/运维负责人/测试负责人”。如 Phase 0 基线显示 P95 已远低于目标，可缩减 Phase 1/2 范围，但不得跳过观测和备份。

### 4.2 每阶段可独立交付物

- **Phase 0**：`scripts/perf_probe.py` 可独立运行；`PERF_LOG=1` 的 JSON 日志可在 `journalctl` 检索；基线报告含 6 类场景。
- **Phase 1**：普通消息 API 调用 2→1；`PRAGMA journal_mode=wal` 与索引在测试库生效；无业务行为变化；可单独回滚代码与 `DROP INDEX`。
- **Phase 2**：Repository 层和 Session 生命周期可单测；回访消息 commit=1、新用户首条 ≤2、媒体组映射批量 commit=1；10× 数据基准通过。
- **Phase 3**：并发开关默认 0；压测脚本可重复；广播/清理有独立测试；可通过环境变量降级。
- **Phase 4**：模块拆分后 `python -m interactive-bot` 启动正常；清理 job 有 dry-run；回滚手册经过演练。
- **Phase 5**：生产 7 天无 P0/P1 事故；指标达到 M-01～M-16 中适用项；遗留项有 owner/日期。

### 4.3 灰度与分批上线策略

1. **代码先行、开关默认关闭**：Phase 3 的 `PERF_CONCURRENT_UPDATES` 默认 `0`；`PERF_LOG` 默认 `0`；上线后先确认启动/冒烟，再逐级打开。
2. **测试用户/管理群 canary**：用 `PERF_CANARY_USER_IDS` 指定 2 个管理员账号先走新转发路径，观察 24 小时；普通用户仍走旧路径（代码分支或开关）。
3. **数据库迁移单独窗口**：停服务 → `wal_checkpoint(TRUNCATE)` → 备份 → 执行 `001_perf_indexes.sql` → `PRAGMA integrity_check` → 启动；不与代码大改同一天做。
4. **并发逐级放量**：`0 → 2 → 4 → 8`，每级至少 24 小时；观察 P95、错误率、顺序违规、内存/CPU；任何一项超阈值立即回退上一级。
5. **广播只在测试群演练**：先用 5 个测试账号 → 20 个 → 全量 236；每次记录 429 和耗时。
6. **全量条件**：连续 7 天 M-15 错误率 <0.1%、M-13 事件循环 P99 <10 ms、M-09 RSS ≤80 MB、无重启。

### 4.4 回滚方案

| 变更 | 回滚动作 | 目标时间 |
| --- | --- | --- |
| 代码（P0-02/P1-01/P1-03 等） | `git checkout <last-good-sha>`；`uv sync --frozen --no-dev`；`systemctl restart telegram-bot` | ≤5 min |
| 并发开关 P1-02 | `PERF_CONCURRENT_UPDATES=0` 写入 `EnvironmentFile`；`systemctl restart` | ≤1 min |
| 日志/缓存 P2 | 恢复旧 `EnvironmentFile`/代码；重启 | ≤5 min |
| 索引迁移 | `DROP INDEX IF EXISTS ...`（仅删新增索引，不动表数据） | ≤5 min |
| WAL 模式 | 停止服务 → `PRAGMA wal_checkpoint(TRUNCATE)` → `PRAGMA journal_mode=DELETE` → 启动 | ≤10 min |
| 数据异常 | 停止服务 → 保留 `db.sqlite3`、`-wal`、`-shm` → 从 `/root/backups/` 恢复最近一致性备份 → `PRAGMA integrity_check` → 启动 | ≤10 min |

回滚必须保留证据：`journalctl -u telegram-bot --since <start>`、`systemctl show` 内存/CPU、迁移前后行数与 `PRAGMA integrity_check` 输出。

## 5. 验证与监控

### 5.1 分阶段测试矩阵

| 阶段 | 单元测试 | 基准测试 | 压测 | A/B 或 canary | 验收数据 |
| --- | --- | --- | --- | --- | --- |
| Phase 0 | `perf_probe.py --self-test`；日志字段单测 | 6 类 handler 基准 | 无（仅基线） | 无 | baseline JSON、P50/P95/P99 |
| Phase 1 | `copy_message` 参数、用户查询合并、迁移脚本 | DB 查询/commit 微基准 | staging 50 条文本/媒体组 | 管理员 2 账号 24h | API 调用计数、commit 计数、无错误 |
| Phase 2 | repository/Session 生命周期、事务边界 | 10× 数据查询 P95 | mock 5 updates/s 10min | 生产 24h 观察 | M-05/M-06、无 `database is locked` |
| Phase 3 | 并发顺序、广播分片、clear 分片 | 广播 236 用户模拟 | 10 updates/s 10min；同一用户 100 条 | `PERF_CANARY_USER_IDS` 72h | M-04/M-08/M-16、429 计数 |
| Phase 4 | 模块导入、清理 job dry-run、HTTP 配置 | 全量基准回归 | 30min 混合负载 | 生产 7 天观察 | 全指标对比、RSS/CPU |
| Phase 5 | 全量回归 | 与 baseline 对比 | 生产低峰全量 | 7 天 soak | 上线报告、遗留项 |

### 5.2 测试方式说明

- **单元测试**：`pytest` + `pytest-asyncio`；所有 Telegram 调用用 `AsyncMock`，不向真实用户发消息。
- **基准测试**：`pytest-benchmark` 或 `scripts/perf_probe.py`；每项至少 1,000 次；输出 mean/p50/p95/p99/max，保存 JSON。
- **压测**：`scripts/perf_probe.py --load --rate 10 --duration 600`；用 mock Bot API 排除 Telegram 网络波动；另在测试群做小流量真实 API 验证。
- **A/B 对比**：单实例无法同时跑两套生产代码；采用“同环境、同数据量、改动前 baseline vs 改动后”对比，生产侧用 `PERF_CANARY_USER_IDS` 做行为对比，至少 24 小时。
- **数据一致性**：迁移/压测后对比 `user`、`message_map`、`formn_status`、`media_group_message` 行数；`PRAGMA integrity_check` 必须返回 `ok`；`MessageMap` 抽样核对用户/群消息 ID 对应关系。
- **业务回归**：测试群覆盖 `/start`、普通文本、回复、图片、媒体组（2/10 条）、`/clear`（0/100/250 条）、`/broadcast`（5/20/236 用户）、验证码正确/错误/超时。

### 5.3 上线后持续监控指标与告警阈值

| 指标 | 采集方式 | 告警阈值 | 观察周期 |
| --- | --- | --- | --- |
| handler P95/P99 | `PERF_LOG=1` JSON → journal | P95 >500 ms 持续 10 min；P99 >1 s | 每日/每周 |
| DB 操作 P95 | repository 计时 | >20 ms 持续 5 min | 每日 |
| Telegram API 错误率 | `telegram.error` 计数 | 5xx/超时 >1%；429 >5 次/5 min | 每日 |
| 事件循环 lag | `event_loop_lag` 探针 | P99 >100 ms 持续 5 min | 每日 |
| RSS / MemoryPeak | `systemctl show` | `MemoryCurrent >320 MB` 10 min；`MemoryPeak >380 MB` | 每日 |
| CPU | `CPUUsageNSec` 速率 | >70% 单核 10 min | 每日 |
| 重启/异常 | `systemctl show NRestarts`、`journalctl` | `NRestarts >0` 或 ERROR >5 条/5 min | 实时（systemd） |
| 广播耗时/429 | 广播汇总消息 + 日志 | >30 s 或 429 >0 | 每次广播 |
| 消息顺序违规 | 压测/生产抽样 | >0 | 每次 canary |
| 表增长 | `COUNT(*)`、DB 文件大小 | `message_map >100k` 或 DB >100 MB（预警） | 每周 |
| 备份时效 | `/root/backups` 时间戳 | >24 h 未备份 | 每日 |

**观测周期**：

- Phase 1/2：上线后 24 小时密集观察；
- Phase 3：每级并发 24 小时，canary 72 小时；
- Phase 5：全量后 7 天，之后每周复盘一次，30 天做最终性能复盘；
- 告警处理：先降级开关/回滚，再排查；不带着 P0 告警继续放量。

## 6. 风险与应对

| 风险 | 概率 | 影响 | 缓解措施 | 应急处理 |
| --- | --- | --- | --- | --- |
| 并发导致同一用户消息乱序 | 高（若不锁） | 高 | 按用户 `asyncio.Lock`；默认并发 0；顺序压测；canary | `PERF_CONCURRENT_UPDATES=0` + 重启；检查 `message_map` 顺序 |
| Session/线程池导致事务串扰或死锁 | 中 | 高 | 每请求 Session；DTO 跨线程；线程池 4；事务短小；单测 | 回滚 P0-01；恢复旧代码；重启 |
| WAL/索引迁移锁库或文件损坏 | 中 | 高 | 低峰窗口；`wal_checkpoint(TRUNCATE)`；备份 `-wal/-shm`；`integrity_check`；先在副本演练 | 停服 → 恢复备份 → 删除 `-wal/-shm` → 启动；必要时 `journal_mode=DELETE` |
| `copy_message` 与 `Chat.send_copy` 语义差异（topic/回复/媒体组） | 中 | 中 | 6 类合成 Update 单测；测试群全量回归；管理员 canary | 切回 `Chat.send_copy` 旧路径；重启 |
| 广播触发 Telegram 429/限流 | 高 | 中 | 20 msg/s 令牌桶；semaphore 5；`RetryAfter` 退避；5→20→236 分批 | 停止广播 job；记录游标；待 `retry_after` 后从失败用户继续 |
| `/clear` 删除不可逆/超 100 条 | 中 | 高 | 分片 100；测试群 250 条演练；生产备份 | 停止 clear；从备份恢复；人工核对 |
| 清理 job 误删仍需的映射 | 中 | 高 | 90 天保留；dry-run 统计；可配置保留天数；先只标记不删 | 关闭 job；恢复备份；恢复保留策略 |
| 日志/探针增加开销或泄露用户信息 | 低 | 中 | 默认关闭；采样 10%；只记录 ID/耗时，不记录消息正文；轮转 | `PERF_LOG=0`；删除日志；调整采样 |
| 模块拆分引入 import cycle/入口错误 | 低 | 中 | 保持 `python -m interactive-bot` 入口；CI 冒烟；小步提交 | 回滚拆分提交；恢复单文件入口 |
| 生产 systemd unit 与仓库 deploy unit 不一致 | 中 | 中 | Phase 0 对齐 `ExecStart`/`EnvironmentFile`；`systemd-analyze verify`；记录实际 unit | 切回旧 unit；`systemctl daemon-reload`；重启 |
| 内存超过 400M 被 cgroup OOM | 低 | 高 | 压测峰值目标 300M；连接池 5；缓存有上限；观察 `MemoryPeak` | 停用缓存/并发；回滚；检查泄漏 |

### 6.1 应急处理流程

1. **发现**：告警/管理员反馈/`journalctl` 出现 ERROR、重启、429、顺序异常。
2. **止血**：优先关闭对应开关（`PERF_CONCURRENT_UPDATES=0`、`PERF_LOG=0`、广播暂停），不先改代码。
3. **保留证据**：`journalctl -u telegram-bot --since ...`、`systemctl show`、DB 行数/`PRAGMA integrity_check`、当前 git SHA。
4. **回滚**：按 4.4 表执行；代码回滚 ≤5 min，DB 恢复 ≤10 min。
5. **验证**：`systemctl status`、`getMe`、测试群发一条文本/回复/媒体组、DB 行数核对。
6. **复盘**：24 小时内输出事故记录（触发条件、影响、根因、修复、防再发），更新本计划与测试用例。

## 7. 待补充清单（不得省略）

以下信息在编制时无法从仓库或生产环境取得，必须在 Phase 0 补齐并回填本文件：

1. **待补充：生产峰值 QPS、消息类型分布、每日消息量**（来源：Telegram update 入口埋点/`message_map` 增长统计）。
2. **待补充：P50/P95/P99 handler 处理时延、端到端时延**（来源：Phase 0 `PERF_LOG` 与 `scripts/perf_probe.py`）。
3. **待补充：事件循环 lag、GC 分代次数与停顿**（来源：`asyncio` 探针、`gc.callbacks`）。
4. **待补充：10 updates/s 压测的吞吐、错误率、内存峰值**（来源：Phase 0 压测脚本）。
5. **待补充：广播 236 用户的实际耗时、429 次数**（来源：测试群/生产小批量演练）。
6. **待补充：各阶段负责人姓名与告警接收渠道**（当前仅保留角色占位）。
7. **待补充：业务对消息顺序、广播耗时、`MessageMap` 保留天数的容忍度**（需产品/客服确认）。
8. **待补充：生产维护窗口审批与变更单号**（按 `02:00–05:00 Asia/Shanghai` 申请）。
9. **待补充：是否允许新增 `aiosqlite`/`pytest-benchmark` 等开发依赖**（当前 `pyproject.toml` 的 `dev` 组为空）。
10. **待补充：生产 systemd unit 最终采用 `.venv/bin/python` 还是 `uv run --frozen --no-dev`**（当前仓库 `deploy/telegram-interactive-bot.service` 与生产实际 unit 不一致）。

## 8. 附录：基线复现命令

```bash
# 代码规模与复杂度（在仓库根目录）
uvx radon raw -s interactive-bot db scripts
uvx radon cc -s -a interactive-bot db scripts
uvx radon mi -s interactive-bot db scripts

# 静态检查
uvx ruff check --statistics interactive-bot db scripts

# 生产资源基线（uk）
ssh -p 7422 root@uk.zmal.top \
  'systemctl show telegram-bot -p MainPID,MemoryCurrent,MemoryPeak,CPUUsageNSec,TasksCurrent,NRestarts'
ssh -p 7422 root@uk.zmal.top \
  'pid=$(systemctl show -p MainPID --value telegram-bot); grep -E "^(VmRSS|VmSwap|Threads):" /proc/$pid/status'

# SQLite 基线（只读；在生产目录执行）
ssh -p 7422 root@uk.zmal.top \
  'cd /root/Telegram-interactive-bot && .venv/bin/python - <<EOF
import sqlite3
con = sqlite3.connect("file:assets/db.sqlite3?mode=ro", uri=True)
print("journal_mode", con.execute("PRAGMA journal_mode").fetchone()[0])
for t in ["user", "message_map", "formn_status", "media_group_message"]:
    print(t, con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0],
          con.execute(f"PRAGMA index_list({t})").fetchall())
con.close()
EOF'

# Telegram API 往返（只读 getMe/getChat，10 次采样；不要在生产高峰执行）
# 见 1.5 节数据来源；脚本不写入数据库、不发消息
```

---

**文档维护**：Phase 0 完成后，将 1.5 节所有“待补充”替换为实测值，并在 PR 中附 `baseline.json` 与对比报告；后续每次性能改动都必须更新本文件的指标表和变更记录。

## 9. 执行任务分解（inline execution）

> 每个任务按 TDD 执行：先写失败测试并看到失败，再写最小实现使测试通过，最后运行完整测试套件。测试命令统一为 `uv run --frozen pytest -q`（需 `UV_CACHE_DIR` 指向可写目录）。

### Task 1: Phase 0 基线探针与观测

范围：3.2 节。新增 `pytest`/`pytest-asyncio`/`pytest-benchmark` 开发依赖并更新 `uv.lock`；新增 `interactive-bot/observability.py`（`PERF_LOG` 采样 JSON 日志、分段计时、事件循环 lag 探针）与 `scripts/perf_probe.py`（6 类合成 Update + AsyncMock Bot，输出 p50/p95/p99，支持 `--baseline`/`--compare`）；新增 `tests/conftest.py` 设置测试环境变量。
步骤：1) 写 observability/probe 的失败测试 2) 实现探针 3) 采集首次 baseline JSON 4) 全量测试通过。
验证：`uv run --frozen pytest -q` 全部通过；`uv run --frozen python scripts/perf_probe.py --self-test` 退出 0。

### Task 2: P0-03 SQLite 索引、WAL、连接池收敛

范围：3.5 节。修改 `db/database.py`（pool 5+5、busy_timeout/WAL/synchronous=NORMAL、`session_scope()`）、`db/model.py`（热字段二级索引、移除主键冗余索引、`MessageMap.created_at` 可空列）；新增 `db/migrations/001_perf_indexes.sql` 与幂等迁移脚本；在临时 SQLite 上验证迁移前后行数与 `integrity_check`。
步骤：1) 写迁移/索引/PRAGMA 失败测试 2) 实现模型与迁移 3) 全量测试通过。
验证：`uv run --frozen pytest -q` 全部通过；迁移脚本在副本上 `integrity_check` 返回 `ok`。

### Task 3: P0-01/P0-02/P1-01 数据访问层、去 get_chat、事务合并

范围：3.3、3.4、3.6 节。新增 `db/repository.py`（同步 repository + DTO）与 `db/executor.py`（4 线程专用线程池 + `run_db`）；handler 改为 `await run_db(...)`，不再共享全局 Session；普通消息/媒体组/广播改用 `Bot.copy_message`/`copy_messages`，合并重复用户查询；一条消息一次短事务、媒体组映射批量提交。
步骤：1) 写“无 get_chat 调用”“只查询一次用户”“commit 次数”“DTO 跨线程”失败测试 2) 实现 repository/executor 3) 改造 handler 4) 全量测试通过。
验证：`uv run --frozen pytest -q` 全部通过；`uv run --frozen python scripts/smoke_test.py` 通过。

### Task 4: P1-02/P1-03/P1-04 并发、广播、clear 分片

范围：3.7～3.9 节。新增 `interactive-bot/services/update_processor.py`（按用户 `asyncio.Lock`、默认关闭的 `PERF_CONCURRENT_UPDATES`）；`_broadcast` 用 `copy_message` + semaphore + 令牌桶 + `RetryAfter` 退避 + 汇总；`/clear` 按 100 条分片删除并清理映射。
步骤：1) 写同用户顺序、广播调用次数/限流/重试、clear 分片失败测试 2) 实现 3) 全量测试通过。
验证：`uv run --frozen pytest -q` 全部通过。

### Task 5: P2-01～P2-05 日志、持久化、HTTP 缓存、模块拆分、数据清理

范围：3.10～3.13 节。日志改可配置轮转、`PicklePersistence` 显式 interval、HTTP 超时/连接池/`allowed_updates`、头像 file_id 与验证码图片列表缓存；把 `__main__.py` 拆为 handlers/services/observability 装配，入口保持 `python -m interactive-bot`；新增默认关闭的数据清理 job。
步骤：1) 写日志轮转、配置解析、入口可导入、清理 dry-run 失败测试 2) 实现 3) 全量测试与冒烟。
验证：`uv run --frozen pytest -q` 全部通过；`uv run --frozen python -m compileall -q interactive-bot db scripts` 退出 0。

### Task 6: CI/文档/基线回填与发布准备

范围：5、7、8 节。CI 增加 pytest 步骤；README/部署文档补充性能开关；用 Task 1 探针在同一环境采集改动后数据，更新 1.5/2.1 节与 `docs/perf/after-*.json`；完成 PR 描述与回滚清单。
步骤：1) 写 CI 配置断言测试 2) 更新 CI/文档 3) 采集 after 数据并回填 4) 全量测试通过。
验证：`uv run --frozen pytest -q` 全部通过；`uv lock --check` 退出 0。

### Task 7: Phase 5 生产灰度与全量上线

范围：4.3、4.4、5.3 节。uk 上备份 → 停服 → 部署代码 → `uv sync --frozen --no-dev` → 执行幂等迁移 → 启动 → 观察 → 逐级放量；记录回滚证据与告警阈值。
步骤：1) 备份与基线快照 2) 灰度 24h 3) 全量 7 天观察 4) 上线报告。
验证：systemd active、`NRestarts=0`、RSS/CPU 达标、无 ERROR/429/顺序违规、DB `integrity_check=ok`。

