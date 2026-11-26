# 全链路升级方案（零成本云端运行 + 自动化训练闭环）

> 状态：方案待确认 · 未实施
> 原则：不伪造数据、渐进式改造、保持 Cloudflare Pages 部署方式、训练闭环零成本（GitHub Actions / Kaggle / Colab 免费算力）

## 一、文件目录清单

### 1.1 新增文件

| 文件 | 用途 |
|---|---|
| `cloudflare/worker/src/cache.ts` | 边缘缓存层（Cache API 60s TTL + stale 快照 + X-Cache 头） |
| `cloudflare/worker/src/market_overview.ts` | `/api/market-overview` 聚合（5 指数 / 全市场宽度 / 板块 Top10 / 清洗） |
| `cloudflare/worker/src/model_infer.ts` | 轻量 LightGBM JSON 推理（树遍历，T+1 三分类概率） |
| `cloudflare/worker/migrations/0008_light_tables.sql` | 4 张轻量业务表 |
| `cloudflare/worker/migrations/0009_drop_heavy.sql` | 剥离历史 K 线/盘中大表（klines、intraday_index、boards、market_snapshots 等） |
| `training/README.md` | 训练管道说明（Kaggle/Colab/GitHub Actions 用法） |
| `training/requirements.txt` | 训练依赖（akshare / lightgbm / pandas / pyarrow） |
| `training/fetch_data.py` | 拉近 3 年前复权日线 + 每日涨跌停池 → `training/data/*.parquet`（压缩） |
| `training/train_lgbm.py` | 特征工程 + Walk-Forward 二分类 LightGBM（扣 0.2% 双边摩擦） |
| `training/export_model.py` | 导出 `model/latest_model.json` + `model/feature_config.json` |
| `model/latest_model.json` | 训练产物（提交回仓库，Worker 加载） |
| `model/feature_config.json` | 特征名/顺序/归一化参数映射 |
| `docs/UPGRADE_PLAN.md` | 本方案 |

### 1.2 修改文件

| 文件 | 改动 |
|---|---|
| `cloudflare/worker/src/index.ts` | 新增 `/api/market-overview`；GET 接口接入 Cache API；错误降级返回 stale + 徽章字段；`/api/market` 兼容转发 |
| `cloudflare/worker/src/db.ts` | 4 张新表 CRUD；移除大表读写函数 |
| `cloudflare/worker/src/schema.ts`、`schema.sql` | 同步 4 表 schema（删除大表定义） |
| `cloudflare/worker/src/market_live.ts` | 指数/宽度/情绪数据源复用；输出对齐 `market_overview` 清洗口径 |
| `cloudflare/worker/src/daily_sync.ts` | 去掉 K 线入库；信号→`signal_log`、模拟盘→`account_snapshot/positions/order_log`；事件/板块/regime 改走缓存 |
| `cloudflare/worker/src/security.ts` | Turnstile 校验后的 cookie 写入与并发控制；429 不再弹遮挡层（交给前端徽章） |
| `frontend/app.js` + `cloudflare/pages/app.js` | ① Turnstile 成功→写 cookie→再触发首次请求（串行）② 轮询只读缓存接口 ③ 顶部状态栏（连接/数据源/盘中-休市/快照时间）④ 00 盘面总览（5 指数+涨跌分布+温度计+板块切换）⑤ 01 信号（概率条/置信度仪表/Top5 特征贡献）⑥ 错误改顶部徽章 |
| `frontend/styles.css` + `cloudflare/pages/styles.css` | 终端风格、等宽数字、红涨绿跌、表格首列固定+横滚、移动端 |
| `frontend/index.html` + `cloudflare/pages/index.html` | 顶部状态栏结构、区域 00-05 重排、版本号（两套保留 6 处模式差异） |
| `src/quant/export_cloud.py` | 推送信号/账户/持仓/订单 到新表 |
| `src/quant/cli.py` | 新增 `train-lgbm`（调用 training 脚本）与 `export-model` |
| `.github/workflows/daily-quant.yml` | 新增：训练 LightGBM → 导出模型 → 提交 `model/`；推送 4 表数据 |
| `.github/workflows/ci.yml` | 训练脚本离线单测（合成数据） |
| `docs/CONTRACT.md` | 新接口/表契约章节 |
| `tests/` | 新增 `test_training_features.py`、`test_model_export.py`、`test_market_overview_contract.py`（离线） |

