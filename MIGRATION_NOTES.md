# 工程化迁移说明（uv / systemd / Docker）

- 基线版本：`master` @ `3bbc2eb`（`docx:Update README.md`）
- 完成日期：2026-11-26
- 变更范围：依赖管理、systemd 部署、Docker 镜像、CI 与文档

## 一、变更清单

| 文件 | 变更 |
| --- | --- |
| `pyproject.toml` | 直接依赖更新到当前较新稳定版本；补充 `readme`；使用 `python-telegram-bot[job-queue]` extra |
| `uv.lock` | 使用 PyPI 官方源重新解析并锁定 15 个包（不再引用阿里云镜像） |
| `db/database.py` | `declarative_base` 改从 `sqlalchemy.orm` 导入，适配 SQLAlchemy 2.1 |
| `deploy/telegram-interactive-bot.service` | 新增可复用的 systemd 单元 |
| `deploy/README.md` | 新增 systemd / Docker 完整部署与运维命令 |
| `dockerfile` | 由 `python:3.11-alpine` 改为 `debian:bookworm-slim`，uv 安装依赖，非 root 运行 |
| `.dockerignore` | 新增，排除 `.git`、`.env`、`.venv`、缓存、运行数据与文档资源 |
| `.github/workflows/ci.yml` | 新增基于 uv 的 CI：`uv sync --frozen --no-dev`、字节编译、smoke 检查 |
| `scripts/smoke_test.py` | 新增无网络 smoke 检查：导入项目包、加载配置、创建 SQLite 表 |
| `README.md` / `README.en.md` | 更新 systemd、Docker 部署说明与仓库链接 |

仓库当前不存在 `requirements*.txt`、`setup.py`、`setup.cfg`、`Pipfile` 等旧依赖入口；
`pyproject.toml` 是唯一的依赖声明入口，`uv.lock` 是唯一锁定文件。

## 二、依赖升级明细

### 直接依赖

| 包 | 迁移前（原 requirements.txt） | 当前锁定版本 | 说明 |
| --- | --- | --- | --- |
| python-telegram-bot | 21.3 | 22.8 | 主版本升级，详见下一节 |
| python-dotenv | 1.0.1 | 1.2.4 | 小版本升级 |
| SQLAlchemy | 2.0.31 | 2.1.9 | 小版本升级，详见下一节 |
| httpx | 0.27.0 | 0.28.1 | 小版本升级，项目代码未使用被移除的旧参数 |
| pytz | 2024.1 | 2026.6 | 时区数据更新 |
| APScheduler | 3.10.4 | 3.11.3 | 受 `python-telegram-bot[job-queue]` 的 `<3.12` 约束，3.11.3 为当前允许的最新稳定版 |

### 关键间接依赖（由 uv.lock 锁定）

`anyio 4.4.0 → 4.16.0`、`certifi 2024.6.2 → 2026.11.20`、`h11 0.14.0 → 0.16.0`、
`httpcore 1.0.5 → 1.0.9`、`idna 3.7 → 3.20`、`typing-extensions 4.12.2 → 4.16.0`、
`tzdata 2024.1 → 2026.6`、`tzlocal 5.2 → 5.4.5`。

原 requirements.txt 中的 `aiolimiter`、`cachetools`、`cffi`、`cryptography`、
`exceptiongroup`、`greenlet`、`h2`、`hpack`、`hyperframe`、`pycparser`、`six`、
`sniffio`、`socksio`、`tomli`、`tornado` 等包不再被当前依赖树直接引入，因此从锁定结果中移除。

### 主版本升级审查

- **python-telegram-bot 21.3 → 22.8**：22.0 的破坏性变更集中在删除 20.x 弃用功能，
  包括 `filters.CHAT`、`filters.StatusUpdate.USER_SHARED`、`Defaults.disable_web_page_preview`、
  `Defaults.quote`、`ApplicationBuilder.(get_updates_)proxy_url`、`HTTPXRequest.proxy_url`、
  `run_polling`/`start_webhook` 的 `*_timeout` 参数、`Message.reply_*` 的 `quote` 参数等。
  本项目均未使用这些 API，因此无需修改业务代码；`JobQueue` 通过
  `python-telegram-bot[job-queue]` extra 安装，`APScheduler` 版本约束保持一致。
