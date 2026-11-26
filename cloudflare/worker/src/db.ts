/**
 * D1 访问层（v2 · D1 瘦身版，迁移 0009 之后）。
 *
 * 云库只保留 4 张轻量表 + edge_cache（缓存）+ stock_names（搜索映射）：
 *   account_snapshot / positions / order_log / signal_log
 *
 * 历史 K 线/盘中/板块/市场快照/事件/回测/regime 等大表已在 0009 迁移中删除，
 * 本文件不再包含任何旧表 SQL；为兼容旧调用方保留的同名函数是**无副作用降级桩**
 * （返回空数据），真正数据源改为：/api/market-overview（实时+边缘缓存）、/api/kline（实时）等。
 */
import { SCHEMA_STATEMENTS } from "./schema.ts";

/** 每个 isolate 只跑一次建表（幂等 DDL，失败时不置位以便重试）。 */
let schemaReady = false;

export async function ensureSchema(db: D1Database): Promise<void> {
  if (schemaReady) return;
  for (const stmt of SCHEMA_STATEMENTS) {
    await db.prepare(stmt).run();
  }
  schemaReady = true;
}

function nowIsoUtc(): string {
  return new Date().toISOString();
}

// ---------------------------------------------------------------- signal_log ----

export interface SignalLogRow {
  id: string;
  symbol: string;
  name: string | null;
  trade_date: string;
  horizon: string;
  prob_up: number | null;
  prob_flat: number | null;
  prob_down: number | null;
  confidence: number | null;
  direction: string | null;
  rank_score: number | null;
  top_features: string | null;
  model_version: string | null;
  expires_at: string | null;
  created_at: string;
}

export interface SignalLogItem {
  signal_id: string;
  symbol: string;
  name: string;
  asof: string;
  trade_date: string;
  price: number | null;
  direction: string;
  prob: { up: number; flat: number; down: number };
  confidence: number;
  horizon: string;
  expires_at: string | null;
  model_version: string | null;
  status: string;
  top_features: Array<{ name: string; value: number; contrib: number }>;
  source: string;
  created_at: string;
}

function safeTopFeatures(raw: string | null): Array<{ name: string; value: number; contrib: number }> {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (f): f is { name: string; value: number; contrib: number } =>
        Boolean(f) && typeof f === "object" && typeof (f as { name?: unknown }).name === "string",
    );
  } catch {
    return [];
  }
}

export function rowToSignalItem(row: SignalLogRow): SignalLogItem {
  const prob = {
    up: row.prob_up ?? 0,
    flat: row.prob_flat ?? 0,
    down: row.prob_down ?? 0,
  };
  const sum = prob.up + prob.flat + prob.down;
  if (sum > 0) {
    prob.up = Math.round((prob.up / sum) * 10000) / 10000;
    prob.flat = Math.round((prob.flat / sum) * 10000) / 10000;
    prob.down = Math.round((prob.down / sum) * 10000) / 10000;
  }
  const direction = row.direction && ["up", "flat", "down"].includes(row.direction) ? row.direction : "flat";
  return {
    signal_id: row.id,
    symbol: row.symbol,
    name: row.name ?? "",
    asof: row.created_at,
    trade_date: row.trade_date,
    price: null,
    direction,
    prob,
    confidence: row.confidence ?? Math.max(prob.up, prob.flat, prob.down),
    horizon: row.horizon ?? "1d",
    expires_at: row.expires_at,
    model_version: row.model_version,
    status: "ok",
    top_features: safeTopFeatures(row.top_features),
    source: "cloud",
    created_at: row.created_at,
  };
}

/** 批量 UPSERT signal_log（幂等；id 冲突覆盖）。 */
export async function upsertSignalLog(db: D1Database, items: SignalLogRow[]): Promise<number> {
  if (items.length === 0) return 0;
  await ensureSchema(db);
  const stmts = items.map((item) =>
    db
      .prepare(
        `INSERT INTO signal_log (id, symbol, name, trade_date, horizon, prob_up, prob_flat, prob_down,
           confidence, direction, rank_score, top_features, model_version, expires_at, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET name = excluded.name, trade_date = excluded.trade_date,
           horizon = excluded.horizon, prob_up = excluded.prob_up, prob_flat = excluded.prob_flat,
           prob_down = excluded.prob_down, confidence = excluded.confidence, direction = excluded.direction,
           rank_score = excluded.rank_score, top_features = excluded.top_features,
           model_version = excluded.model_version, expires_at = excluded.expires_at`,
      )
      .bind(
        item.id,
        item.symbol,
        item.name,
        item.trade_date,
        item.horizon,
        item.prob_up,
        item.prob_flat,
        item.prob_down,
        item.confidence,
        item.direction,
        item.rank_score,
        item.top_features,
        item.model_version,
        item.expires_at,
        item.created_at,
      ),
  );
  for (let i = 0; i < stmts.length; i += 40) await db.batch(stmts.slice(i, i + 40));
  return stmts.length;
}

