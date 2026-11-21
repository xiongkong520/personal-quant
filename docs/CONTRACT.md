# 项目契约 v1（所有模块必须遵守）

个人量化工作台 · A股 · 模拟盘 · 本地 + Cloudflare 双环境
本文件由 Lead 维护，模块实现者**只读**。需要改契约先提给 Lead。

---

## 1. 边界（违反即返工）

- **只做研究 + 模拟盘**：不接券商/交易所私钥，不真实下单，不保存任何账户凭据。
- 数据只来自 AKShare 公开接口；不引入付费数据源。
- 所有产出必须可离线复现（数据可缓存，模型固定种子）。
- 免责声明随 README 发布：仅供研究学习，非投资建议。

## 2. 模块归属与文件边界

| 模块 | 归属目录 | 负责人 |
|---|---|---|
| 数据 | `src/quant/data/`、`scripts/fetch_data.py` | data-dev |
| 特征+模型+回测 | `src/quant/features/`、`src/quant/model/`、`src/quant/backtest/` | model-dev |
| 模拟盘+本地API+看板 | `src/quant/paper/`、`src/quant/api/`、`frontend/` | paper-dev |
| Cloudflare | `cloudflare/` | cloud-dev |
| 契约/配置/集成/CLI/审查 | `docs/`、`config/`、`pyproject.toml`、`src/quant/cli.py`、`src/quant/config.py`、`tests/test_integration*.py` | Lead |

每人只改自己目录 + 自己的 `tests/test_<模块>.py`；**不要动别人的文件，不要改 `config/default.yaml` 的结构**（可新增键，需在 `config/schema.md` 记录）。

## 3. 数据契约（SQLite，路径 `data/db/quant.db`）

### 3.1 `daily_bars`（日线，不复权价 + 前复权列）

| 列 | 类型 | 说明 |
|---|---|---|
| symbol | TEXT | 6 位代码，如 `600519`；主键之一 |
| trade_date | TEXT | `YYYY-MM-DD`；主键之一 |
| open/high/low/close | REAL | **不复权**原始价（元），保留 2 位 |
| pre_close | REAL | 前收盘（不复权） |
| volume | INTEGER | 成交量（股） |
| amount | REAL | 成交额（元） |
| pct_chg | REAL | 涨跌幅（%），如 2.35 |
| turnover_rate | REAL | 换手率（%），缺失填 0 |
| adj_factor | REAL | 复权因子，默认 1.0 |
| adj_close | REAL | 前复权收盘价 = close × adj_factor |
| limit_up / limit_down | REAL | 按板块规则算出的涨跌停价（见 §6） |
| is_suspended | INTEGER | 1=停牌（无成交），0=正常 |

主键 `(symbol, trade_date)`；无数据的日子（停牌/未上市）不写行，读方自行对齐交易日历。
**写库为幂等 UPSERT**。

### 3.2 `minute_bars`（分钟线）

| 列 | 类型 | 说明 |
|---|---|---|
| symbol | TEXT | 6 位代码 |
| period | INTEGER | 1/5/15/30/60 |
| ts | TEXT | `YYYY-MM-DD HH:MM` 本地时间，收盘时刻 |
| open/high/low/close | REAL | 不复权 |
| volume | INTEGER | 股 |
| amount | REAL | 元 |

主键 `(symbol, period, ts)`。分钟线只保留最近可用窗口（AKShare 限制），不做长期存储承诺。

### 3.3 `signals`（信号历史）

| 列 | 类型 | 说明 |
|---|---|---|
| signal_id | TEXT | 主键，`{symbol}-{trade_date}-{asof_compact}` |
| symbol | TEXT | |
| asof | TEXT | ISO 时间，带 +08:00 |
| trade_date | TEXT | 信号所基于的交易日 |
| price | REAL | 产生信号时的价格 |
| direction | TEXT | `up` / `flat` / `down` |
| prob_up/prob_flat/prob_down | REAL | 和为 1（±1e-6） |
| confidence | REAL | = max(prob)，保留 4 位 |
| horizon | TEXT | `1d` |
| expires_at | TEXT | ISO 时间；过期信号不得参与模拟盘 |
| model_version | TEXT | 与 `model.json` 的 version 一致 |
| top_features | TEXT | JSON 数组，≤5 项，见 §4 |
| source | TEXT | `local` / `cloud` |
| status | TEXT | `ok` / `stale` / `warming_up` |
| created_at | TEXT | ISO 时间 |

### 3.4 模拟盘表（`paper_*`）

