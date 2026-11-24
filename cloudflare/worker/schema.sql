-- 个人量化工作台 · Cloudflare D1 schema
-- 契约 §3.3 signals 表（少 paper_* 内部字段），与本地 SQLite 同名表字段一致。
-- 手工建表（本地）：npx wrangler d1 execute personal_quant --local --file=schema.sql
-- 或迁移（本地）：npx wrangler d1 migrations apply personal_quant --local
-- 真实部署：把本文件内容 apply 到远端 D1（见 cloudflare/README.md），不要直接 deploy 前忘建表。

CREATE TABLE IF NOT EXISTS signals (
  signal_id     TEXT PRIMARY KEY,
  symbol        TEXT NOT NULL,
  asof          TEXT NOT NULL,
  trade_date    TEXT NOT NULL,
  price         REAL NOT NULL,
  direction     TEXT NOT NULL CHECK (direction IN ('up', 'flat', 'down')),
  prob_up       REAL NOT NULL,
  prob_flat     REAL NOT NULL,
  prob_down     REAL NOT NULL,
  confidence    REAL NOT NULL,
  horizon       TEXT NOT NULL DEFAULT '1d',
  expires_at    TEXT NOT NULL,
  model_version TEXT NOT NULL,
  top_features  TEXT NOT NULL DEFAULT '[]',
  source        TEXT NOT NULL DEFAULT 'local' CHECK (source IN ('local', 'cloud')),
  status        TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'stale', 'warming_up')),
  created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_asof ON signals (symbol, asof DESC);
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_trade_date ON signals (trade_date DESC);

CREATE TABLE IF NOT EXISTS market_snapshots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  asof       TEXT NOT NULL,
  trade_date TEXT,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_asof ON market_snapshots (asof DESC);

CREATE TABLE IF NOT EXISTS stock_names (
  symbol     TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id           TEXT PRIMARY KEY,
  published_at TEXT NOT NULL,
  source       TEXT,
  title        TEXT NOT NULL,
  sentiment    TEXT,
  score        REAL,
  payload      TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_published ON events (published_at DESC);
CREATE TABLE IF NOT EXISTS backtest_reports (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  asof       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_snapshots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  asof       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS klines (
  symbol     TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  open       REAL,
  high       REAL,
  low        REAL,
  close      REAL,
  volume     REAL,
  amount     REAL,
  PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_klines_symbol_date ON klines (symbol, trade_date DESC);
CREATE TABLE IF NOT EXISTS regime_snapshots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  asof       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regime_asof ON regime_snapshots (asof DESC);

CREATE TABLE IF NOT EXISTS boards (
  board_name TEXT NOT NULL,
  type       TEXT NOT NULL,
  pct_1d     REAL,
  pct_5d     REAL,
  net_inflow REAL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (board_name, type)
);
CREATE INDEX IF NOT EXISTS idx_boards_type_pct ON boards (type, pct_1d DESC);

CREATE TABLE IF NOT EXISTS intraday_index (
  code       TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  payload    TEXT NOT NULL,
  PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_intraday_index_date ON intraday_index (trade_date DESC);

CREATE TABLE IF NOT EXISTS market_summaries (
  trade_date   TEXT NOT NULL,
  session      TEXT NOT NULL,
  asof         TEXT NOT NULL,
  summary_text TEXT NOT NULL,
  payload      TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (trade_date, session)
);
CREATE INDEX IF NOT EXISTS idx_market_summaries_asof ON market_summaries (asof DESC);

