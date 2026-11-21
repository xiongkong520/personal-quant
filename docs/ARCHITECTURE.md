# 架构

## 一句话

本地做**研究闭环**（采集 → 特征 → 训练 → 回测 → 模拟盘），Cloudflare 做**实时展示与判断推送**，两端通过契约化的信号 JSON 与模型权重 JSON 解耦。

## 数据流

```
                ┌─────────────── 本地 Python ───────────────┐
 AKShare ──────▶│ fetch → SQLite(quant.db)                  │
 (东财接口)     │   │                                        │
                │   ├─▶ features → model.json（训练）        │
                │   ├─▶ backtest → reports/backtest.md+json  │
                │   └─▶ paper（模拟盘，T+1/费用/滑点）        │
                │        │                                   │
                │        └─▶ CLI signal → signals.json       │
                └──────────────┬─────────────────────────────┘
                               │ POST /api/ingest（token）
                               ▼
                ┌────────── Cloudflare ──────────┐
                │ Worker ── D1（signals 历史）    │
                │   ├─ GET /api/signals           │
                │   ├─ GET /api/stream（SSE）     │
                │   └─ Pages 看板（方向/概率/胜率）│
                └────────────────────────────────┘
```

## 模块职责

| 模块 | 目录 | 职责 | 关键约束 |
|---|---|---|---|
| 数据 | `src/quant/data/` | AKShare 拉取、清洗、SQLite 存储 | 幂等 UPSERT；成交量手→股；不复权 + 前复权双列 |
| 特征 | `src/quant/features/` | 20–30 个历史特征 + T+1 标签 | 严禁未来函数 |
| 模型 | `src/quant/model/` | softmax 回归训练、JSON 导出、推理 | 双端可复现（§5 公式） |
| 回测 | `src/quant/backtest/` | walk-forward、方向准确率、胜率、校准、基准 | §12 字段齐全 |
| 模拟盘 | `src/quant/paper/` | T+1、涨跌停、费用、滑点、虚拟资金 | 规则与回测共用 rules.py |
| 本地 API | `src/quant/api/` | FastAPI + 静态看板 | 契约 §4 信号 JSON |
| Cloudflare | `cloudflare/` | Worker + D1 + Pages | 无需账号可 `wrangler dev --local` 验证 |

## 关键设计决策

1. **模型选 softmax 回归而非 GBDT/LLM**：可导出成 JSON、Python 与 TS 两端逐位复现、可解释（贡献度近似）。精度靠特征和校准补。若以后要上 GBDT，需要另加 ONNX 或服务化方案，不破坏契约。
2. **胜率与方向准确率分开统计**：方向准确率衡量预测质量，胜率（交易口径）衡量模拟交易质量，两者不可混用（§12）。
3. **模拟盘先于实盘**：不接券商接口，避免资金与合规风险；模拟盘用真实行情 + 真实费用规则，结果可比。
4. **Cloudflare 本地优先**：`wrangler dev --local` 用本地 D1 跑通全链路，部署只是换 binding，不是阻塞项。
5. **信号 JSON 只增不改**：前端、Worker、Python 三方消费同一 schema，版本号在 `schema` 字段。

## 失败模式与兜底

| 故障 | 表现 | 兜底 |
|---|---|---|
| AKShare 限流/改名 | 抓数失败 | 重试退避 + 本地缓存；接口名变更写进契约 §11 |
| 数据缺失 | 特征 NaN | 丢弃该样本；信号 `status=warming_up` |
| 模型文件缺失 | 推理失败 | 信号 `status=warming_up`，前端提示"未训练" |
| Worker 未启动 | 推送失败 | 本地先落库，云端下次全量同步 |
| 非交易时段 | 数据不更新 | 信号 `status=stale`，不参与模拟盘 |

## 安全边界

- 无交易密钥、无券商账户、无付费数据源。
- Worker 的 `/api/ingest` 用 token 校验，token 只存本地 `.dev.vars` / 环境变量，不进 git。
- 所有展示页带免责声明。