### 1.3 删除/归档

| 文件/表 | 处理 |
|---|---|
| D1 表：`klines`、`intraday_index`、`boards`、`market_snapshots`、`events`、`backtest_reports`、`paper_snapshots`、`regime_snapshots`、`market_summaries`、`signals` | 迁移期保留只读兼容，完成后删除（历史行情改 Parquet；快照/事件/回测/regime 改 Cache API） |
| D1 表：`stock_names` | 移出 D1 → 搜索用静态 JSON（前端内置 40 只池 + `/api/search` 读静态常量） |
| `cloudflare/worker/src/index_kline.ts`、`stock_pool.ts` | 删除（不再批量入库；`/api/kline` 保留实时拉取+缓存） |
| 本地 `paper` 旧表 | 保留本地研究环境，不影响云端 |

## 二、数据库 Schema（D1 仅 4 张轻量表）

```sql
-- 1) 模拟盘账户快照（每次推送覆盖最新一条）
CREATE TABLE IF NOT EXISTS account_snapshot (
  asof          TEXT PRIMARY KEY,   -- ISO +08:00
  total_asset   REAL NOT NULL,      -- 总资产
  cash          REAL NOT NULL,      -- 可用现金
  market_value  REAL NOT NULL,      -- 持仓市值
  today_pnl     REAL NOT NULL,      -- 今日盈亏
  total_pnl     REAL NOT NULL,      -- 累计盈亏
  position_count INTEGER NOT NULL DEFAULT 0,
  source        TEXT NOT NULL DEFAULT 'paper',
  created_at    TEXT NOT NULL
);

-- 2) 当前模拟持仓（每标的一行，快照覆盖）
CREATE TABLE IF NOT EXISTS positions (
  symbol      TEXT PRIMARY KEY,
  name        TEXT,
  qty         INTEGER NOT NULL,
  cost_price  REAL NOT NULL,
  last_price  REAL,
  market_value REAL,
  pnl         REAL,     -- 浮动盈亏
  pnl_pct     REAL,
  asof        TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- 3) 模拟订单流水（含费用明细）
CREATE TABLE IF NOT EXISTS order_log (
  id           TEXT PRIMARY KEY,    -- 订单号
  symbol       TEXT NOT NULL,
  name         TEXT,
  side         TEXT NOT NULL,       -- buy / sell
  qty          INTEGER NOT NULL,
  target_price REAL,
  filled_price REAL,
  commission   REAL NOT NULL DEFAULT 0,  -- 佣金
  stamp_tax    REAL NOT NULL DEFAULT 0,  -- 印花税
  transfer_fee REAL NOT NULL DEFAULT 0,  -- 过户费
  fee_total    REAL NOT NULL DEFAULT 0,
  status       TEXT NOT NULL,       -- filled / rejected / partial
  reason       TEXT,
  trade_date   TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_log_created ON order_log (created_at DESC);

-- 4) T+1 信号日志（模型预测概率 + Top5 特征贡献）
CREATE TABLE IF NOT EXISTS signal_log (
  id           TEXT PRIMARY KEY,    -- symbol-trade_date-model_version
  symbol       TEXT NOT NULL,
  name         TEXT,
  trade_date   TEXT NOT NULL,       -- 信号对应交易日
  horizon      TEXT NOT NULL DEFAULT '1d',
  prob_up      REAL,
  prob_flat    REAL,
  prob_down    REAL,
  confidence   REAL,
  direction    TEXT,                -- up / flat / down
  rank_score   REAL,                -- 综合排序分
  top_features TEXT,                -- JSON [{name,value,contrib}]
  model_version TEXT,
  expires_at   TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signal_log_date ON signal_log (trade_date DESC, rank_score DESC);
```