`paper_account(id TEXT PRIMARY KEY, cash REAL, created_at TEXT)`；
`paper_positions(symbol TEXT PRIMARY KEY, qty INTEGER, available_qty INTEGER, avg_cost REAL, last_price REAL, updated_at TEXT)`；
`paper_orders(order_id TEXT PRIMARY KEY, symbol TEXT, side TEXT[buysell], qty INTEGER, target_price REAL, status TEXT[pending/filled/rejected/expired], reject_reason TEXT, created_at TEXT, filled_at TEXT, filled_price REAL, fee REAL)`；
`paper_trades` 与 filled 订单一一对应；
`paper_equity_curve(ts TEXT PRIMARY KEY, cash REAL, market_value REAL, equity REAL)`。

## 4. 对外信号 JSON（本地 API / Cloudflare / 前端共用，schema=1）

```json
{
  "schema": 1,
  "symbol": "600519",
  "name": "贵州茅台",
  "asof": "2026-11-21T14:35:00+08:00",
  "trade_date": "2026-11-21",
  "price": 1680.5,
  "direction": "up",
  "prob": { "up": 0.42, "flat": 0.31, "down": 0.27 },
  "confidence": 0.42,
  "horizon": "1d",
  "expires_at": "2026-11-24T09:30:00+08:00",
  "model_version": "v0.1.0",
  "status": "ok",
  "top_features": [
    { "name": "ret_5d", "value": 0.031, "contrib": 0.08 }
  ],
  "paper": { "equity": 1000000.0, "position_qty": 0, "today_pnl": 0.0 }
}
```

约束：
- `direction` 取 `up|flat|down`；数据不足时 `status="warming_up"`、`direction="flat"`、confidence 仍给出但前端显示"数据不足"。
- 交易时段外产生的信号 `status="stale"`。
- `top_features` 按 `|contrib|` 降序，最多 5 条。
- 字段只增不改；新增字段不破坏旧版本消费方。

## 5. 模型产物契约（Python 训练 → 双端推理）

`data/models/model.json`：

```json
{
  "schema": 1,
  "version": "v0.1.0",
  "kind": "softmax_regression",
  "classes": ["down", "flat", "up"],
  "features": ["ret_1d", "ret_5d", "..."],
  "mean": [0.0, 0.0],
  "scale": [1.0, 1.0],
  "coef": [[0.0], [0.0], [0.0]],
  "intercept": [0.0, 0.0, 0.0],
  "trained_at": "2026-11-20T22:00:00+08:00",
  "train_range": ["2023-01-01", "2025-12-31"],
  "horizon": "1d",
  "label_rule": "r = close[t+1]/close[t]-1; r>0.5% -> up, r<-0.5% -> down, else flat",
  "metrics": { "accuracy": 0.0, "logloss": 0.0, "n": 0 }
}
```

- 推理公式：`z = (x - mean) / scale`；`p = softmax(z @ coef.T + intercept)`；class 顺序与 `classes` 一致。
- Cloudflare Worker 端必须能用同一份 JSON 复现 Python 推理（误差 < 1e-6），由 `tests/test_model_export.py` 断言。

## 6. A股交易规则（回测与模拟盘**共用同一实现** `src/quant/paper/rules.py`）

- **T+1**：当日买入的股数计入 `available_qty` 的时点为下一交易日开盘。
- **涨跌停价**：主板（600/601/603/605/000/001/002/003）±10%；ST ±5%（代码含 ST 标记时，数据层置 `is_st=1` 可选，缺省按 10%）；创业板 300/301、科创板 688 ±20%；北交所 8/4 开头 ±30%。涨跌停价 = round(pre_close × (1±比例), 2)，按四舍五入到分（A股规则）。
- **费用**：佣金 `max(成交额 × 0.00025, 5)` 双向；印花税卖出 `成交额 × 0.0005`；过户费 `成交额 × 0.00001` 双向。
- **滑点**：默认 5bps；买入价 = min(open × (1+bps), limit_up)，卖出价 = max(open × (1-bps), limit_down)。
- **整手**：买入数量向下取整到 100 股；资金不足则按可买整手数下单；卖出可零股。
- **成交时点**：T 日收盘后出信号 → T+1 交易日 09:30 以当日开盘价 ± 滑点成交。开盘价触及涨停（买入）/跌停（卖出）视为无法成交，订单 `rejected`。
- **停牌**：`is_suspended=1` 的交易日不成交，订单顺延至下一可交易日。
- **现金**：不允许负现金；不允许超卖。
- 初始虚拟资金与参数见 `config/default.yaml`。

