/**
 * D1 访问层：建表、UPSERT 信号、查询、健康检查。
 * 只依赖 D1 binding 的 prepare/bind/all/first/run/batch/exec 接口，
 * 因此同一份 Worker 代码也能被 scripts/dev-server.mjs 用 node:sqlite 实现本地验证。
 */
import { SCHEMA_STATEMENTS } from "./schema.ts";
import type { SignalItem, SignalRow, TopFeature } from "./types.ts";

/** 每个 isolate 只跑一次建表（幂等 DDL，失败时不置位以便重试）。 */
let schemaReady = false;

export async function ensureSchema(db: D1Database): Promise<void> {
  if (schemaReady) return;
  for (const stmt of SCHEMA_STATEMENTS) {
    await db.prepare(stmt).run();
  }
  schemaReady = true;
}

const INSERT_SQL = `INSERT INTO signals (
  signal_id, symbol, asof, trade_date, price, direction,
  prob_up, prob_flat, prob_down, confidence, horizon, expires_at,
  model_version, top_features, source, status, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(signal_id) DO UPDATE SET
  symbol = excluded.symbol,
  asof = excluded.asof,
  trade_date = excluded.trade_date,
  price = excluded.price,
  direction = excluded.direction,
  prob_up = excluded.prob_up,
  prob_flat = excluded.prob_flat,
  prob_down = excluded.prob_down,
  confidence = excluded.confidence,
  horizon = excluded.horizon,
  expires_at = excluded.expires_at,
  model_version = excluded.model_version,
  top_features = excluded.top_features,
  source = excluded.source,
  status = excluded.status,
  created_at = excluded.created_at`;

const INSERT_CHUNK = 40; // D1 batch 上限内留余量

export interface QuerySignalsOptions {
  limit?: number;
  symbols?: string[];
  latest?: boolean;
  status?: string;
}

export interface QuerySignalsResult {
  items: SignalItem[];
  updated_at: string | null;
}

function safeTopFeatures(raw: string): TopFeature[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (f): f is TopFeature =>
        Boolean(f) && typeof f === "object" && typeof (f as TopFeature).name === "string" && Number.isFinite((f as TopFeature).value) && Number.isFinite((f as TopFeature).contrib),
    );
  } catch {
    return [];
  }
}

/** D1 row -> 契约 §4 信号 JSON。 */
export function rowToSignalItem(row: SignalRow): SignalItem {
  return {
    signal_id: row.signal_id,
    symbol: row.symbol,
    asof: row.asof,
    trade_date: row.trade_date,
    price: row.price,
    direction: row.direction,
    prob: { up: row.prob_up, flat: row.prob_flat, down: row.prob_down },
    confidence: row.confidence,
    horizon: row.horizon,
    expires_at: row.expires_at,
    model_version: row.model_version,
    status: row.status,
    top_features: safeTopFeatures(row.top_features),
    source: row.source,
    created_at: row.created_at,
  };
}

/** GET /api/signals 的数据来源。updated_at 恒为全表 max(created_at)。 */
export async function querySignals(db: D1Database, options: QuerySignalsOptions = {}): Promise<QuerySignalsResult> {
  await ensureSchema(db);
  const limit = Math.min(Math.max(Math.trunc(options.limit ?? 200), 1), 500);
  const where: string[] = [];
  const params: unknown[] = [];

  const symbols = (options.symbols ?? []).filter((s) => /^\d{6}$/.test(s));
  if (symbols.length > 0) {
    where.push(`symbol IN (${symbols.map(() => "?").join(",")})`);
    params.push(...symbols);
  }
  if (options.status) {
    where.push(`status = ?`);
    params.push(options.status);
  }
  const whereSql = where.length > 0 ? `WHERE ${where.join(" AND ")}` : "";

  let sql: string;
  if (options.latest) {
    sql = `SELECT * FROM (
      SELECT s.*, ROW_NUMBER() OVER (PARTITION BY s.symbol ORDER BY s.asof DESC, s.created_at DESC) AS rn
      FROM signals s ${whereSql}
    ) WHERE rn = 1 ORDER BY asof DESC, created_at DESC LIMIT ?`;
  } else {
    sql = `SELECT * FROM signals ${whereSql} ORDER BY asof DESC, created_at DESC LIMIT ?`;
  }
  params.push(limit);

  const result = await db.prepare(sql).bind(...params).all<SignalRow>();
  const rows = (result.results ?? []) as SignalRow[];
  const meta = await db.prepare(`SELECT MAX(created_at) AS updated_at FROM signals`).first<{ updated_at: string | null }>();
  return { items: rows.map(rowToSignalItem), updated_at: meta?.updated_at ?? null };
}

