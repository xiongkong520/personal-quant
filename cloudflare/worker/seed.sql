-- 本地演示数据（3 条信号，覆盖 up/flat/down 与 stale/warming_up 状态），方便前端联调。
-- 用法（需要本地 D1 已建表）：
--   npx wrangler d1 execute personal_quant --local --file=seed.sql
--   npm run db:seed:local        # 等价封装
-- 幂等：INSERT OR REPLACE，同一 signal_id 重复执行不会报错。
-- 注意：信号内容是演示用构造数据，不是真实市场观点。

INSERT OR REPLACE INTO signals (
  signal_id, symbol, asof, trade_date, price, direction,
  prob_up, prob_flat, prob_down, confidence, horizon, expires_at,
  model_version, top_features, source, status, created_at
) VALUES
(
  '600519-2026-11-20-20261120143500', '600519', '2026-11-20T14:35:00+08:00', '2026-11-20', 1680.50, 'up',
  0.4135, 0.3128, 0.2737, 0.4135, '1d', '2026-11-23T09:30:00+08:00',
  'v0.1.0-fixture',
  '[{"name":"ret_5d","value":0.031,"contrib":0.0821},{"name":"ma20_bias","value":0.012,"contrib":-0.0413},{"name":"vol_ratio","value":1.62,"contrib":0.0276},{"name":"rsi14","value":58.2,"contrib":0.0194},{"name":"macd_hist","value":0.012,"contrib":0.0112}]',
  'local', 'ok', '2026-11-20T14:35:06+08:00'
),
(
  '000001-2026-11-20-20261120143600', '000001', '2026-11-20T14:36:00+08:00', '2026-11-20', 11.28, 'flat',
  0.3021, 0.3712, 0.3267, 0.3712, '1d', '2026-11-23T09:30:00+08:00',
  'v0.1.0-fixture',
  '[{"name":"ret_1d","value":-0.0021,"contrib":-0.0152},{"name":"turnover_rate","value":0.87,"contrib":0.0093}]',
  'local', 'ok', '2026-11-20T14:36:05+08:00'
),
(
  '300750-2026-11-19-20261119143500', '300750', '2026-11-19T14:35:00+08:00', '2026-11-19', 241.60, 'down',
  0.2588, 0.3087, 0.4325, 0.4325, '1d', '2026-11-20T09:30:00+08:00',
  'v0.1.0-fixture',
  '[{"name":"ret_20d","value":-0.083,"contrib":-0.0624},{"name":"ma5_bias","value":-0.021,"contrib":-0.0357},{"name":"rsi14","value":32.4,"contrib":-0.0188}]',
  'local', 'stale', '2026-11-19T15:02:00+08:00'
);

-- 一条 warming_up 示例（数据不足时 status=warming_up、direction 仍为 flat，§4 约束）
INSERT OR REPLACE INTO signals (
  signal_id, symbol, asof, trade_date, price, direction,
  prob_up, prob_flat, prob_down, confidence, horizon, expires_at,
  model_version, top_features, source, status, created_at
) VALUES (
  '000001-2026-11-21-20261121103000', '000001', '2026-11-21T10:30:00+08:00', '2026-11-21', 11.31, 'flat',
  0.3417, 0.3425, 0.3158, 0.3425, '1d', '2026-11-23T09:30:00+08:00',
  'v0.1.0-fixture', '[]', 'local', 'warming_up', '2026-11-21T10:30:04+08:00'
);
