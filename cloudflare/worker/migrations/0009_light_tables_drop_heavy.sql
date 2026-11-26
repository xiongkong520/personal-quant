-- D1 瘦身（用户明确要求）：云库只保留 4 张轻量表 + edge_cache 缓存表（+ stock_names 搜索映射）。
-- 历史 K 线/盘中/快照/事件/回测/模拟盘快照等大表全部删除；历史行情改本地 Parquet，
-- 快照/事件/板块/regime 改 Worker 边缘缓存（Cache API + edge_cache 二级）。
--
-- 4 张轻量表（与 docs/UPGRADE_PLAN.md §二 一致）：
CREATE TABLE IF NOT EXISTS account_snapshot (
  asof          TEXT PRIMARY KEY,
  total_asset   REAL NOT NULL,
  cash          REAL NOT NULL,
  market_value  REAL NOT NULL,
  today_pnl     REAL NOT NULL,
  total_pnl     REAL NOT NULL,
  position_count INTEGER NOT NULL DEFAULT 0,
  source        TEXT NOT NULL DEFAULT 'paper',
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
  symbol      TEXT PRIMARY KEY,
  name        TEXT,
  qty         INTEGER NOT NULL,
  cost_price  REAL NOT NULL,
  last_price  REAL,
  market_value REAL,
  pnl         REAL,
  pnl_pct     REAL,
  asof        TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_log (
  id           TEXT PRIMARY KEY,
  symbol       TEXT NOT NULL,
  name         TEXT,
  side         TEXT NOT NULL,
  qty          INTEGER NOT NULL,
  target_price REAL,
  filled_price REAL,
  commission   REAL NOT NULL DEFAULT 0,
  stamp_tax    REAL NOT NULL DEFAULT 0,
  transfer_fee REAL NOT NULL DEFAULT 0,
  fee_total    REAL NOT NULL DEFAULT 0,
  status       TEXT NOT NULL,
  reason       TEXT,
  trade_date   TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_log_created ON order_log (created_at DESC);

CREATE TABLE IF NOT EXISTS signal_log (
  id           TEXT PRIMARY KEY,
  symbol       TEXT NOT NULL,
  name         TEXT,
  trade_date   TEXT NOT NULL,
  horizon      TEXT NOT NULL DEFAULT '1d',
  prob_up      REAL,
  prob_flat    REAL,
  prob_down    REAL,
  confidence   REAL,
  direction    TEXT,
  rank_score   REAL,
  top_features TEXT,
  model_version TEXT,
  expires_at   TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signal_log_date ON signal_log (trade_date DESC, rank_score DESC);

-- 删除历史大表（与 UPGRADE_PLAN §1.3 一致；stock_names / edge_cache 保留）
DROP TABLE IF EXISTS klines;
DROP TABLE IF EXISTS intraday_index;
DROP TABLE IF EXISTS boards;
DROP TABLE IF EXISTS market_snapshots;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS backtest_reports;
DROP TABLE IF EXISTS paper_snapshots;
DROP TABLE IF EXISTS regime_snapshots;
DROP TABLE IF EXISTS market_summaries;
DROP TABLE IF EXISTS signals;