**迁移规则**：`signals` → `signal_log`（字段映射，兼容期双写）；`paper_snapshots/paper_*` → `account_snapshot/positions/order_log`；`klines/intraday_index/boards/market_snapshots/events/backtest_reports/regime_snapshots/market_summaries` → 删除，改 Cache API。

## 三、接口 Schema

### 3.1 `GET /api/market-overview`（新增，主接口）

请求参数：`?sector_type=industry|concept&sort=gainers|losers&limit=10`

```json
{
  "schema": 1,
  "asof": "2026-11-26T10:23:00+08:00",
  "trade_date": "2026-11-26",
  "session": "trading | lunch | closed",
  "cache": { "hit": true, "age_sec": 32, "ttl": 60, "stale": false },
  "indices": [
    { "code": "000300", "name": "沪深300", "price": 3963.82, "pct_chg": 0.14, "amount": 1234567890.0 },
    { "code": "000688", "name": "科创50",  "price": 1023.45, "pct_chg": 0.52, "amount": 123456789.0 },
    { "code": "399006", "name": "创业板指", "price": 2278.99, "pct_chg": -0.05, "amount": 234567890.0 },
    { "code": "000905", "name": "中证500", "price": 6123.45, "pct_chg": 0.21, "amount": 345678901.0 },
    { "code": "000852", "name": "中证1000", "price": 6501.23, "pct_chg": 0.33, "amount": 456789012.0 }
  ],
  "breadth": {
    "up": 1838, "down": 3267, "flat": 114,
    "limit_up": 64, "limit_down": 4, "broken_rate": 0.18,
    "total_amount": 1234567890000.0,
    "sentiment": { "score": 63.3, "state": "divergence", "label": "分化", "breadth_score": 36.0, "heat_score": 94.1 }
  },
  "sectors": {
    "industry": {
      "gainers": [{ "name": "陶瓷行业", "pct_chg": 2.21, "net_inflow": 12300000.0,
        "leader": { "symbol": "002100", "name": "天康生物", "pct_chg": 9.98 } }],
      "losers":  [{ "name": "船舶制造", "pct_chg": -1.35, "net_inflow": -8000000.0, "leader": null }]
    },
    "concept": { "gainers": [], "losers": [] }
  },
  "quality": { "sources": ["tencent", "legulegu", "sina"], "degraded": false, "errors": [] }
}
```

### 3.2 兼容接口
- `GET /api/market`：内部转发 `market-overview`（保留旧字段）——兼容期保留，前端切换后标记 deprecated
- `GET /api/kline?symbol=&days=`：实时拉取（东财/腾讯）→ Cache API 60s，不落 D1
- `GET /api/signals`：改读 `signal_log`
- `GET /api/paper`：聚合 `account_snapshot + positions + order_log`
- `GET /api/events`、`/api/regime`、`/api/backtest`、`/api/summary/latest`：数据源改 Cache API（GitHub Actions 推送 → KV/仓库 JSON → Worker 缓存）
- 缓存头：`X-Cache: HIT|MISS|STALE`、`X-Snapshot-Age: 32`

### 3.3 模型 JSON Schema（`model/latest_model.json`）

```json
{
  "schema": 1,
  "model": "lightgbm",
  "version": "lgbm-2026-11-25",
  "trained_at": "2026-11-25T20:00:00+08:00",
  "label": { "horizon": 1, "definition": "T+1 收益 > 0.2%（双边摩擦）", "classes": ["up", "flat", "down"] },
  "features": [
    { "name": "excess_ret_5d", "index": 0, "dtype": "float" },
    { "name": "amount_share", "index": 1, "dtype": "float" }
  ],
  "feature_config": {
    "winsorize": { "lower": 0.01, "upper": 0.99 },
    "normalize": { "mean": [], "std": [] }
  },
  "trees": [
    { "split_feature": [0, 1], "threshold": [0.5, -0.2], "left_child": [1, -1], "right_child": [-1, -2], "leaf_value": [], "decision_type": ["<=", "<="] }
  ],
  "objective": "multiclass",
  "num_class": 3,
  "metrics": { "walk_forward": { "accuracy": 0.0, "win_rate": 0.0 }, "cost": 0.002 }
}
```