## 7. 命令契约（CLI 由 Lead 实现，调用各模块）

```
python -m quant.cli fetch   --symbols 600519,000001 --start 2024-01-01 --end 2026-11-20
python -m quant.cli train   --horizon 1d --out data/models/model.json
python -m quant.cli signal  --symbols 600519,000001 --out data/reports/signals.json
python -m quant.cli backtest --start 2025-01-01 --end 2026-11-20 --report data/reports/backtest.md
python -m quant.cli paper   --init-cash 1000000 --advance 1
python -m quant.cli serve   --port 8787
python -m quant.cli export  --target cloudflare
```

## 8. 验收（每模块合并前必须过）

- `pytest tests/ -q` 全绿，测试不联网（用 `tests/fixtures/` 的固定数据）。
- 数据层：拉 600519 近 3 年日线成功，行数与交易日历一致，复权/涨跌停列非空。
- 模型层：`model.json` 通过 schema 校验；Python 与 TS 推理一致性测试通过；回测报告含命中率/logloss/校准/基准对比。
- 模拟盘：T+1、涨跌停拒单、费用、滑点各有单测（构造极端行情）。
- Cloudflare：`wrangler dev --local` 起得来，`/api/health`、`/api/signals` 返回契约 JSON；D1 本地模式建表成功。
- 全链路：fetch → train → backtest → paper → serve 能按顺序跑通（`scripts/smoke.sh`）。
- **前端（frontend/ 与 cloudflare/pages/）必须遵循 `docs/DESIGN.md`**：无 CDN/在线字体、无 AI 模板味、60fps 动效、reduced-motion 支持、数据更新只 patch 不整页刷新。

## 9. 模块入口签名（CLI 按这些调用，实现方不得改名）

```python
# quant.data.store
init_db(db_path: Path) -> None
upsert_daily(df: pd.DataFrame, db_path: Path) -> int
upsert_minute(df: pd.DataFrame, db_path: Path) -> int
read_daily(symbol: str, db_path: Path, start: str|None = None, end: str|None = None) -> pd.DataFrame
read_daily_multi(symbols: list[str], db_path: Path, start: str|None = None, end: str|None = None) -> pd.DataFrame
write_signal(sig: dict, db_path: Path) -> None
read_signals(db_path: Path, limit: int = 200) -> list[dict]
read_latest_signals(db_path: Path, symbols: list[str]) -> list[dict]

# quant.data.calendar
trade_days(start: str, end: str, cfg: dict) -> list[str]     # 本地缓存 + AKShare 兜底

# quant.data.fetch
sync_daily(symbols: list[str], start: str, end: str, cfg: dict) -> dict[str, int]
sync_minute(symbols: list[str], period: int, cfg: dict) -> dict[str, int]

# quant.features.builder
build_features(daily: pd.DataFrame, horizon_days: int = 1, label_threshold: float = 0.005) -> pd.DataFrame
# 返回列：trade_date + 特征列（float）+ "label"（up/flat/down）；特征列名写入 model.json

# quant.model.train
train(symbols: list[str], cfg: dict) -> dict          # 训练并写 data/models/model.json，返回 metrics

# quant.model.predict
load_model(path: Path) -> dict
predict_proba(model: dict, x: dict[str, float]) -> dict   # {"up":p,"flat":p,"down":p}
generate_signal(symbol: str, cfg: dict) -> dict            # 契约 §4 JSON；status 按数据情况
infer_signal(model: dict, daily: pd.DataFrame) -> dict | None   # 供 Cloudflare 镜像测试对照

# quant.backtest.engine
run_backtest(start: str, end: str, cfg: dict) -> dict       # 写 report.md，返回指标 dict

# quant.paper.rules（回测与模拟盘共用）
limit_pct(symbol: str, is_st: bool = False) -> float
limit_prices(pre_close: float, symbol: str, is_st: bool = False) -> tuple[float, float]
fees(side: str, amount: float, cfg: dict) -> dict           # {"commission","stamp_tax","transfer_fee","total"}

# quant.paper.engine
init_account(cash: float, db_path: Path) -> None
advance(days: int, cfg: dict) -> dict                       # 推进模拟盘，返回快照
snapshot(cfg: dict) -> dict                                 # 账户/持仓/净值

# quant.api.server
create_app(cfg: dict) -> FastAPI
run(port: int, cfg: dict) -> None

# quant.export_cloud（Lead 负责）
export_to_cloudflare(cfg: dict) -> dict
```

