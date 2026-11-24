-- 盘中指数分时（东财 trends2，每 5 分钟覆盖写当日分钟序列）+ 午评/收评
CREATE TABLE IF NOT EXISTS intraday_index (
  code       TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  payload    TEXT NOT NULL,            -- {code, name, trade_date, pre_close, points:[{time,price,avg_price,volume}]}
  PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_intraday_index_date ON intraday_index (trade_date DESC);

CREATE TABLE IF NOT EXISTS market_summaries (
  trade_date   TEXT NOT NULL,
  session      TEXT NOT NULL,          -- midday / close
  asof         TEXT NOT NULL,
  summary_text TEXT NOT NULL,
  payload      TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (trade_date, session)
);
CREATE INDEX IF NOT EXISTS idx_market_summaries_asof ON market_summaries (asof DESC);