export interface QuerySignalLogOptions {
  limit?: number;
  symbols?: string[];
  latest?: boolean;
}

export async function querySignalLog(
  db: D1Database,
  options: QuerySignalLogOptions = {},
): Promise<{ items: SignalLogItem[]; updated_at: string | null }> {
  await ensureSchema(db);
  const limit = Math.min(Math.max(Math.trunc(options.limit ?? 200), 1), 500);
  const symbols = (options.symbols ?? []).filter((s) => /^\d{6}$/.test(s));
  const where: string[] = [];
  const params: unknown[] = [];
  if (symbols.length > 0) {
    where.push(`symbol IN (${symbols.map(() => "?").join(",")})`);
    params.push(...symbols);
  }
  const whereSql = where.length > 0 ? `WHERE ${where.join(" AND ")}` : "";
  const sql = options.latest
    ? `SELECT * FROM (
         SELECT s.*, ROW_NUMBER() OVER (PARTITION BY s.symbol ORDER BY s.trade_date DESC, s.created_at DESC) AS rn
         FROM signal_log s ${whereSql}
       ) WHERE rn = 1 ORDER BY trade_date DESC, created_at DESC LIMIT ?`
    : `SELECT * FROM signal_log ${whereSql} ORDER BY trade_date DESC, created_at DESC LIMIT ?`;
  params.push(limit);
  const rows = await db.prepare(sql).bind(...params).all<SignalLogRow>();
  const meta = await db.prepare(`SELECT MAX(created_at) AS updated_at FROM signal_log`).first<{ updated_at: string | null }>();
  return { items: (rows.results ?? []).map(rowToSignalItem), updated_at: meta?.updated_at ?? null };
}

// ---------------------------------------------------------- account_snapshot ----

export interface AccountSnapshotRow {
  asof: string;
  total_asset: number;
  cash: number;
  market_value: number;
  today_pnl: number;
  total_pnl: number;
  position_count: number;
  source: string;
  created_at: string;
}

export async function upsertAccountSnapshot(db: D1Database, row: AccountSnapshotRow): Promise<void> {
  await ensureSchema(db);
  await db
    .prepare(
      `INSERT INTO account_snapshot (asof, total_asset, cash, market_value, today_pnl, total_pnl, position_count, source, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(asof) DO UPDATE SET total_asset = excluded.total_asset, cash = excluded.cash,
         market_value = excluded.market_value, today_pnl = excluded.today_pnl, total_pnl = excluded.total_pnl,
         position_count = excluded.position_count, source = excluded.source, created_at = excluded.created_at`,
    )
    .bind(row.asof, row.total_asset, row.cash, row.market_value, row.today_pnl, row.total_pnl, row.position_count, row.source, row.created_at)
    .run();
}

export async function queryLatestAccount(db: D1Database): Promise<AccountSnapshotRow | null> {
  await ensureSchema(db);
  const row = await db
    .prepare(`SELECT * FROM account_snapshot ORDER BY asof DESC LIMIT 1`)
    .first<AccountSnapshotRow>();
  return row ?? null;
}

// ---------------------------------------------------------------- positions ----

export interface PositionRow {
  symbol: string;
  name: string | null;
  qty: number;
  cost_price: number;
  last_price: number | null;
  market_value: number | null;
  pnl: number | null;
  pnl_pct: number | null;
  asof: string;
  updated_at: string;
}