Worker 端点（cloud-dev 实现）：`GET /api/health`、`GET /api/signals`、`POST /api/ingest`、`GET /api/stream`（SSE）。
D1 表 `signals` 字段与契约 §3.3 一致（少 paper_* 内部字段）。

## 10. 双端推理测试

`tests/test_model_export.py`（Lead 写，model-dev 与 cloud-dev 各保证自己一侧）：
用固定 fixture 特征向量，断言 Python `predict_proba` 与 Worker 内 TS 推理结果差 < 1e-6（TS 侧由 cloud-dev 提供 `cloudflare/worker/test/parity.mjs`，`node` 直跑）。

## 11. AKShare 1.20.x 接口映射（2026-11-21 实测，data-dev 必读）

| 用途 | 调用 | 返回列 | 转换要点 |
|---|---|---|---|
| 交易日历 | `ak.calendar_trade_date_hist_sina()` | `trade_date`(date) | 转 `YYYY-MM-DD` list，缓存 `data/raw/trade_calendar.csv` |
| 日线 | `ak.stock_zh_a_hist_em(symbol="600519", period="daily", start_date="20250101", end_date="20261120", adjust="qfq")` | 日期/开盘/收盘/最高/最低/成交量/成交额/振幅/涨跌幅/涨跌额/换手率/股票代码 | **成交量单位=手 → ×100 转股**；`pre_close = 收盘 - 涨跌额`；`close` 为不复权 → 需再拉一次 `adjust=""` 并 merge（qfq 列的"收盘"即 `adj_close`） |
| 分钟线 | `ak.stock_zh_a_hist_min_em(symbol="600519", period="5", start_date="2026-11-10 09:30:00", end_date="2026-11-20 15:00:00", adjust="")` | 时间/开盘/收盘/最高/最低/成交量/成交额/均价 | 成交量手→股；时间转 `YYYY-MM-DD HH:MM` |
| 实时快照 | `ak.stock_zh_a_spot_em()` | 全市场表 | 只取所需 symbol 行；非交易时段返回最新收盘 |
| 五档盘口 | `ak.stock_bid_ask_em(symbol="600519")` | 卖5~买5 | 可选，用于生成额外特征 |

注意：
- 旧名 `tool_trade_date_hist_sina`、`stock_zh_a_hist` 在 1.20.4x 已更名，**不要用旧名**。
- 限流：东财接口无 key 但高频会被限；采集间隔 ≥0.3s，失败重试 2 次退避，写库幂等。
- 日线一次可拉数年；拉全量分 symbol 循环，单 symbol 失败不中断整体。

## 12. 历史回测 + 胜率口径（`run_backtest` 必须全部输出，缺一项视为未完成）

输出两份：`data/reports/backtest.md`（人读）+ `data/reports/backtest.json`（机读，字段如下）。

### 12.1 方向判断指标（预测质量）
- `accuracy`：三分类总体准确率；`n`、`coverage`（有信号样本占比）
- 分方向：`up` / `flat` / `down` 各自的 precision、recall、F1
- `logloss`、`brier`（三分类 multiclass Brier）
- `confusion`：3×3 混淆矩阵
- 按置信度分桶实际命中率：≤0.4 / 0.4–0.5 / 0.5–0.6 / >0.6，每桶给 n 与 hit_rate → 校准曲线
- 分年度 / 分季度准确率（稳定性），标出样本量

### 12.2 胜率（交易口径，必须与方向准确率分开命名）
- `win_rate`：平仓盈利次数 / 平仓次数；同时给 `win_rate_by_direction`（long/short/flat 不参与）
- `trades`：总笔数、盈利笔数、亏损笔数、平局
- `avg_return_per_trade`、`payoff_ratio`（平均盈利/平均亏损）
- `max_drawdown`、`sharpe`（日频年化，无风险利率按 0）、`total_return`
- 胜率 95% **Wilson 置信区间**（样本 < 30 时必须标注"样本不足，仅供参考"）
- `equity_curve`：日期 + 净值数组（供前端画图）

### 12.3 基准对比（必须并列）
- `always_flat`（全预测走平）、`majority`（训练窗多数类）、`ma5_cross`（5 日线交叉策略）、`random`
- 每项给准确率/胜率/收益，报告里明确"相对基准提升了多少、是否在置信区间外"

### 12.4 walk-forward 规则
- 按时间滚动切分：训练窗 ≥ 1 年，测试窗 1 季度，`walk_forward_splits` 见配置；严禁用测试期数据训练或调参
- 报告开头写明：数据区间、symbol 列表、样本量、切分方式、模型版本、随机种子
- `metrics` 同时写入 `model.json` 的 `metrics` 字段（§5）