export interface InsertSignalsResult {
  accepted: number;
  updated_at: string | null;
}

/** 幂等 UPSERT（signal_id 冲突则更新），返回本次写入条数与最大 created_at。 */
export async function insertSignals(db: D1Database, rows: SignalRow[]): Promise<InsertSignalsResult> {
  if (rows.length === 0) return { accepted: 0, updated_at: null };
  await ensureSchema(db);
  const statements: D1PreparedStatement[] = [];
  for (const r of rows) {
    statements.push(
      db
        .prepare(INSERT_SQL)
        .bind(
          r.signal_id,
          r.symbol,
          r.asof,
          r.trade_date,
          r.price,
          r.direction,
          r.prob_up,
          r.prob_flat,
          r.prob_down,
          r.confidence,
          r.horizon,
          r.expires_at,
          r.model_version,
          r.top_features,
          r.source,
          r.status,
          r.created_at,
        ),
    );
  }
  for (let i = 0; i < statements.length; i += INSERT_CHUNK) {
    await db.batch(statements.slice(i, i + INSERT_CHUNK));
  }
  const updatedAt = rows.reduce<string | null>((max, r) => (max === null || r.created_at > max ? r.created_at : max), null);
  return { accepted: rows.length, updated_at: updatedAt };
}

export interface D1Health {
  status: "ok" | "error";
  signals?: number;
  updated_at?: string | null;
  error?: string;
}

export async function d1Health(db: D1Database | undefined): Promise<D1Health> {
  if (!db) return { status: "error", error: "D1 binding 未配置（检查 wrangler.toml 的 [[d1_databases]]）" };
  try {
    await ensureSchema(db);
    const row = await db.prepare(`SELECT COUNT(*) AS n, MAX(created_at) AS updated_at FROM signals`).first<{ n: number; updated_at: string | null }>();
    return { status: "ok", signals: Number(row?.n ?? 0), updated_at: row?.updated_at ?? null };
  } catch (err) {
    return { status: "error", error: err instanceof Error ? err.message : String(err) };
  }
}


// ---------------------------------------------------------------- market ----

/** 市场整体快照（指数/涨跌家数/情绪分/冰点），对应 D1 market_snapshots 表。 */
export async function insertMarketSnapshot(
  db: D1Database,
  snapshot: Record<string, unknown>,
): Promise<{ asof: string }> {
  await ensureSchema(db);
  const asof = typeof snapshot["asof"] === "string" ? snapshot["asof"] : new Date().toISOString();
  const tradeDate = typeof snapshot["trade_date"] === "string" ? snapshot["trade_date"] : null;
  const payload = JSON.stringify(snapshot);
  await db
    .prepare(`INSERT INTO market_snapshots (asof, trade_date, payload, created_at) VALUES (?, ?, ?, ?)`)
    .bind(asof, tradeDate, payload, new Date().toISOString())
    .run();
  return { asof };
}

/** 最新一条市场快照；无数据返回 null。 */
export async function queryLatestMarket(
  db: D1Database,
): Promise<{ snapshot: Record<string, unknown>; updated_at: string } | null> {
  await ensureSchema(db);
  const row = await db
    .prepare(`SELECT payload, created_at FROM market_snapshots ORDER BY asof DESC, id DESC LIMIT 1`)
    .first<{ payload: string; created_at: string }>();
  if (!row) return null;
  try {
    return { snapshot: JSON.parse(row.payload) as Record<string, unknown>, updated_at: row.created_at };
  } catch {
    return null;
  }
}


// ------------------------------------------------------------ stock names ----

export async function upsertStockNames(
  db: D1Database,
  rows: Array<{ symbol: string; name: string }>,
): Promise<number> {
  if (rows.length === 0) return 0;
  await ensureSchema(db);
  const now = new Date().toISOString();
  const statements = rows
    .filter((r) => /^\d{6}$/.test(r.symbol) && r.name)
    .map((r) =>
      db
        .prepare(
          `INSERT INTO stock_names (symbol, name, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at`,
        )
        .bind(r.symbol, r.name, now),
    );
  if (statements.length === 0) return 0;
  for (let i = 0; i < statements.length; i += 40) {
    await db.batch(statements.slice(i, i + 40));
  }
  return statements.length;
}

