-- 本地演示数据（D1 瘦身后：signal_log + 模拟盘轻量表）。
-- 幂等：INSERT OR REPLACE；重复执行不报错、不翻倍。
INSERT OR REPLACE INTO signal_log (
  id, symbol, name, trade_date, horizon, prob_up, prob_flat, prob_down,
  confidence, direction, rank_score, top_features, model_version, expires_at, created_at
) VALUES
('600519-2026-11-20-v0.1.0', '600519', '贵州茅台', '2026-11-20', '1d', 0.4135, 0.3128, 0.2737,
 0.4135, 'up', 0.4135,
 '[{"name":"ret_5d","value":0.031,"contrib":0.0821},{"name":"ma20_bias","value":0.012,"contrib":-0.0413},{"name":"vol_ratio","value":1.62,"contrib":0.0276},{"name":"rsi14","value":58.2,"contrib":0.0194},{"name":"macd_hist","value":0.012,"contrib":0.0112}]',
 'v0.1.0', '2026-11-23T09:30:00+08:00', '2026-11-20T14:35:06+08:00'),
('000001-2026-11-20-v0.1.0', '000001', '平安银行', '2026-11-20', '1d', 0.3021, 0.3712, 0.3267,
 0.3712, 'flat', 0.3712,
 '[{"name":"ret_1d","value":-0.0021,"contrib":-0.0152},{"name":"turnover_rate","value":0.87,"contrib":0.0093}]',
 'v0.1.0', '2026-11-23T09:30:00+08:00', '2026-11-20T14:36:05+08:00'),
('300750-2026-11-19-v0.1.0', '300750', '宁德时代', '2026-11-19', '1d', 0.2588, 0.3087, 0.4325,
 0.4325, 'down', 0.4325,
 '[{"name":"ret_20d","value":-0.083,"contrib":-0.0624},{"name":"ma5_bias","value":-0.021,"contrib":-0.0357},{"name":"rsi14","value":32.4,"contrib":-0.0188}]',
 'v0.1.0', '2026-11-20T09:30:00+08:00', '2026-11-19T15:02:00+08:00'),
('600519-2026-11-21-v0.1.0', '600519', '贵州茅台', '2026-11-21', '1d', 0.3417, 0.3425, 0.3158,
 0.3425, 'flat', 0.3425, '[]', 'v0.1.0', '2026-11-24T09:30:00+08:00', '2026-11-21T10:30:04+08:00');

INSERT OR REPLACE INTO account_snapshot (
  asof, total_asset, cash, market_value, today_pnl, total_pnl, position_count, source, created_at
) VALUES ('2026-11-25T15:00:00+08:00', 1000000, 800000, 200000, 1200, 5000, 1, 'paper', '2026-11-25T15:00:05Z');

INSERT OR REPLACE INTO positions (
  symbol, name, qty, cost_price, last_price, market_value, pnl, pnl_pct, asof, updated_at
) VALUES ('600519', '贵州茅台', 100, 1600.0, 1680.5, 168050, 8050, 0.0503, '2026-11-25T15:00:00+08:00', '2026-11-25T15:00:05Z');

INSERT OR REPLACE INTO order_log (
  id, symbol, name, side, qty, target_price, filled_price, commission, stamp_tax,
  transfer_fee, fee_total, status, reason, trade_date, created_at
) VALUES ('ord-demo-0001', '600519', '贵州茅台', 'buy', 100, 1600.0, 1600.0, 5.0, 0, 1.6,
 6.6, 'filled', NULL, '2026-11-20', '2026-11-20T09:31:02Z');
