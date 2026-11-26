/**
 * D1 schema（契约 §3.3 signals 表）。
 * 与 worker/schema.sql / migrations/0001_create_signals.sql 保持一致；
 * `node scripts/check-schema.mjs` 会在测试里校验三者同步，避免改一处忘另一处。
 */
export const SCHEMA_STATEMENTS: string[] = [
  `CREATE TABLE IF NOT EXISTS signals (
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
)`,
  `CREATE INDEX IF NOT EXISTS idx_signals_symbol_asof ON signals (symbol, asof DESC)`,
  `CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals (created_at DESC)`,
  `CREATE INDEX IF NOT EXISTS idx_signals_trade_date ON signals (trade_date DESC)`,
  `CREATE TABLE IF NOT EXISTS market_snapshots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  asof       TEXT NOT NULL,
  trade_date TEXT,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
)`,
  `CREATE INDEX IF NOT EXISTS idx_market_asof ON market_snapshots (asof DESC)`,
  `CREATE TABLE IF NOT EXISTS stock_names (
  symbol TEXT PRIMARY KEY,
  name   TEXT NOT NULL,
  updated_at TEXT NOT NULL
)`,
  `CREATE TABLE IF NOT EXISTS events (
  id           TEXT PRIMARY KEY,
  published_at TEXT NOT NULL,
  source       TEXT,
  title        TEXT NOT NULL,
  sentiment    TEXT,
  score        REAL,
  payload      TEXT NOT NULL,
  created_at   TEXT NOT NULL
)`,
  `CREATE INDEX IF NOT EXISTS idx_events_published ON events (published_at DESC)`,
  `CREATE TABLE IF NOT EXISTS backtest_reports (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  asof       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
)`,
  `CREATE TABLE IF NOT EXISTS paper_snapshots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  asof       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
)`,
  `CREATE TABLE IF NOT EXISTS klines (
  symbol     TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  open       REAL,
  high       REAL,
  low        REAL,
  close      REAL,
  volume     REAL,
  amount     REAL,
  PRIMARY KEY (symbol, trade_date)
)`,
  `CREATE INDEX IF NOT EXISTS idx_klines_symbol_date ON klines (symbol, trade_date DESC)`,
  `CREATE TABLE IF NOT EXISTS regime_snapshots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  asof       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
)`,
  `CREATE INDEX IF NOT EXISTS idx_regime_asof ON regime_snapshots (asof DESC)`,
  `CREATE TABLE IF NOT EXISTS boards (
  board_name TEXT NOT NULL,
  type       TEXT NOT NULL,
  pct_1d     REAL,
  pct_5d     REAL,
  net_inflow REAL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (board_name, type)
)`,
  `CREATE INDEX IF NOT EXISTS idx_boards_type_pct ON boards (type, pct_1d DESC)`,
  `CREATE TABLE IF NOT EXISTS intraday_index (
  code       TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  payload    TEXT NOT NULL,
  PRIMARY KEY (code, trade_date)
)`,
  `CREATE INDEX IF NOT EXISTS idx_intraday_index_date ON intraday_index (trade_date DESC)`,
  `CREATE TABLE IF NOT EXISTS market_summaries (
  trade_date   TEXT NOT NULL,
  session      TEXT NOT NULL,
  asof         TEXT NOT NULL,
  summary_text TEXT NOT NULL,
  payload      TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (trade_date, session)
)`,
  `CREATE INDEX IF NOT EXISTS idx_market_summaries_asof ON market_summaries (asof DESC)`,
  `CREATE TABLE IF NOT EXISTS edge_cache (
  cache_key  TEXT PRIMARY KEY,
  payload    TEXT NOT NULL,
  cached_at  INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
)`,
  `CREATE INDEX IF NOT EXISTS idx_edge_cache_expires ON edge_cache (expires_at)`,
  `CREATE TABLE IF NOT EXISTS account_snapshot (
  asof          TEXT PRIMARY KEY,
  total_asset   REAL NOT NULL,
  cash          REAL NOT NULL,
  market_value  REAL NOT NULL,
  today_pnl     REAL NOT NULL,
  total_pnl     REAL NOT NULL,
  position_count INTEGER NOT NULL DEFAULT 0,
  source        TEXT NOT NULL DEFAULT 'paper',
  created_at    TEXT NOT NULL
)`,
  `CREATE TABLE IF NOT EXISTS positions (
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
)`,
  `CREATE TABLE IF NOT EXISTS order_log (
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
)`,
  `CREATE INDEX IF NOT EXISTS idx_order_log_created ON order_log (created_at DESC)`,
  `CREATE TABLE IF NOT EXISTS signal_log (
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
)`,
  `CREATE INDEX IF NOT EXISTS idx_signal_log_date ON signal_log (trade_date DESC, rank_score DESC)`,
  `DROP TABLE IF EXISTS klines`,
  `DROP TABLE IF EXISTS intraday_index`,
  `DROP TABLE IF EXISTS boards`,
  `DROP TABLE IF EXISTS market_snapshots`,
  `DROP TABLE IF EXISTS events`,
  `DROP TABLE IF EXISTS backtest_reports`,
  `DROP TABLE IF EXISTS paper_snapshots`,
  `DROP TABLE IF EXISTS regime_snapshots`,
  `DROP TABLE IF EXISTS market_summaries`,
  `DROP TABLE IF EXISTS signals`,
];

export const SCHEMA_SQL = `${SCHEMA_STATEMENTS.join(";\n\n")};\n`;

/** signals 表字段（契约 §3.3），供测试与文档引用。 */
export const SIGNALS_COLUMNS = [
  "signal_id",
  "symbol",
  "asof",
  "trade_date",
  "price",
  "direction",
  "prob_up",
  "prob_flat",
  "prob_down",
  "confidence",
  "horizon",
  "expires_at",
  "model_version",
  "top_features",
  "source",
  "status",
  "created_at",
] as const;