- **SQLAlchemy 2.0.31 → 2.1.9**：2.1 将最低 Python 版本提高到 3.11（与项目
  `requires-python = ">=3.11"` 一致）；`greenlet` 不再默认安装，本项目使用同步 ORM，
  不依赖 asyncio 扩展。实际影响仅为 `declarative_base` 的导入路径，已改为
  `from sqlalchemy.orm import declarative_base, sessionmaker`。其余 ORM 行为变更
  （dataclass 默认值、loader options、`filter_by()` 语义等）不适用于本项目的简单查询用法。
- **httpx 0.27.0 → 0.28.1**：项目未使用 `app=` 等 0.28 中移除的接口。
- **APScheduler 3.10.4 → 3.11.3**：项目仅通过 `context.job_queue` 间接使用，未直接调用
  APScheduler API。APScheduler 4.x 是重大重构且 `python-telegram-bot[job-queue]` 明确要求
  `<3.12`，因此本轮不升级，后续应随 python-telegram-bot 的兼容性更新再评估。

### 无法升级的依赖

无。所有直接依赖均已解析到当前约束允许的最新稳定版本。唯一受上游约束的是
`APScheduler`：`python-telegram-bot 22.8` 的 `job-queue` extra 要求 `apscheduler<3.12.0,>=3.10.4`，
因此保持 3.11.3。

## 三、systemd 单元说明

单元文件：`deploy/telegram-interactive-bot.service`

| 配置 | 值 / 作用 |
| --- | --- |
| `Description` | `Telegram Interactive Bot` |
| `After=network-online.target`、`Wants=network-online.target` | 等待网络就绪后再启动 |
| `User=telegram-bot`、`Group=telegram-bot` | 使用专用非特权系统用户运行 |
| `WorkingDirectory=/opt/telegram-interactive-bot` | 项目根目录 |
| `EnvironmentFile=/etc/telegram-interactive-bot/telegram-bot.env` | 独立配置文件，避免密钥进入版本库 |
| `ExecStart=/usr/local/bin/uv run --frozen --no-dev python -m interactive-bot` | 使用 uv 启动仓库实际入口 `interactive-bot.__main__` |
| `Restart=on-failure`、`RestartSec=10` | 异常退出后 10 秒自动重启 |
| `StandardOutput=journal`、`StandardError=journal` | 日志写入 journald |
| `KillSignal=SIGINT`、`TimeoutStopSec=30` | 优雅停止 python-telegram-bot 轮询 |

完整安装、重载、开机自启、启停和 journal 日志命令见 `deploy/README.md` 与
`README.md` 的 3.2 节。

## 四、验证记录

验证环境：`uk.zmal.top:7422`（Debian 13 trixie，1 vCPU / 929 MB，uv 0.13.10，
Docker 26.1.5）。所有测试均在 `/root/tg-bot-migration-final` 独立目录进行，未改动生产目录
`/root/Telegram-interactive-bot`，也未使用生产 `BOT_TOKEN` 启动第二个轮询实例。

### 1. uv sync / uv lock

```text
$ uv sync --frozen --no-dev
Using CPython 3.11.18
Creating virtual environment at: .venv
Installed 13 packages in 700ms
 + anyio==4.16.0
 + apscheduler==3.11.3
 + certifi==2026.11.20
 + h11==0.16.0
 + httpcore==1.0.9
 + httpx==0.28.1
 + idna==3.20
 + python-dotenv==1.2.4
 + python-telegram-bot==22.8
 + pytz==2026.6
 + sqlalchemy==2.1.9
 + typing-extensions==4.16.0
 + tzlocal==5.4.5

$ uv lock --check
Resolved 15 packages in 4ms
```

### 2. uv run / 启动检查

```text
$ uv run --frozen --no-dev python -m compileall -q interactive-bot db scripts
COMPILE_OK

$ uv run --frozen --no-dev python scripts/smoke_test.py
smoke ok: app=interactive-bot, tables=['formn_status', 'media_group_message', 'message_map', 'user']

$ cp /root/Telegram-interactive-bot/assets/db.sqlite3 assets/db.sqlite3
$ uv run --frozen --no-dev python -c "from db.database import SessionMaker; from db.model import User, MessageMap; ..."
users= 236 message_map= 6
```