export async function queryStockName(db: D1Database, symbol: string): Promise<string | null> {
  await ensureSchema(db);
  const row = await db
    .prepare(`SELECT name FROM stock_names WHERE symbol = ?`)
    .bind(symbol)
    .first<{ name: string }>();
  return row?.name ?? null;
}


// ---------------------------------------------------------- events / reports ----

export async function upsertEvents(db: D1Database, items: Array<Record<string, unknown>>): Promise<number> {
  await ensureSchema(db);
  const now = new Date().toISOString();
  const statements: D1PreparedStatement[] = [];
  for (const item of items) {
    const title = typeof item["title"] === "string" ? item["title"] : "";
    if (!title) continue;
    const id =
      typeof item["id"] === "string"
        ? item["id"]
        : `ev-${now}-${Math.random().toString(36).slice(2, 10)}`;
    statements.push(
      db
        .prepare(
          `INSERT INTO events (id, published_at, source, title, sentiment, score, payload, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET title = excluded.title, sentiment = excluded.sentiment,
             score = excluded.score, payload = excluded.payload`,
        )
        .bind(
          id,
          String(item["published_at"] ?? now),
          String(item["source"] ?? ""),
          title,
          String(item["sentiment"] ?? "neutral"),
          Number(item["score"] ?? 0),
          JSON.stringify(item),
          now,
        ),
    );
  }
  for (let i = 0; i < statements.length; i += 40) {
    await db.batch(statements.slice(i, i + 40));
  }
  return statements.length;
}

export async function queryEvents(db: D1Database, limit = 60): Promise<Array<Record<string, unknown>>> {
  await ensureSchema(db);
  const rows = await db
    .prepare(`SELECT payload FROM events ORDER BY published_at DESC LIMIT ?`)
    .bind(Math.min(Math.max(limit, 1), 200))
    .all<{ payload: string }>();
  return (rows.results ?? [])
    .map((row) => {
      try {
        return JSON.parse(row.payload) as Record<string, unknown>;
      } catch {
        return null;
      }
    })
    .filter((item): item is Record<string, unknown> => item !== null);
}

export type SnapshotTable = "backtest_reports" | "paper_snapshots" | "regime_snapshots";

export async function insertJsonSnapshot(
  db: D1Database,
  table: SnapshotTable,
  payload: Record<string, unknown>,
): Promise<{ asof: string }> {
  await ensureSchema(db);
  const asof = typeof payload["asof"] === "string" ? payload["asof"] : new Date().toISOString();
  await db
    .prepare(`INSERT INTO ${table} (asof, payload, created_at) VALUES (?, ?, ?)`)
    .bind(asof, JSON.stringify(payload), new Date().toISOString())
    .run();
  return { asof };
}