Worker `model_infer.ts`：加载 JSON → 按特征顺序计算 → 遍历树 → softmax 得到三分类概率。

## 四、模块一实现要点（限流与缓存）

1. **Turnstile 时序**：`verify` 成功 → Worker 返回 `Set-Cookie: quant_pass` → 前端在 `fetch('/api/verify')` 的响应完成且 cookie 写入后，才调用 `refreshAll()`；轮询启动放在首次请求 `finally` 中，保证串行。
2. **轮询只读缓存**：前端 60s 轮询直接打 `/api/market-overview`（Cache API HIT）；Worker 内部：`cache.match(key)` 命中直接返回；未命中：判断交易时段 → 抓取 → `cache.put` 60s；上游限流/休市 → 返回上次有效快照（KV/内存）并标 `cache.stale=true`。
3. **错误展示**：429/上游失败不再显示遮挡弹窗 → 顶部状态徽章（`盘中/休市`、`数据延迟`、`缓存 32s 前`）。

## 五、模块四训练管道要点

- 数据：AKShare 近 3 年前复权日线（`stock_zh_a_hist`，qfq）+ `stock_zt_pool_em` 每日涨跌停池 → `training/data/*.parquet`（snappy 压缩）
- 特征（全部 shift(1) 防未来函数）：超额收益（个股 vs 板块/指数）、成交额全市场占比、上下影线比例、均线乖离度、市场情绪温度计（涨停/跌停/炸板/连板）
- 训练：LightGBM 三分类（up/flat/down）Walk-Forward（训练窗口滚动，样本外验证），标签扣除 0.2% 双边摩擦
- 导出：`model/latest_model.json`（轻量树）+ `model/feature_config.json`；GitHub Actions 训练后自动 commit 回仓库
- Worker：启动/定时从 GitHub raw 拉取模型 JSON + Cache API 缓存 → `model_infer.ts` 线上推理 → 写 `signal_log`

## 六、分步实施与验证

| 步骤 | 内容 | 验证 |
|---|---|---|
| S1 | 模块一：cache.ts + Turnstile 时序 + 轮询改造 | tsc / 页面无 429 / 缓存头 HIT |
| S2 | 模块二：market-overview（5 指数 + breadth + sectors + 清洗） | curl 字段齐全 / 前后端一致 |
| S3 | 模块三：4 表迁移 + 剥离 K 线入库 + export 改造 | 迁移 apply / 表计行数 / 旧接口兼容 |
| S4 | 模块四：training/ 脚本 + 模型导出 + Actions 集成 + Worker 推理 | 本地跑通训练（小样本）/ 模型 JSON 校验 / 推理单测 |
| S5 | 模块五：前端区域 00-05 重排 + 终端风格 + 顶栏状态 | CDP 截图 1440/390 / 无 console error |
| S6 | 构建验证：CI 全绿、部署、线上验证 | Actions / 截图 / 接口 200 |

## 七、待确认问题

1. **搜索数据（stock_names）**：D1 只保留 4 张表 → 搜索用静态 JSON（前端内置 40 只池 + `/api/search` 读常量），不再占 D1。是否同意？
2. **历史 K 线**：D1 不存 K 线；`/api/kline` 实时拉取 + 60s 缓存；训练用 Parquet。是否同意？
3. **模型分发**：训练产物 `model/latest_model.json` 提交回 GitHub 仓库，Worker 从 raw.githubusercontent 拉取并缓存（不占 D1）。是否同意（仓库公开可见模型参数）？
4. **实施顺序**：按 S1→S6 顺序（先生效缓存与接口，再改表结构，最后前端重排）。是否同意？
5. **旧接口兼容**：`/api/market`、`/api/signals` 等保留兼容期（前端切换完成后再标记 deprecated），不立即删除。是否同意？
