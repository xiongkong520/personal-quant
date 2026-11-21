-- 市场整体快照（指数/涨跌家数/情绪分/冰点），由本地 `python -m quant.cli market` + export 推送
CREATE TABLE IF NOT EXISTS market_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asof TEXT NOT NULL,
  trade_date TEXT,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_asof ON market_snapshots (asof DESC);