export async function queryLatestJsonSnapshot(
  db: D1Database,
  table: SnapshotTable,
): Promise<Record<string, unknown> | null> {
  await ensureSchema(db);
  const row = await db.prepare(`SELECT payload FROM ${table} ORDER BY id DESC LIMIT 1`).first<{ payload: string }>();
  if (!row) return null;
  try {
    return JSON.parse(row.payload) as Record<string, unknown>;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- klines ----

/** 云端 K 线行（D1 klines 表；供前端 Canvas 蜡烛图）。 */
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

/** 批量 UPSERT 日线（幂等；单次 request 内部按 40 条一批提交）。 */
export async function upsertKlines(db: D1Database, rows: KlineRow[]): Promise<number> {
  if (rows.length === 0) return 0;
  await ensureSchema(db);
  const statements = rows.map((row) =>
    db
      .prepare(
        `INSERT INTO klines (symbol, trade_date, open, high, low, close, volume, amount)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(symbol, trade_date) DO UPDATE SET open = excluded.open, high = excluded.high,
           low = excluded.low, close = excluded.close, volume = excluded.volume, amount = excluded.amount`,
      )
      .bind(
        row.symbol,
        row.trade_date,
        row.open,
        row.high,
        row.low,
        row.close,
        row.volume,
        row.amount,
      ),
  );
  for (let i = 0; i < statements.length; i += 40) {
    await db.batch(statements.slice(i, i + 40));
  }
  return statements.length;
}

/** 最近 limit 根日线，按 trade_date 升序返回。 */
export async function queryKlines(
  db: D1Database,
  symbol: string,
  limit: number,
): Promise<KlineRow[]> {
  await ensureSchema(db);
  const cap = Math.min(Math.max(Math.trunc(limit), 1), 250);
  const rows = await db
    .prepare(
      `SELECT symbol, trade_date, open, high, low, close, volume, amount
       FROM klines WHERE symbol = ? ORDER BY trade_date DESC LIMIT ?`,
    )
    .bind(symbol, cap)
    .all<KlineRow>();
  return (rows.results ?? []).reverse();
}


// ---------------------------------------------------------------- boards ----
// 板块行情（Worker Cron 收盘同步写；简化 regime 主线 + 前端市场全景榜使用）

export interface BoardRow {
  board_name: string;
  type: string;
  pct_1d: number | null;
  pct_5d: number | null;
  net_inflow: number | null;
  updated_at: string;
}

/** 批量 UPSERT 板块行情（幂等；同 (board_name,type) 覆盖更新）。 */
export async function upsertBoards(db: D1Database, rows: BoardRow[]): Promise<number> {
  if (rows.length === 0) return 0;
  await ensureSchema(db);
  const statements = rows.map((row) =>
    db
      .prepare(
        `INSERT INTO boards (board_name, type, pct_1d, pct_5d, net_inflow, updated_at)
         VALUES (?, ?, ?, ?, ?, ?)
         ON CONFLICT(board_name, type) DO UPDATE SET pct_1d = excluded.pct_1d,
           pct_5d = excluded.pct_5d, net_inflow = excluded.net_inflow, updated_at = excluded.updated_at`,
      )
      .bind(row.board_name, row.type, row.pct_1d, row.pct_5d, row.net_inflow, row.updated_at),
  );
  for (let i = 0; i < statements.length; i += 40) {
    await db.batch(statements.slice(i, i + 40));
  }
  return statements.length;
}

/** 板块列表：type=industry/concept 可选；按当日涨幅降序。 */
export async function queryBoards(
  db: D1Database,
  options: { type?: string; limit?: number } = {},
): Promise<BoardRow[]> {
  await ensureSchema(db);
  const limit = Math.min(Math.max(Math.trunc(options.limit ?? 20), 1), 200);
  const where = options.type ? `WHERE type = ?` : "";
  const params: unknown[] = options.type ? [options.type] : [];
  params.push(limit);
  const result = await db
    .prepare(
      `SELECT board_name, type, pct_1d, pct_5d, net_inflow, updated_at FROM boards ${where}
       ORDER BY pct_1d DESC NULLS LAST, board_name ASC LIMIT ?`,
    )
    .bind(...params)
    .all<BoardRow>();
  return result.results ?? [];
}

// ------------------------------------------------------------ intraday ----

/** 盘中指数分时（当日分钟序列，每次覆盖写；供 /api/intraday 与前端分时图）。 */
export async function upsertIntradayIndex(
  db: D1Database,
  code: string,
  tradeDate: string,
  payload: Record<string, unknown>,
  updatedAt: string,
): Promise<void> {
  await ensureSchema(db);
  await db
    .prepare(
      `INSERT INTO intraday_index (code, trade_date, updated_at, payload) VALUES (?, ?, ?, ?)
       ON CONFLICT(code, trade_date) DO UPDATE SET updated_at = excluded.updated_at, payload = excluded.payload`,
    )
    .bind(code, tradeDate, updatedAt, JSON.stringify(payload))
    .run();
}

/** 最新一条盘中分时（优先当日）。 */
export async function queryIntradayIndex(
  db: D1Database,
  code: string,
): Promise<{ code: string; trade_date: string; updated_at: string; payload: Record<string, unknown> } | null> {
  await ensureSchema(db);
  const row = await db
    .prepare(
      `SELECT code, trade_date, updated_at, payload FROM intraday_index
       WHERE code = ? ORDER BY trade_date DESC LIMIT 1`,
    )
    .bind(code)
    .first<{ code: string; trade_date: string; updated_at: string; payload: string }>();
  if (!row) return null;
  try {
    return { code: row.code, trade_date: row.trade_date, updated_at: row.updated_at, payload: JSON.parse(row.payload) as Record<string, unknown> };
  } catch {
    return null;
  }
}

// ------------------------------------------------------ market summaries ----

export interface MarketSummaryRow {
  trade_date: string;
  session: string;
  asof: string;
  summary_text: string;
  payload: string;
  created_at: string;
}

/** 写入/覆盖一条午评或收评（trade_date + session 唯一）。 */
export async function upsertMarketSummary(
  db: D1Database,
  row: Omit<MarketSummaryRow, "payload"> & { payload: Record<string, unknown> },
): Promise<void> {
  await ensureSchema(db);
  await db
    .prepare(
      `INSERT INTO market_summaries (trade_date, session, asof, summary_text, payload, created_at)
       VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT(trade_date, session) DO UPDATE SET asof = excluded.asof,
         summary_text = excluded.summary_text, payload = excluded.payload, created_at = excluded.created_at`,
    )
    .bind(row.trade_date, row.session, row.asof, row.summary_text, JSON.stringify(row.payload), row.created_at)
    .run();
}

function parseSummaryRow(row: MarketSummaryRow | null): Record<string, unknown> | null {
  if (!row) return null;
  let payload: Record<string, unknown> = {};
  try {
    payload = JSON.parse(row.payload) as Record<string, unknown>;
  } catch {
    payload = {};
  }
  return {
    trade_date: row.trade_date,
    session: row.session,
    asof: row.asof,
    summary_text: row.summary_text,
    payload,
    created_at: row.created_at,
  };
}

/** 最近一条午评 + 最近一条收评。 */
export async function queryLatestSummaries(
  db: D1Database,
): Promise<{ midday: Record<string, unknown> | null; close: Record<string, unknown> | null }> {
  await ensureSchema(db);
  const out: { midday: Record<string, unknown> | null; close: Record<string, unknown> | null } = { midday: null, close: null };
  for (const session of ["midday", "close"] as const) {
    const row = await db
      .prepare(`SELECT * FROM market_summaries WHERE session = ? ORDER BY trade_date DESC, asof DESC LIMIT 1`)
      .bind(session)
      .first<MarketSummaryRow>();
    out[session] = parseSummaryRow(row ?? null);
  }
  return out;
}

/** 最近 N 条午评/收评（时间倒序）。 */
export async function queryMarketSummaries(db: D1Database, limit = 20): Promise<Array<Record<string, unknown>>> {
  await ensureSchema(db);
  const cap = Math.min(Math.max(Math.trunc(limit), 1), 100);
  const rows = await db
    .prepare(`SELECT * FROM market_summaries ORDER BY trade_date DESC, asof DESC LIMIT ?`)
    .bind(cap)
    .all<MarketSummaryRow>();
  return (rows.results ?? []).map((row) => parseSummaryRow(row) as Record<string, unknown>);
}

/** 某交易日的市场快照序列（盘中情绪曲线用，时间升序）。 */
export async function queryMarketHistory(
  db: D1Database,
  date: string,
  limit = 120,
): Promise<Array<{ asof: string; trade_date: string | null; payload: Record<string, unknown> }>> {
  await ensureSchema(db);
  const cap = Math.min(Math.max(Math.trunc(limit), 1), 240);
  const rows = await db
    .prepare(
      `SELECT asof, trade_date, payload FROM market_snapshots
       WHERE trade_date = ? ORDER BY asof ASC, id ASC LIMIT ?`,
    )
    .bind(date, cap)
    .all<{ asof: string; trade_date: string | null; payload: string }>();
  return (rows.results ?? []).map((row) => {
    let payload: Record<string, unknown> = {};
    try {
      payload = JSON.parse(row.payload) as Record<string, unknown>;
    } catch {
      payload = {};
    }
    return { asof: row.asof, trade_date: row.trade_date, payload };
  });
}

/** 股票池（stock_names 表；Worker Cron 收盘同步取 40 只）。 */
export async function queryStockSymbols(db: D1Database, limit = 40): Promise<string[]> {
  await ensureSchema(db);
  const cap = Math.min(Math.max(Math.trunc(limit), 1), 200);
  const rows = await db
    .prepare(`SELECT symbol FROM stock_names ORDER BY symbol ASC LIMIT ?`)
    .bind(cap)
    .all<{ symbol: string }>();
  return (rows.results ?? []).map((r) => r.symbol).filter((s) => /^\d{6}$/.test(s));
}
