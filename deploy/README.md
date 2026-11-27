# 部署指南

本项目使用 [uv](https://docs.astral.sh/uv/) 管理 Python 版本、虚拟环境和依赖。
下面给出 systemd（推荐）和 Docker 两种部署方式。

## 一、systemd 部署（推荐）

以下命令以 Debian / Ubuntu 为例，默认安装到 `/opt/telegram-interactive-bot`。

### 1. 安装 uv 和系统依赖

```bash
sudo apt-get update
sudo apt-get install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
uv --version
```

### 2. 创建专用系统用户和目录

```bash
sudo useradd --system --user-group \
  --home-dir /opt/telegram-interactive-bot \
  --shell /usr/sbin/nologin telegram-bot
sudo mkdir -p /opt/telegram-interactive-bot /etc/telegram-interactive-bot
sudo chown telegram-bot:telegram-bot /opt/telegram-interactive-bot
```

### 3. 获取代码并安装依赖

```bash
sudo -u telegram-bot git clone https://github.com/kmizmal/Telegram-interactive-bot.git \
  /opt/telegram-interactive-bot
cd /opt/telegram-interactive-bot
sudo -u telegram-bot /usr/local/bin/uv sync --frozen --no-dev
```

### 4. 配置环境变量

```bash
sudo install -m 600 .env_example /etc/telegram-interactive-bot/telegram-bot.env
sudoedit /etc/telegram-interactive-bot/telegram-bot.env
```

按 `.env_example` 中的注释填写 `BOT_TOKEN`、`APP_NAME`、`ADMIN_GROUP_ID`、
`ADMIN_USER_IDS` 等配置。该文件只由 systemd 读取，不会进入版本库。

### 5. 安装并启动 systemd 服务

```bash
sudo cp deploy/telegram-interactive-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-interactive-bot.service
```

### 6. 常用管理命令

```bash
# 查看服务状态
sudo systemctl status telegram-interactive-bot.service

# 启动 / 停止 / 重启
sudo systemctl start telegram-interactive-bot.service
sudo systemctl stop telegram-interactive-bot.service
sudo systemctl restart telegram-interactive-bot.service

# 开机自启 / 取消开机自启
sudo systemctl enable telegram-interactive-bot.service
sudo systemctl disable telegram-interactive-bot.service

# 查看实时 journal 日志（Ctrl+C 退出）
sudo journalctl -u telegram-interactive-bot.service -f

# 查看最近 200 行日志
sudo journalctl -u telegram-interactive-bot.service -n 200 --no-pager
```

### 7. 更新代码

```bash
cd /opt/telegram-interactive-bot
sudo -u telegram-bot git pull --ff-only
sudo -u telegram-bot /usr/local/bin/uv sync --frozen --no-dev
sudo systemctl restart telegram-interactive-bot.service
```

> 服务单元中的关键配置：
> - `After=network-online.target` + `Wants=network-online.target`：等待网络就绪。
> - `User=telegram-bot` / `Group=telegram-bot`：使用非特权用户运行。
> - `WorkingDirectory=/opt/telegram-interactive-bot`：项目根目录。
> - `EnvironmentFile=/etc/telegram-interactive-bot/telegram-bot.env`：从独立文件读取配置。
> - `ExecStart=/usr/local/bin/uv run --frozen --no-dev python -m interactive-bot`：用 uv 启动实际入口。
> - `Restart=on-failure`、`RestartSec=10`：异常退出后自动重启。
> - `StandardOutput=journal`、`StandardError=journal`：日志统一交给 journald。

## 二、Docker 部署

### 1. 构建镜像

```bash
docker build -t telegram-interactive-bot:latest .
```

构建过程基于 `debian:bookworm-slim`，安装系统 Python 3 和 uv，复制
`pyproject.toml` 与 `uv.lock` 后执行 `uv sync --frozen --no-dev`，再复制源码，
并以非 root 用户 `bot`（UID 10001）运行。

### 2. 准备配置文件

```bash
cp .env_example .env
# 编辑 .env，填入真实的 BOT_TOKEN / ADMIN_GROUP_ID / ADMIN_USER_IDS 等
```

### 3. 启动容器

```bash
docker run -d \
  --name telegram-interactive-bot \
  --restart unless-stopped \
  --env-file .env \
  -v telegram-bot-assets:/app/assets \
  telegram-interactive-bot:latest
```

`/app/assets` 用于持久化 SQLite 数据库、Pickle 会话文件和日志；首次创建命名卷时
会从镜像复制 `assets/` 初始内容。

### 4. 查看日志与停止

```bash
docker logs -f telegram-interactive-bot
docker stop telegram-interactive-bot
docker rm telegram-interactive-bot
```

### 5. 验证镜像

```bash
# 确认容器以非 root 用户运行
docker run --rm --entrypoint id telegram-interactive-bot:latest

# 确认依赖和项目入口可导入（不连接 Telegram）
docker run --rm --entrypoint /app/.venv/bin/python telegram-interactive-bot:latest \
  -c "import telegram, sqlalchemy, httpx, dotenv, pytz, apscheduler; print('deps ok')"

## 三、性能配置、迁移与观测

升级到含性能优化的版本后，**先执行数据库迁移，再启动服务**（幂等，可重复执行）：

```bash
cd /opt/telegram-interactive-bot
sudo -u telegram-bot /usr/local/bin/uv run --frozen --no-dev python -m db.migrations.perf_indexes \
  --database assets/db.sqlite3
sudo systemctl restart telegram-interactive-bot.service
```

迁移会补 `message_map.created_at`、六个热字段二级索引，并删除主键上的冗余索引；
启用 WAL 后请确保备份同时包含 `db.sqlite3-wal` 与 `db.sqlite3-shm`。

可选环境变量（写入 `EnvironmentFile`，均保持默认即可获得优化收益）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PERF_LOG` | `0` | `1` 时向 journal 输出 JSON 性能记录 |
| `PERF_SAMPLE_RATE` | `1` | 性能日志采样率，`0.1` 表示 10% |
| `PERF_CONCURRENT_UPDATES` | `0` | `2/4/8` 开启受控并发；上线后逐级放量 |
| `PERSISTENCE_INTERVAL` | `300` | PicklePersistence flush 间隔（秒） |
| `LOG_TO_FILE` | `1` | `0` 时只写 journal；文件为 10MB × 5 轮转 |
| `HTTP_POOL_SIZE` | `8` | Telegram HTTP 连接池大小 |
| `ENABLE_DATA_CLEANUP` | `0` | `1` 时启用每日 03:00 的映射清理（默认关闭） |
| `MESSAGE_MAP_RETENTION_DAYS` | `90` | 保留天数；清理前请先用 dry-run 核对 |

本地回归验证：

```bash
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen python scripts/perf_probe.py --self-test
```

详见 [性能计划](../PERFORMANCE_REFACTORING_PLAN.md) 与 [基线说明](../docs/perf/README.md)。
```