真实生产数据库副本的只读验证通过：SQLAlchemy 2.1.9 能正常读取现有 236 个用户和
6 条消息映射，未修改生产文件。

入口启动检查使用格式合法但无效的测试 Token，确认进程能完成配置加载、模块导入并到达
Telegram API 鉴权阶段（预期 `InvalidToken`）：

```text
$ BOT_TOKEN="123456789:***" APP_NAME=interactive-bot \
    ADMIN_GROUP_ID=-1001234567890 ADMIN_USER_IDS=123456789 \
    uv run --frozen --no-dev python -m interactive-bot
telegram.error.InvalidToken: The token `123456789:***` was rejected by the server.
```

使用真实 Token 时应正常进入 `run_polling()`；此处不使用生产 Token，因为 Telegram
`getUpdates` 是单消费者，同 Token 双实例会反复 `Conflict: terminated by other getUpdates request`。

### 3. systemd 单元验证

```text
$ systemd-analyze verify deploy/telegram-interactive-bot.service
SYSTEMD_VERIFY_OK
```

`systemd-analyze verify` 退出码为 0。单元必需的
`Description`、`After=network-online.target`、`Wants=network-online.target`、
`User/Group`、`WorkingDirectory`、`EnvironmentFile`、`ExecStart`、`Restart=on-failure`、
`RestartSec`、`StandardOutput/StandardError=journal` 均已包含。

### 4. Docker build / docker run

```text
$ DOCKER_BUILDKIT=1 docker build --progress=plain -t telegram-interactive-bot:migration-final .
...
#15 RUN uv sync --frozen --no-dev
#15 Using CPython 3.11.2 interpreter at: /usr/bin/python3.11
#15 Installed 13 packages in 461ms
#15 DONE 8.5s
...
#20 writing image sha256:ab78aaf0f4b2...
#20 naming to docker.io/library/telegram-interactive-bot:migration-final
DONE 25.9s

$ docker images telegram-interactive-bot:migration-final
telegram-interactive-bot   migration-final   ab78aaf0f4b2   301MB
```

```text
$ docker image inspect telegram-interactive-bot:migration-final \
    --format 'User={{.Config.User}} Entrypoint={{json .Config.Entrypoint}}'
User=bot Entrypoint=["/app/.venv/bin/python","-m","interactive-bot"]

$ docker run --rm --entrypoint id telegram-interactive-bot:migration-final
uid=10001(bot) gid=10001(bot) groups=10001(bot)

$ docker run --rm --entrypoint /app/.venv/bin/python telegram-interactive-bot:migration-final \
    -c "import sys, telegram, sqlalchemy, httpx, dotenv, pytz, apscheduler; print(sys.version.split()[0], telegram.__version__, sqlalchemy.__version__)"
3.11.2 22.8 2.1.9
```

默认 `ENTRYPOINT` 使用测试 Token 启动时同样到达 Telegram 鉴权阶段并返回预期的
`InvalidToken`，说明镜像内项目入口、配置读取和依赖均可用；真实运行需通过
`--env-file .env` 注入实际配置。

## 五、遗留事项 / 后续建议

1. 生产环境切换前，建议先备份现有 `assets/db.sqlite3` 和 `assets/*.pickle`，并按
   `deploy/README.md` 创建 `telegram-bot` 用户和 `/etc/telegram-interactive-bot/telegram-bot.env`。
2. 本项目没有既有自动化测试；本次新增的 `scripts/smoke_test.py` 只覆盖导入、配置加载与
   SQLite 建表，建议后续补充针对消息转发逻辑的单元测试。
3. APScheduler 4.x 暂不升级；待 python-telegram-bot 的 `job-queue` extra 放宽版本约束后，
   再按 changelog 评估迁移。
4. Docker 镜像基于 `debian:bookworm-slim`（Python 3.11），与 `.python-version` 对齐；
   如需使用 Python 3.12/3.13，可同时调整 `.python-version` 和基础镜像并重新生成 `uv.lock`。
