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
