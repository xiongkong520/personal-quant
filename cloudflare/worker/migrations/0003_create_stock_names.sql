-- 股票代码 → 名称映射（供 /api/stock 查询展示）
CREATE TABLE IF NOT EXISTS stock_names (
  symbol     TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
