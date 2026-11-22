-- 历史 K 线（个股日线，供看板 Canvas K 线图）+ 大盘状态快照（状态/情绪/主线/仓位建议）
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
