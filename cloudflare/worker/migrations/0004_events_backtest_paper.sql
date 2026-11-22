-- 重大事件（利好/利空）+ 回测报告 + 模拟盘快照
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
