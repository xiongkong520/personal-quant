-- 边缘缓存 D1 二级存储：Cloudflare 限制 *.workers.dev 上 Cache API 操作 no impact，
-- 用 D1 表保证 workers.dev 也能 60s 快照 MISS→HIT；自定义域名接入后 Cache API 优先、此表兜底。
CREATE TABLE IF NOT EXISTS edge_cache (
  cache_key  TEXT PRIMARY KEY,
  payload    TEXT NOT NULL,
  cached_at  INTEGER NOT NULL,   -- epoch ms
  expires_at INTEGER NOT NULL    -- epoch ms（fresh=ttl，lkg=24h）
);
CREATE INDEX IF NOT EXISTS idx_edge_cache_expires ON edge_cache (expires_at);
