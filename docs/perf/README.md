# 性能基线采集说明

所有数据由 `scripts/perf_probe.py` 在工作区生成，不连接 Telegram：合成 Update +
`AsyncMock` Bot 调用真实 handler，因此测量的是应用内处理耗时（含 SQLite），不含
Telegram 网络往返。`--real` 模式会设置测试用环境变量；生产 Token 不会被读取。

运行时数据层使用标准库 `sqlite3`，不再安装 SQLAlchemy/pytz。可用下面的命令确认
入口可导入且 SQLAlchemy 没有进入 `sys.modules`：

```bash
uv run --frozen python -c "import importlib, sys; importlib.import_module('interactive-bot.__main__'); assert 'sqlalchemy' not in sys.modules; print('runtime deps ok')"
```

采集命令：

```bash
export UV_CACHE_DIR=/root/dsh/.uv-cache

# 仅验证探针本身
uv run --frozen python scripts/perf_probe.py --self-test

# 采集基线（建议使用全新的临时数据库，避免历史行数干扰）
TMPDB=$(mktemp /tmp/tg-perf-XXXX.sqlite)
DATABASE_URL="sqlite:///$TMPDB" uv run --frozen python scripts/perf_probe.py \
  --real --iterations 300 --baseline docs/perf/baseline-2026-11-27.json
rm -f "$TMPDB"

# 回归门禁：任一 p50/p95/p99/mean 恶化超过 10% 即返回 1
uv run --frozen python scripts/perf_probe.py --real \
  --compare docs/perf/baseline-2026-11-27.json
```

## 内存采样与验收

改造后使用 `scripts/memory_probe.py` 从 `/proc/<pid>/status` 读取 RSS、VmData 和线程数，
按 JSON Lines 输出。验收目标：30 分钟 idle RSS ≤55 MB；24 小时首小时与末小时中位数
增长 ≤3 MB，线程数保持平稳。

```bash
uv run --frozen python scripts/memory_probe.py --self-check

# 30 分钟验收（服务单元名按实际部署替换，例如 telegram-bot.service）
PID=$(systemctl show -p MainPID --value telegram-bot.service)
uv run --frozen python scripts/memory_probe.py --pid "$PID" \
  --interval 60 --duration 1800 > /tmp/tg-memory-30m.jsonl

# 24 小时巡检
uv run --frozen python scripts/memory_probe.py --pid "$PID" \
  --interval 300 --duration 86400 > /tmp/tg-memory-24h.jsonl
```

改造前基线：[memory-baseline-2026-11-27.json](memory-baseline-2026-11-27.json)；上线后另存
`memory-after-2026-11-27.json` 作为对比。

## 2026-11-27 结果（300 次/场景）

| 场景 | p50 前 → 后 (ms) | p95 前 → 后 (ms) | p99 前 → 后 (ms) |
| --- | --- | --- | --- |
| text | 29.47 → 16.51 | 40.85 → 27.48 | 47.63 → 68.30 |
| reply | 18.71 → 15.28 | 26.92 → 25.05 | 33.33 → 34.35 |
| media | 12.29 → 9.48 | 20.74 → 15.71 | 30.33 → 21.13 |
| new_user | 16.63 → 12.83 | 26.94 → 26.28 | 43.06 → 60.63 |
| clear | 1.35 → 1.42 | 3.68 → 11.08 | 7.19 → 16.91 |
| broadcast | 1.38 → 1.33 | 6.47 → 3.74 | 12.17 → 9.60 |

结论：主要热路径的 p50/p95 明显改善（text p95 -33%，media p95 -24%）；
部分 p99 出现离群值（线程调度/GC/短样本），clear 的场景在探针中几乎不写库，
其 p95 变化属于噪声。回归门禁的 10% 阈值对 p99 过于敏感，正式验收应以生产
结构化日志（`PERF_LOG=1`）的多日 P95/P99 为准，探针只作相对参考。

原始 JSON：[baseline-2026-11-27.json](baseline-2026-11-27.json)、
[after-2026-11-27.json](after-2026-11-27.json)。
