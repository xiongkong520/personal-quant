-- 板块行情（Worker Cron 收盘同步写入；简化 regime 主线 Top5 的数据来源）
-- 数据源：东财 push2 clist（行业 m:90 t:2 / 概念 m:90 t:3），
--         Worker 端不可达时降级为新浪行业（GBK，TextDecoder 解码），再失败则跳过并记录 errors。
CREATE TABLE IF NOT EXISTS boards (
  board_name  TEXT NOT NULL,
  type        TEXT NOT NULL,           -- industry / concept
  pct_1d      REAL,
  pct_5d      REAL,
  net_inflow  REAL,                    -- 亿元（东财返回元，写入前 /1e8）
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (board_name, type)
);
CREATE INDEX IF NOT EXISTS idx_boards_type_pct ON boards (type, pct_1d DESC);