/** 整体替换当前持仓快照（先删后插同一 batch，保证一致性）。 */
export async function replacePositions(db: D1Database, rows: PositionRow[]): Promise<number> {
  await ensureSchema(db);
  const stmts: D1PreparedStatement[] = [db.prepare(`DELETE FROM positions`)];
  for (const row of rows) {
    stmts.push(
      db
        .prepare(
          `INSERT INTO positions (symbol, name, qty, cost_price, last_price, market_value, pnl, pnl_pct, asof, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .bind(row.symbol, row.name, row.qty, row.cost_price, row.last_price, row.market_value, row.pnl, row.pnl_pct, row.asof, row.updated_at),
    );
  }
  for (let i = 0; i < stmts.length; i += 40) await db.batch(stmts.slice(i, i + 40));
  return rows.length;
}

export async function queryPositions(db: D1Database): Promise<PositionRow[]> {
  await ensureSchema(db);
  const rows = await db.prepare(`SELECT * FROM positions ORDER BY symbol ASC`).all<PositionRow>();
  return rows.results ?? [];
}

// ---------------------------------------------------------------- order_log ----

export interface OrderLogRow {
  id: string;
  symbol: string;
  name: string | null;
  side: string;
  qty: number;
  target_price: number | null;
  filled_price: number | null;
  commission: number;
  stamp_tax: number;
  transfer_fee: number;
  fee_total: number;
  status: string;
  reason: string | null;
  trade_date: string | null;
  created_at: string;
}

export async function upsertOrders(db: D1Database, rows: OrderLogRow[]): Promise<number> {
  if (rows.length === 0) return 0;
  await ensureSchema(db);
  const stmts = rows.map((row) =>
    db
      .prepare(
        `INSERT INTO order_log (id, symbol, name, side, qty, target_price, filled_price, commission, stamp_tax,
           transfer_fee, fee_total, status, reason, trade_date, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET filled_price = excluded.filled_price, commission = excluded.commission,
           stamp_tax = excluded.stamp_tax, transfer_fee = excluded.transfer_fee, fee_total = excluded.fee_total,
           status = excluded.status, reason = excluded.reason`,
      )
      .bind(
        row.id,
        row.symbol,
        row.name,
        row.side,
        row.qty,
        row.target_price,
        row.filled_price,
        row.commission,
        row.stamp_tax,
        row.transfer_fee,
        row.fee_total,
        row.status,
        row.reason,
        row.trade_date,
        row.created_at,
      ),
  );
  for (let i = 0; i < stmts.length; i += 40) await db.batch(stmts.slice(i, i + 40));
  return stmts.length;
}

export async function queryOrders(db: D1Database, limit = 50): Promise<OrderLogRow[]> {
  await ensureSchema(db);
  const cap = Math.min(Math.max(Math.trunc(limit), 1), 200);
  const rows = await db.prepare(`SELECT * FROM order_log ORDER BY created_at DESC LIMIT ?`).bind(cap).all<OrderLogRow>();
  return rows.results ?? [];
}

// --------------------------------------------------------------- stock_names ----

export async function upsertStockNames(db: D1Database, rows: Array<{ symbol: string; name: string }>): Promise<number> {
  if (rows.length === 0) return 0;
  await ensureSchema(db);
  const now = nowIsoUtc();
  const stmts = rows
    .filter((r) => /^\d{6}$/.test(r.symbol) && r.name)
    .map((r) =>
      db
        .prepare(
          `INSERT INTO stock_names (symbol, name, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at`,
        )
        .bind(r.symbol, r.name, now),
    );
  if (stmts.length === 0) return 0;
  for (let i = 0; i < stmts.length; i += 40) await db.batch(stmts.slice(i, i + 40));
  return stmts.length;
}

export async function queryStockName(db: D1Database, symbol: string): Promise<string | null> {
  await ensureSchema(db);
  const row = await db.prepare(`SELECT name FROM stock_names WHERE symbol = ?`).bind(symbol).first<{ name: string }>();
  return row?.name ?? null;
}

/** 股票池（stock_names 表；供搜索/兼容查询）。 */
export async function queryStockSymbols(db: D1Database, limit = 40): Promise<string[]> {
  await ensureSchema(db);
  const cap = Math.min(Math.max(Math.trunc(limit), 1), 500);
  const rows = await db.prepare(`SELECT symbol FROM stock_names ORDER BY symbol ASC LIMIT ?`).bind(cap).all<{ symbol: string }>();
  return (rows.results ?? []).map((r) => r.symbol).filter((s) => /^\d{6}$/.test(s));
}

// ---------------------------------------------------------------- health ----

export interface D1Health {
  status: "ok" | "error";
  signals?: number;
  updated_at?: string | null;
  error?: string;
}

export async function d1Health(db: D1Database | undefined): Promise<D1Health> {
  if (!db) return { status: "error", error: "D1 binding 未配置（检查 wrangler.toml）" };
  try {
    await ensureSchema(db);
    const row = await db
      .prepare(`SELECT COUNT(*) AS n, MAX(created_at) AS updated_at FROM signal_log`)
      .first<{ n: number; updated_at: string | null }>();
    return { status: "ok", signals: Number(row?.n ?? 0), updated_at: row?.updated_at ?? null };
  } catch (err) {
    return { status: "error", error: err instanceof Error ? err.message : String(err) };
  }
}

// ------------------------------------------------- 旧接口兼容降级桩（不碰旧表） ----
// 0009 迁移已删除旧大表；以下同名函数保留只为旧调用方编译/运行不崩，
// 返回空数据，调用方应改走 /api/market-overview、/api/kline 实时接口。

export type SnapshotTable = "backtest_reports" | "paper_snapshots" | "regime_snapshots";

export async function insertMarketSnapshot(_db: D1Database, snapshot: Record<string, unknown>): Promise<{ asof: string }> {
  return { asof: typeof snapshot["asof"] === "string" ? (snapshot["asof"] as string) : nowIsoUtc() };
}
export async function queryLatestMarket(_db: D1Database): Promise<{ snapshot: Record<string, unknown>; updated_at: string } | null> {
  return null;
}
export async function insertJsonSnapshot(_db: D1Database, _table: SnapshotTable, payload: Record<string, unknown>): Promise<{ asof: string }> {
  return { asof: typeof payload["asof"] === "string" ? (payload["asof"] as string) : nowIsoUtc() };
}
export async function queryLatestJsonSnapshot(_db: D1Database, _table: SnapshotTable): Promise<Record<string, unknown> | null> {
  return null;
}
export async function upsertEvents(_db: D1Database, _items: Array<Record<string, unknown>>): Promise<number> {
  return 0;
}
export async function queryEvents(_db: D1Database, _limit = 60): Promise<Array<Record<string, unknown>>> {
  return [];
}
export interface KlineRow {
  symbol: string;
  trade_date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  amount: number | null;
}
export async function upsertKlines(_db: D1Database, _rows: KlineRow[]): Promise<number> {
  return 0;
}
export async function queryKlines(_db: D1Database, _symbol: string, _limit: number): Promise<KlineRow[]> {
  return [];
}
export interface BoardRow {
  board_name: string;
  type: string;
  pct_1d: number | null;
  pct_5d: number | null;
  net_inflow: number | null;
  updated_at: string;
}
export async function upsertBoards(_db: D1Database, _rows: BoardRow[]): Promise<number> {
  return 0;
}
export async function queryBoards(_db: D1Database, _options: { type?: string; limit?: number } = {}): Promise<BoardRow[]> {
  return [];
}
export interface MarketSummaryRow {
  trade_date: string;
  session: string;
  asof: string;
  summary_text: string;
  payload: string;
  created_at: string;
}
export async function upsertIntradayIndex(_db: D1Database, _code: string, _tradeDate: string, _payload: Record<string, unknown>, _updatedAt: string): Promise<void> {
  return;
}
export async function queryIntradayIndex(_db: D1Database, _code: string): Promise<{ code: string; trade_date: string; updated_at: string; payload: Record<string, unknown> } | null> {
  return null;
}
export async function upsertMarketSummary(_db: D1Database, _row: Omit<MarketSummaryRow, "payload"> & { payload: Record<string, unknown> }): Promise<void> {
  return;
}
export async function queryLatestSummaries(_db: D1Database): Promise<{ midday: Record<string, unknown> | null; close: Record<string, unknown> | null }> {
  return { midday: null, close: null };
}
export async function queryMarketSummaries(_db: D1Database, _limit = 20): Promise<Array<Record<string, unknown>>> {
  return [];
}
export async function queryMarketHistory(_db: D1Database, _date: string, _limit = 120): Promise<Array<{ asof: string; trade_date: string | null; payload: Record<string, unknown> }>> {
  return [];
}
