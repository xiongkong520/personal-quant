/**
 * 个人量化工作台 · Cloudflare Worker 入口（v2 · D1 瘦身 + 市场全景 + 边缘缓存）
 *
 * 数据架构（0009 迁移后）：
 * - 实时市场全景：/api/market-overview（腾讯指数 + 乐咕宽度 + 新浪行业 + 炸板率），60s 边缘缓存
 *   （Cache API 一级 + D1 edge_cache 二级；workers.dev 上 Cache API no-op，D1 保证 MISS→HIT）
 * - 历史 K 线：不再入 D1，/api/kline 实时拉东财并缓存 5 分钟
 * - D1 轻量表：signal_log（信号）、account_snapshot / positions / order_log（模拟盘）
 *   以及 stock_names（搜索映射）、edge_cache（边缘缓存二级）
 * - 旧大表（klines/intraday/boards/market/events/regime/backtest/paper 快照/signals）已删除，
 *   旧接口保留兼容响应（空数据 + degraded 标记），避免前端/脚本 500。
 *
 * 端点：
 *   GET  /api/config /api/health /api/me /api/model
 *   POST /api/verify（Turnstile -> quant_pass cookie）
 *   GET  /api/market-overview  市场全景聚合（§3.1 契约）
 *   GET  /api/market           兼容旧结构（内部复用 market-overview）
 *   GET  /api/intraday         指数实时分时（东财 trends2，30s 缓存）
 *   GET  /api/kline            个股/指数实时 K 线（东财，60s 缓存 + 模块内 5 分钟缓存）
 *   GET  /api/boards           行业板块榜（来自 market-overview，120s 缓存）
 *   GET  /api/signals /api/stream /api/search /api/stock
 *   GET  /api/paper            模拟盘聚合（account_snapshot + positions + order_log）
 *   GET  /api/account /api/positions /api/orders /api/signal-log
 *   POST /api/ingest /api/signal-log/ingest /api/account/ingest /api/positions/ingest /api/orders/ingest
 *   POST /api/stock-names/ingest /api/daily-sync /api/ai/generate
 *   GET  /api/ai/insight /api/summary/latest /api/summaries /api/events /api/regime /api/backtest /api/market/history
 *   其余路径 -> 静态资源（cloudflare/pages/）
 */
import modelJson from "../models/model.json" with { type: "json" };

import { confidenceOf, directionOf, predictProba, validateModel, type QuantModel } from "./model.ts";
import { refreshMarketLive } from "./market_live.ts";
import { handleScheduledCron, runManualDailySync } from "./daily_sync.ts";
import { applyCacheHeaders, withEdgeCache, PassthroughError, type CachedResult } from "./cache.ts";
import {
  buildMarketOverview,
  fetchIntradayData,
  overviewToBoardRows,
  parseOverviewParams,
  toLegacyMarketSnapshot,
} from "./market_overview.ts";
import { fetchRealtimeKline, indexName, isIndexSymbol } from "./index_kline.ts";
import { searchStocks } from "./search.ts";
import { generateAiInsight, getAiInsight } from "./ai_proxy.ts";
import {
  d1Health,
  ensureSchema,
  queryLatestAccount,
  queryOrders,
  queryPositions,
  querySignalLog,
  queryStockName,
  replacePositions,
  upsertAccountSnapshot,
  upsertOrders,
  upsertSignalLog,
  upsertStockNames,
  type AccountSnapshotRow,
  type OrderLogRow,
  type PositionRow,
  type SignalLogRow,
} from "./db.ts";
import { extractIngestToken, MAX_INGEST_BYTES, normalizeSignal, nowShanghai, parseIngestBody, timingSafeEqual } from "./signals.ts";
import {
  applySecurityHeaders,
  checkAcceptHeader,
  checkRateLimit,
  checkSameOrigin,
  checkUserAgent,
  clientIp,
  isTrustedIp,
  COOKIE_NAME,
  COOKIE_TTL_SECONDS,
  issuePassCookie,
  readCookieKey,
  readRateLimitPerMin,
  readTurnstileSitekey,
  TURNSTILE_TEST_SITEKEY,
  verifyPassCookie,
  verifyTurnstileToken,
  type RateLimitResult,
} from "./security.ts";
import type { Env, SignalRow } from "./types.ts";

const SERVICE = "personal-quant-dashboard";
const VERSION = "0.3.0";
const SCHEMA_VERSION = 1;

const SSE_HEARTBEAT_MS = 30_000;
const SSE_POLL_MS = 5_000;
const SSE_MAX_LIFETIME_MS = 30 * 60_000;

const QUANT_MODEL = modelJson as unknown as QuantModel;
let modelError: string | null = null;
try {
  validateModel(QUANT_MODEL);
  const probe = predictProba(QUANT_MODEL, {});
  if (Math.abs(probe.up + probe.flat + probe.down - 1) > 1e-6) throw new Error("模型自检概率和不为 1");
} catch (err) {
  modelError = err instanceof Error ? err.message : String(err);
  console.error(`[worker] model.json 不符合契约 §5：${modelError}`);
}

// ---------------------------------------------------------------- helpers ----

function jsonResponse(data: unknown, status = 200, extraHeaders: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...extraHeaders,
    },
  });
}

function errorResponse(status: number, error: string, message: string, extra: Record<string, unknown> = {}): Response {
  return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error, message, ...extra }, status);
}

/** 已废弃的旧 ingest 接口：返回 200 兼容响应，避免 GitHub Actions/旧脚本 500。 */
function deprecatedIngest(feature: string, hint: string): Response {
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    accepted: 0,
    deprecated: true,
    message: `${feature} 已废弃：${hint}`,
  });
}

function normalizePath(pathname: string): string {
  if (pathname.length > 1 && pathname.endsWith("/")) return pathname.replace(/\/+$/, "");
  return pathname;
}

function methodNotAllowed(allow: string[]): Response {
  return jsonResponse(
    { schema: SCHEMA_VERSION, ok: false, error: "method_not_allowed", message: `只支持 ${allow.join(", ")}` },
    405,
    { allow: allow.join(", ") },
  );
}

function methodGuard(request: Request, allow: string[]): Response | null {
  return allow.includes(request.method) ? null : methodNotAllowed(allow);
}

function rateLimitedResponse(result: RateLimitResult): Response {
  return jsonResponse(
    {
      schema: SCHEMA_VERSION,
      ok: false,
      error: "rate_limited",
      message: "请求过于频繁，请稍后重试",
      retry_after: result.retryAfter,
      limit: result.limit,
    },
    429,
    {
      "retry-after": String(result.retryAfter),
      "x-ratelimit-limit": String(result.limit),
      "x-ratelimit-remaining": "0",
      "x-ratelimit-reset": String(Math.ceil(result.resetAt / 1000)),
    },
  );
}

function forbiddenClient(reason: string, message: string): Response {
  return errorResponse(403, "forbidden_client", message, { reason });
}

function challengeRequired(): Response {
  return jsonResponse(
    {
      schema: SCHEMA_VERSION,
      ok: false,
      error: "challenge_required",
      challenge: true,
      message: "请先完成人机验证后继续。",
    },
    401,
  );
}

async function requireChallenge(request: Request, env: Env): Promise<Response | null> {
  if (isTrustedIp(request, env)) return null;
  if (await verifyPassCookie(env, request.headers.get("cookie"))) return null;
  return challengeRequired();
}

function readIngestToken(env: Env): string {
  return (env.QUANT_INGEST_TOKEN ?? env.INGEST_TOKEN ?? "").trim();
}

/** 机器接口 token 校验（x-ingest-token / Bearer）；失败返回 Response，成功返回 null。 */
function requireMachineToken(request: Request, env: Env): Response | null {
  const token = readIngestToken(env);
  if (!token) return errorResponse(503, "server_misconfigured", "服务暂不可用，请稍后重试");
  const got = extractIngestToken(request);
  if (!got || !timingSafeEqual(got, token)) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "unauthorized", message: "请求未授权" }, 401);
  }
  return null;
}

/**
 * GET 数据接口统一边缘缓存包装：
 * - 先鉴权（challenge）后读缓存；
 * - HIT/MISS/STALE 统一 X-Cache 头；非 200 透传不缓存；上游失败回 last-known-good 并标 STALE。
 */
async function cachedRoute(
  request: Request,
  env: Env,
  ttlSec: number,
  handler: () => Promise<Response>,
  decorate?: (data: unknown, result: CachedResult) => unknown,
): Promise<Response> {
  const challenge = await requireChallenge(request, env);
  if (challenge) return challenge;
  if (env.DB) {
    try {
      await ensureSchema(env.DB);
    } catch {
      // 建表失败不阻塞主流程；缓存层自行降级
    }
  }
  try {
    const result = await withEdgeCache(
      request,
      ttlSec,
      async () => {
        const resp = await handler();
        if (resp.status !== 200) throw new PassthroughError(resp);
        return await resp.json();
      },
      env.DB,
    );
    const payload = decorate ? decorate(result.data, result) : result.data;
    return applyCacheHeaders(jsonResponse(payload, 200), { ...result });
  } catch (err) {
    if (err instanceof PassthroughError) return err.response;
    console.error(`[worker] 缓存路由失败：${err instanceof Error ? err.message : String(err)}`);
    return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  }
}

function cacheDecorate(ttlSec: number) {
  return (data: unknown, result: CachedResult): unknown => {
    const base = (data && typeof data === "object" ? data : {}) as Record<string, unknown>;
    return {
      ...base,
      cache: {
        hit: result.cacheState === "HIT",
        age_sec: result.ageSec,
        ttl: ttlSec,
        stale: result.cacheState === "STALE",
      },
    };
  };
}

// ---------------------------------------------------------------- health ----

async function handleHealth(env: Env): Promise<Response> {
  const d1 = await d1Health(env.DB);
  const tokenConfigured = readIngestToken(env).length > 0;
  const cookieKeyConfigured = readCookieKey(env).length > 0;
  const ok = d1.status === "ok" && modelError === null;
  return jsonResponse(
    {
      schema: SCHEMA_VERSION,
      ok,
      service: SERVICE,
      version: env.WORKER_VERSION ?? VERSION,
      now: nowShanghai(),
      environment: "cloudflare-worker",
      d1: { ...d1, storage: "light-tables-v2" },
      ingest: {
        token_configured: tokenConfigured,
        auth: "Authorization: Bearer <QUANT_INGEST_TOKEN> 或 X-Ingest-Token",
        endpoints: [
          "POST /api/ingest (signal_log 兼容映射)",
          "POST /api/signal-log/ingest",
          "POST /api/account/ingest",
          "POST /api/positions/ingest",
          "POST /api/orders/ingest",
          "POST /api/stock-names/ingest",
          "POST /api/daily-sync",
        ],
      },
      security: {
        ua_filter: true,
        accept_check: true,
        same_origin_cors: true,
        rate_limit_per_min: readRateLimitPerMin(env),
        rate_limit_backend: env.RATE_LIMIT_KV ? "kv" : "memory",
        challenge_required_for: ["/api/signals", "/api/stream", "/api/model"],
        trusted_ip_configured: Boolean((env.TRUSTED_IPS ?? "").trim()),
        cookie: { name: COOKIE_NAME, ttl_seconds: COOKIE_TTL_SECONDS, cookie_key_configured: cookieKeyConfigured },
        turnstile: {
          sitekey: readTurnstileSitekey(env),
          using_test_keys: readTurnstileSitekey(env) === TURNSTILE_TEST_SITEKEY || !env.TURNSTILE_SECRET,
          secret_configured: Boolean((env.TURNSTILE_SECRET ?? "").trim()),
          verify_endpoint_override: Boolean((env.TURNSTILE_VERIFY_URL ?? "").trim()),
        },
      },
      model: {
        schema: QUANT_MODEL.schema,
        version: QUANT_MODEL.version,
        kind: QUANT_MODEL.kind,
        classes: QUANT_MODEL.classes,
        features: Array.isArray(QUANT_MODEL.features) ? QUANT_MODEL.features.length : 0,
        error: modelError,
      },
      endpoints: [
        "GET /api/config",
        "POST /api/verify",
        "GET /api/health",
        "GET /api/market-overview",
        "GET /api/market",
        "GET /api/kline",
        "GET /api/intraday",
        "GET /api/boards",
        "GET /api/signals",
        "GET /api/stream",
        "GET /api/search",
        "GET /api/paper",
        "GET /api/summary/latest",
        "GET /api/summaries",
        "GET /api/ai/insight",
        "POST /api/ai/generate",
      ],
    },
    ok ? 200 : 503,
  );
}

// ---------------------------------------------------------------- config ----

function handleConfig(env: Env): Response {
  const sitekey = readTurnstileSitekey(env);
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    challenge: true,
    turnstile_sitekey: sitekey,
    turnstileSitekey: sitekey,
    sitekey,
    turnstile: {
      sitekey,
      siteKey: sitekey,
      verify_path: "/api/verify",
      test_sitekey: sitekey === TURNSTILE_TEST_SITEKEY,
      configured: Boolean((env.TURNSTILE_SITEKEY ?? "").trim() && (env.TURNSTILE_SECRET ?? "").trim()),
    },
    verify_path: "/api/verify",
    cookie_name: COOKIE_NAME,
    cookie_ttl_seconds: COOKIE_TTL_SECONDS,
    verify_ready: readCookieKey(env).length > 0,
    rate_limit: { requests_per_minute: readRateLimitPerMin(env) },
  });
}

// ---------------------------------------------------------------- verify ----

async function readTokenField(request: Request): Promise<{ token?: string; error?: string }> {
  const contentType = (request.headers.get("content-type") ?? "").toLowerCase();
  try {
    if (contentType.includes("application/json")) {
      const body = (await request.json()) as Record<string, unknown> | null;
      const token = body && typeof body.token === "string" ? body.token : undefined;
      return token ? { token } : { error: "缺 token 字段" };
    }
    if (contentType.includes("application/x-www-form-urlencoded") || contentType.includes("multipart/form-data")) {
      const form = await request.formData();
      const token = form.get("token");
      return typeof token === "string" && token ? { token } : { error: "缺 token 字段" };
    }
    const text = await request.text();
    if (text) {
      const body = JSON.parse(text) as Record<string, unknown>;
      const token = body && typeof body.token === "string" ? body.token : undefined;
      return token ? { token } : { error: "缺 token 字段" };
    }
    return { error: "请求体为空" };
  } catch (err) {
    return { error: `请求体解析失败：${err instanceof Error ? err.message : String(err)}` };
  }
}

async function handleVerify(request: Request, env: Env): Promise<Response> {
  if (!readCookieKey(env)) {
    return errorResponse(503, "server_misconfigured", "COOKIE_KEY 未配置：本地写 .dev.vars；部署 wrangler secret put COOKIE_KEY");
  }
  const parsed = await readTokenField(request);
  if (parsed.error || !parsed.token) return errorResponse(400, "invalid_body", parsed.error ?? "缺少 token");
  const result = await verifyTurnstileToken(env, parsed.token, clientIp(request));
  if (!result.ok) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "challenge_failed", errors: result.errors }, 403);
  }
  const pass = await issuePassCookie(env);
  if (!pass) return errorResponse(503, "server_misconfigured", "COOKIE_KEY 未配置，无法签发 quant_pass");
  return jsonResponse(
    {
      schema: SCHEMA_VERSION,
      ok: true,
      expires_in: COOKIE_TTL_SECONDS,
      expires_at: nowShanghai(new Date(pass.expiresAt * 1000)),
      degraded: result.degraded === true,
    },
    200,
    { "set-cookie": pass.setCookie },
  );
}

// ---------------------------------------------------------------- model ----

function handleModel(): Response {
  const features = Array.isArray(QUANT_MODEL.features) ? QUANT_MODEL.features : [];
  let probe: { prob: Record<string, number>; direction: string; confidence: number } | null = null;
  if (modelError === null) {
    const zeroVector: Record<string, number> = {};
    for (const f of features) zeroVector[f] = 0;
    const prob = predictProba(QUANT_MODEL, zeroVector);
    probe = { prob: { ...prob }, direction: directionOf(prob), confidence: confidenceOf(prob) };
  }
  return jsonResponse(
    {
      schema: SCHEMA_VERSION,
      ok: modelError === null,
      source: "cloudflare/worker/models/model.json",
      model: QUANT_MODEL,
      parity_probe: { zero_features: probe },
    },
    modelError === null ? 200 : 500,
  );
}

// ---------------------------------------------------------------- signals ----

async function handleSignals(env: Env, url: URL): Promise<Response> {
  if (!env.DB) return errorResponse(503, "d1_not_configured", "D1 binding 未配置（检查 wrangler.toml）");
  const limitRaw = Number(url.searchParams.get("limit") ?? "200");
  const limit = Number.isFinite(limitRaw) ? Math.min(Math.max(Math.trunc(limitRaw), 1), 500) : 200;
  const symbolsRaw = (url.searchParams.get("symbols") ?? "").trim();
  const symbols = symbolsRaw.split(",").map((s) => s.trim()).filter(Boolean);
  if (symbols.length > 50) return errorResponse(400, "invalid_symbols", "symbols 最多 50 个");
  const bad = symbols.find((s) => !/^\d{6}$/.test(s));
  if (bad) return errorResponse(400, "invalid_symbols", `symbols 必须是 6 位数字代码，收到 ${JSON.stringify(bad)}`);
  const latest = ["1", "true", "yes"].includes((url.searchParams.get("latest") ?? "").toLowerCase());
  const { items, updated_at } = await querySignalLog(env.DB, { limit, symbols, latest });
  return jsonResponse({ schema: SCHEMA_VERSION, items, updated_at });
}

function signalRowToLogRow(row: SignalRow, name: string | null): SignalLogRow {
  return {
    id: row.signal_id,
    symbol: row.symbol,
    name,
    trade_date: row.trade_date,
    horizon: row.horizon,
    prob_up: row.prob_up,
    prob_flat: row.prob_flat,
    prob_down: row.prob_down,
    confidence: row.confidence,
    direction: row.direction,
    rank_score: row.confidence,
    top_features: row.top_features,
    model_version: row.model_version,
    expires_at: row.expires_at,
    created_at: row.created_at,
  };
}

/** GET /api/stock?symbol=600519：个股快照（signal_log 最新一条 + 名称）。 */
async function handleStock(request: Request, env: Env, url: URL): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const symbol = (url.searchParams.get("symbol") ?? "").trim();
  if (!/^\d{6}$/.test(symbol)) return errorResponse(400, "invalid_symbol", "请输入 6 位股票代码");
  const { items } = await querySignalLog(env.DB, { symbols: [symbol], latest: true, limit: 1 });
  const name = (await queryStockName(env.DB, symbol)) ?? "";
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    symbol,
    name,
    signal: items[0] ?? null,
    updated_at: items[0]?.created_at ?? null,
  });
}

// ------------------------------------------------------- 轻量表 ingest / 查询 ----

async function readJsonBody(request: Request): Promise<{ body?: unknown; error?: Response }> {
  const length = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(length) && length > MAX_INGEST_BYTES) {
    return { error: errorResponse(413, "payload_too_large", `单次推送不能超过 ${MAX_INGEST_BYTES} 字节`) };
  }
  let text: string;
  try {
    text = await request.text();
  } catch (err) {
    return { error: errorResponse(400, "invalid_body", `读取请求体失败：${err instanceof Error ? err.message : String(err)}`) };
  }
  if (text.length > MAX_INGEST_BYTES) return { error: errorResponse(413, "payload_too_large", `单次推送不能超过 ${MAX_INGEST_BYTES} 字节`) };
  try {
    return { body: JSON.parse(text) };
  } catch (err) {
    return { error: errorResponse(400, "invalid_json", `请求体不是合法 JSON：${err instanceof Error ? err.message : String(err)}`) };
  }
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

/** POST /api/ingest：兼容旧 §4 信号 JSON → 写 signal_log（幂等）。 */
async function handleIngest(request: Request, env: Env): Promise<Response> {
  const auth = requireMachineToken(request, env);
  if (auth) return auth;
  if (!env.DB) return errorResponse(503, "d1_not_configured", "D1 binding 未配置（检查 wrangler.toml）");
  const parsedBody = await readJsonBody(request);
  if (parsedBody.error) return parsedBody.error;
  const parsed = parseIngestBody(parsedBody.body);
  if (parsed.error) return errorResponse(400, "invalid_body", parsed.error);
  const nowIso = nowShanghai();
  const rows: SignalLogRow[] = [];
  const rejected: Array<Record<string, unknown>> = [];
  const warnings: Array<Record<string, unknown>> = [];
  parsed.signals.forEach((raw, index) => {
    const result = normalizeSignal(raw, nowIso);
    if (result.row) {
      rows.push(signalRowToLogRow(result.row, null));
      if (result.warnings.length > 0) warnings.push({ index, warnings: result.warnings });
    } else {
      rejected.push({ index, errors: result.errors, warnings: result.warnings });
    }
  });
  if (rows.length === 0) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "all_signals_rejected", accepted: 0, rejected }, 400);
  }
  const accepted = await upsertSignalLog(env.DB, rows);
  const latest = rows.reduce<string | null>((max, row) => (max === null || row.created_at > max ? row.created_at : max), null);
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    accepted,
    rejected,
    warnings,
    signal_ids: rows.map((row) => row.id),
    updated_at: latest,
    storage: "signal_log",
  });
}

/** POST /api/signal-log/ingest：新链路（可直接推 signal_log 行）。 */
async function handleSignalLogIngest(request: Request, env: Env): Promise<Response> {
  const auth = requireMachineToken(request, env);
  if (auth) return auth;
  if (!env.DB) return errorResponse(503, "d1_not_configured", "D1 binding 未配置");
  const parsedBody = await readJsonBody(request);
  if (parsedBody.error) return parsedBody.error;
  const body = parsedBody.body as Record<string, unknown> | null;
  const items = Array.isArray(body) ? body : Array.isArray(body?.["items"]) ? (body?.["items"] as unknown[]) : null;
  if (!items) return errorResponse(400, "invalid_body", "需要 items 数组");
  const now = new Date().toISOString();
  const rows: SignalLogRow[] = [];
  for (const item of items) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const obj = item as Record<string, unknown>;
    const symbol = typeof obj["symbol"] === "string" ? obj["symbol"].trim() : "";
    const tradeDate = typeof obj["trade_date"] === "string" ? obj["trade_date"].trim() : "";
    if (!/^\d{6}$/.test(symbol) || !/^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) continue;
    const id = typeof obj["id"] === "string" ? obj["id"] : `${symbol}-${tradeDate}-${String(obj["model_version"] ?? "cloud")}`;
    rows.push({
      id,
      symbol,
      name: typeof obj["name"] === "string" ? (obj["name"] as string) : null,
      trade_date: tradeDate,
      horizon: typeof obj["horizon"] === "string" ? (obj["horizon"] as string) : "1d",
      prob_up: asNumber(obj["prob_up"] ?? (obj["prob"] as Record<string, unknown> | undefined)?.["up"]),
      prob_flat: asNumber(obj["prob_flat"] ?? (obj["prob"] as Record<string, unknown> | undefined)?.["flat"]),
      prob_down: asNumber(obj["prob_down"] ?? (obj["prob"] as Record<string, unknown> | undefined)?.["down"]),
      confidence: asNumber(obj["confidence"]),
      direction: typeof obj["direction"] === "string" ? (obj["direction"] as string) : null,
      rank_score: asNumber(obj["rank_score"] ?? obj["confidence"]),
      top_features: typeof obj["top_features"] === "string" ? (obj["top_features"] as string) : JSON.stringify(obj["top_features"] ?? []),
      model_version: typeof obj["model_version"] === "string" ? (obj["model_version"] as string) : null,
      expires_at: typeof obj["expires_at"] === "string" ? (obj["expires_at"] as string) : null,
      created_at: typeof obj["created_at"] === "string" ? (obj["created_at"] as string) : now,
    });
  }
  if (rows.length === 0) return errorResponse(400, "invalid_body", "没有合法 signal 行");
  const accepted = await upsertSignalLog(env.DB, rows);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, accepted, skipped: items.length - rows.length });
}

/** POST /api/account/ingest：模拟盘账户快照（覆盖最新）。 */
async function handleAccountIngest(request: Request, env: Env): Promise<Response> {
  const auth = requireMachineToken(request, env);
  if (auth) return auth;
  if (!env.DB) return errorResponse(503, "d1_not_configured", "D1 binding 未配置");
  const parsedBody = await readJsonBody(request);
  if (parsedBody.error) return parsedBody.error;
  const body = parsedBody.body as Record<string, unknown> | null;
  const snapshot = (body && typeof body["snapshot"] === "object" ? (body["snapshot"] as Record<string, unknown>) : body) ?? {};
  const totalAsset = asNumber(snapshot["total_asset"] ?? snapshot["equity"]);
  if (totalAsset === null) return errorResponse(400, "invalid_body", "缺少 total_asset / equity");
  const cash = asNumber(snapshot["cash"]) ?? 0;
  const marketValue = asNumber(snapshot["market_value"]) ?? Math.max(0, totalAsset - cash);
  const row: AccountSnapshotRow = {
    asof: typeof snapshot["asof"] === "string" ? (snapshot["asof"] as string) : nowShanghai(),
    total_asset: totalAsset,
    cash,
    market_value: marketValue,
    today_pnl: asNumber(snapshot["today_pnl"]) ?? 0,
    total_pnl: asNumber(snapshot["total_pnl"]) ?? 0,
    position_count: asNumber(snapshot["position_count"]) ?? 0,
    source: typeof snapshot["source"] === "string" ? (snapshot["source"] as string) : "paper",
    created_at: new Date().toISOString(),
  };
  await upsertAccountSnapshot(env.DB, row);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, accepted: 1, asof: row.asof });
}

/** POST /api/positions/ingest：整体替换当前持仓。 */
async function handlePositionsIngest(request: Request, env: Env): Promise<Response> {
  const auth = requireMachineToken(request, env);
  if (auth) return auth;
  if (!env.DB) return errorResponse(503, "d1_not_configured", "D1 binding 未配置");
  const parsedBody = await readJsonBody(request);
  if (parsedBody.error) return parsedBody.error;
  const body = parsedBody.body as Record<string, unknown> | null;
  const items = Array.isArray(body) ? body : Array.isArray(body?.["items"]) ? (body?.["items"] as unknown[]) : null;
  if (!items) return errorResponse(400, "invalid_body", "需要 items 数组");
  const now = new Date().toISOString();
  const rows: PositionRow[] = [];
  for (const item of items) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const obj = item as Record<string, unknown>;
    const symbol = typeof obj["symbol"] === "string" ? obj["symbol"].trim() : "";
    const qty = asNumber(obj["qty"]);
    const cost = asNumber(obj["cost_price"]);
    if (!/^\d{6}$/.test(symbol) || qty === null || cost === null) continue;
    rows.push({
      symbol,
      name: typeof obj["name"] === "string" ? (obj["name"] as string) : null,
      qty: Math.trunc(qty),
      cost_price: cost,
      last_price: asNumber(obj["last_price"]),
      market_value: asNumber(obj["market_value"]),
      pnl: asNumber(obj["pnl"]),
      pnl_pct: asNumber(obj["pnl_pct"]),
      asof: typeof obj["asof"] === "string" ? (obj["asof"] as string) : nowShanghai(),
      updated_at: now,
    });
  }
  const count = await replacePositions(env.DB, rows);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, accepted: count });
}

/** POST /api/orders/ingest：订单流水（按 id 幂等）。 */
async function handleOrdersIngest(request: Request, env: Env): Promise<Response> {
  const auth = requireMachineToken(request, env);
  if (auth) return auth;
  if (!env.DB) return errorResponse(503, "d1_not_configured", "D1 binding 未配置");
  const parsedBody = await readJsonBody(request);
  if (parsedBody.error) return parsedBody.error;
  const body = parsedBody.body as Record<string, unknown> | null;
  const items = Array.isArray(body) ? body : Array.isArray(body?.["items"]) ? (body?.["items"] as unknown[]) : null;
  if (!items) return errorResponse(400, "invalid_body", "需要 items 数组");
  const now = new Date().toISOString();
  const rows: OrderLogRow[] = [];
  for (const item of items) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const obj = item as Record<string, unknown>;
    const symbol = typeof obj["symbol"] === "string" ? obj["symbol"].trim() : "";
    const side = typeof obj["side"] === "string" ? (obj["side"] as string) : "";
    const qty = asNumber(obj["qty"]);
    const id = typeof obj["id"] === "string" ? (obj["id"] as string) : "";
    if (!id || !/^\d{6}$/.test(symbol) || qty === null || !["buy", "sell"].includes(side)) continue;
    rows.push({
      id,
      symbol,
      name: typeof obj["name"] === "string" ? (obj["name"] as string) : null,
      side,
      qty: Math.trunc(qty),
      target_price: asNumber(obj["target_price"]),
      filled_price: asNumber(obj["filled_price"]),
      commission: asNumber(obj["commission"]) ?? 0,
      stamp_tax: asNumber(obj["stamp_tax"]) ?? 0,
      transfer_fee: asNumber(obj["transfer_fee"]) ?? 0,
      fee_total: asNumber(obj["fee_total"]) ?? 0,
      status: typeof obj["status"] === "string" ? (obj["status"] as string) : "filled",
      reason: typeof obj["reason"] === "string" ? (obj["reason"] as string) : null,
      trade_date: typeof obj["trade_date"] === "string" ? (obj["trade_date"] as string) : null,
      created_at: typeof obj["created_at"] === "string" ? (obj["created_at"] as string) : now,
    });
  }
  const accepted = await upsertOrders(env.DB, rows);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, accepted, skipped: items.length - rows.length });
}

async function handleStockNamesIngest(request: Request, env: Env): Promise<Response> {
  const auth = requireMachineToken(request, env);
  if (auth) return auth;
  if (!env.DB) return errorResponse(503, "d1_not_configured", "D1 binding 未配置");
  const parsedBody = await readJsonBody(request);
  if (parsedBody.error) return parsedBody.error;
  const body = parsedBody.body as Record<string, unknown> | null;
  const items = Array.isArray(body) ? body : Array.isArray(body?.["items"]) ? (body?.["items"] as unknown[]) : null;
  if (!items) return errorResponse(400, "invalid_body", "需要 items 数组");
  const rows: Array<{ symbol: string; name: string }> = [];
  for (const item of items) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const obj = item as Record<string, unknown>;
    const symbol = typeof obj["symbol"] === "string" ? obj["symbol"].trim() : "";
    const name = typeof obj["name"] === "string" ? obj["name"].trim() : "";
    if (/^\d{6}$/.test(symbol) && name) rows.push({ symbol, name });
  }
  const accepted = await upsertStockNames(env.DB, rows);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, accepted });
}

// ------------------------------------------------------- 市场 / 板块 / 分时 ----

/** GET /api/market-overview：市场全景（§3.1），60s 缓存 + cache 字段。 */
async function handleMarketOverview(_request: Request, _env: Env, url: URL): Promise<Response> {
  const params = parseOverviewParams(url);
  if (!params) {
    return errorResponse(400, "invalid_params", "sector_type 支持 industry/concept；sort 支持 gainers/losers；limit 1-30");
  }
  const payload = await buildMarketOverview(params);
  return jsonResponse(payload);
}

/** GET /api/market：兼容旧结构（内部复用 market-overview，保留 snapshot/updated_at 字段）。 */
async function handleMarket(): Promise<Response> {
  const overview = await buildMarketOverview({});
  const snapshot = toLegacyMarketSnapshot(overview);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, snapshot, updated_at: overview["asof"], cache_source: "market-overview" });
}

/** GET /api/boards：行业板块榜（来自 market-overview sectors）。 */
async function handleBoards(url: URL): Promise<Response> {
  const limitRaw = Number(url.searchParams.get("limit") ?? "20");
  const limit = Number.isFinite(limitRaw) ? Math.min(Math.max(Math.trunc(limitRaw), 1), 100) : 20;
  const overview = await buildMarketOverview({});
  const items = overviewToBoardRows(overview, limit);
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    type: "industry",
    count: items.length,
    items,
    updated_at: overview["asof"],
    degraded: Boolean((overview["quality"] as Record<string, unknown> | undefined)?.["degraded"]),
  });
}

/** GET /api/intraday?code=sh000001：指数实时分时（东财 trends2，30s 缓存）。 */
async function handleIntraday(url: URL): Promise<Response> {
  const code = (url.searchParams.get("code") ?? "sh000001").trim();
  if (!/^(sh|sz)\d{6}$/.test(code)) return errorResponse(400, "invalid_code", "code 需形如 sh000001 / sz399001");
  const data = await fetchIntradayData(code);
  if (!data) return errorResponse(503, "data_unavailable", "分时数据暂不可用，请稍后重试");
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    code,
    trade_date: data.trade_date,
    updated_at: nowShanghai(),
    stale: data.trade_date !== nowShanghai().slice(0, 10),
    data,
  });
}

// ------------------------------------------------------------------ kline ----

async function klineHandler(url: URL): Promise<Response> {
  const symbol = (url.searchParams.get("symbol") ?? "").trim();
  if (!/^(\d{6}|(sh|sz|bj)\d{6})$/.test(symbol)) {
    return errorResponse(400, "invalid_symbol", "请输入 6 位股票代码或 sh/sz+6 位指数代码");
  }
  const limitRaw = Number(url.searchParams.get("limit") ?? url.searchParams.get("days") ?? "120");
  const limit = Number.isFinite(limitRaw) ? Math.min(Math.max(Math.trunc(limitRaw), 1), 250) : 120;
  const kind = isIndexSymbol(symbol) ? "index" : "stock";
  const items = await fetchRealtimeKline(symbol, limit);
  if (!items) return errorResponse(503, "data_unavailable", "K 线数据暂不可用，请稍后重试");
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    symbol,
    name: indexName(symbol),
    kind,
    limit,
    count: items.length,
    items,
    updated_at: nowShanghai(),
  });
}

// ---------------------------------------------------------------- paper ----

async function handlePaper(request: Request, env: Env): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const [account, positions, orders] = await Promise.all([
    queryLatestAccount(env.DB),
    queryPositions(env.DB),
    queryOrders(env.DB, 20),
  ]);
  const degraded = account === null;
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    snapshot: {
      account,
      positions,
      orders,
      asof: account?.asof ?? null,
    },
    updated_at: account?.asof ?? null,
    degraded,
  });
}

/** POST /api/paper/ingest：兼容旧模拟盘快照推送 -> account_snapshot（positions 可选）。 */
async function handlePaperIngest(request: Request, env: Env): Promise<Response> {
  const auth = requireMachineToken(request, env);
  if (auth) return auth;
  if (!env.DB) return errorResponse(503, "d1_not_configured", "D1 binding 未配置");
  const parsedBody = await readJsonBody(request);
  if (parsedBody.error) return parsedBody.error;
  const body = parsedBody.body as Record<string, unknown> | null;
  const snapshot = (body && typeof body["snapshot"] === "object" ? (body["snapshot"] as Record<string, unknown>) : body) ?? {};
  const totalAsset = asNumber(snapshot["total_asset"] ?? snapshot["equity"] ?? snapshot["total_asset_value"]);
  const positionsRaw = Array.isArray(body?.["positions"]) ? (body?.["positions"] as unknown[]) : null;
  if (totalAsset !== null) {
    const row: AccountSnapshotRow = {
      asof: typeof snapshot["asof"] === "string" ? (snapshot["asof"] as string) : nowShanghai(),
      total_asset: totalAsset,
      cash: asNumber(snapshot["cash"]) ?? 0,
      market_value: asNumber(snapshot["market_value"]) ?? Math.max(0, totalAsset - (asNumber(snapshot["cash"]) ?? 0)),
      today_pnl: asNumber(snapshot["today_pnl"]) ?? 0,
      total_pnl: asNumber(snapshot["total_pnl"]) ?? 0,
      position_count: asNumber(snapshot["position_count"]) ?? 0,
      source: "paper",
      created_at: new Date().toISOString(),
    };
    await upsertAccountSnapshot(env.DB, row);
  }
  let positionsAccepted = 0;
  if (positionsRaw) {
    const now = new Date().toISOString();
    const rows: PositionRow[] = [];
    for (const item of positionsRaw) {
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;
      const obj = item as Record<string, unknown>;
      const symbol = typeof obj["symbol"] === "string" ? (obj["symbol"] as string) : "";
      const qty = asNumber(obj["qty"]);
      const cost = asNumber(obj["avg_cost"] ?? obj["cost_price"]);
      if (!/^\d{6}$/.test(symbol) || qty === null || cost === null) continue;
      rows.push({
        symbol,
        name: null,
        qty: Math.trunc(qty),
        cost_price: cost,
        last_price: asNumber(obj["last_price"]),
        market_value: asNumber(obj["market_value"]),
        pnl: asNumber(obj["pnl"]),
        pnl_pct: asNumber(obj["pnl_pct"]),
        asof: nowShanghai(),
        updated_at: now,
      });
    }
    positionsAccepted = await replacePositions(env.DB, rows);
  }
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    accepted: totalAsset !== null ? 1 : 0,
    positions: positionsAccepted,
    deprecated: totalAsset === null && positionsRaw === null,
  });
}

/** GET /api/account /api/positions /api/orders：轻量表直接读取。 */
async function handleAccountGet(request: Request, env: Env): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const account = await queryLatestAccount(env.DB);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, account, updated_at: account?.asof ?? null, degraded: account === null });
}

async function handlePositionsGet(request: Request, env: Env): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const items = await queryPositions(env.DB);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, count: items.length, items });
}

async function handleOrdersGet(request: Request, env: Env, url: URL): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const limitRaw = Number(url.searchParams.get("limit") ?? "50");
  const limit = Number.isFinite(limitRaw) ? Math.min(Math.max(Math.trunc(limitRaw), 1), 200) : 50;
  const items = await queryOrders(env.DB, limit);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, count: items.length, items });
}

async function handleSignalLogGet(request: Request, env: Env, url: URL): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const limitRaw = Number(url.searchParams.get("limit") ?? "200");
  const limit = Number.isFinite(limitRaw) ? Math.min(Math.max(Math.trunc(limitRaw), 1), 500) : 200;
  const latest = ["1", "true"].includes((url.searchParams.get("latest") ?? "").toLowerCase());
  const symbols = (url.searchParams.get("symbols") ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  const { items, updated_at } = await querySignalLog(env.DB, { limit, symbols, latest });
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, count: items.length, items, updated_at });
}

// ------------------------------------------------- 空态兼容接口（已删大表） ----

async function handleEvents(request: Request, env: Env): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    items: [],
    updated_at: null,
    degraded: true,
    note: "事件大表已在 0009 迁移删除；事件数据改由本地/GitHub Actions 生成或 AI 语义分析",
  });
}

async function handleRegime(request: Request, env: Env): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    snapshot: null,
    updated_at: null,
    degraded: true,
    note: "regime 大表已删除；云端以 /api/market-overview 的指数/宽度/主板块实时数据为准",
  });
}

async function handleSummaryLatest(request: Request, env: Env): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    midday: null,
    close: null,
    updated_at: null,
    degraded: true,
    note: "午评/收评改由前端基于 market-overview 或 /api/ai/insight 生成",
  });
}

async function handleSummaries(request: Request, env: Env): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, count: 0, items: [], updated_at: null, degraded: true });
}

async function handleMarketHistory(request: Request, env: Env): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, count: 0, items: [], updated_at: null, degraded: true });
}

async function handleBacktest(request: Request, env: Env): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    snapshot: null,
    updated_at: null,
    degraded: true,
    note: "历史回测由本地训练管道生成，云端轻量库不再存储",
  });
}

/** GET /api/public/summary：公开市场摘要（供嵌入/首页轻量展示）。 */
async function handlePublicSummary(): Promise<Response> {
  const overview = await buildMarketOverview({});
  const legacy = toLegacyMarketSnapshot(overview);
  const sentiment = (legacy["sentiment"] ?? {}) as Record<string, unknown>;
  const indices = Array.isArray(legacy["indices"]) ? (legacy["indices"] as Array<Record<string, unknown>>) : [];
  const marketDirection = (legacy["market_direction"] ?? {}) as Record<string, unknown>;
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    asof: overview["asof"],
    trade_date: overview["trade_date"],
    session: overview["session"],
    status: String(overview["quality"] && (overview["quality"] as Record<string, unknown>)["degraded"] ? "partial" : "ok"),
    market: {
      indices: indices.map((item) => ({
        code: item["code"],
        name: item["name"],
        price: item["price"],
        pct_chg: item["pct_chg"],
        direction: item["direction"],
      })),
      sentiment: { score: sentiment["score"], label: sentiment["label"], breadth: sentiment["breadth"] },
      market_direction: marketDirection,
    },
    disclaimer: "研究用途，不构成投资建议",
  });
}

// ------------------------------------------------------------------ SSE ----

function handleStream(request: Request, env: Env): Response {
  const encoder = new TextEncoder();
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let lifetimeTimer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;
  let busy = false;
  let lastSignature = "";

  const cleanup = () => {
    if (closed) return;
    closed = true;
    if (heartbeatTimer !== null) clearInterval(heartbeatTimer);
    if (pollTimer !== null) clearInterval(pollTimer);
    if (lifetimeTimer !== null) clearTimeout(lifetimeTimer);
    heartbeatTimer = null;
    pollTimer = null;
    lifetimeTimer = null;
  };

  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const push = (chunk: string) => {
        if (closed) return;
        try {
          controller.enqueue(encoder.encode(chunk));
        } catch {
          cleanup();
        }
      };
      const emit = (event: string, data: unknown, id?: string) => {
        push(`${id ? `id: ${id}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
      };
      const poll = async () => {
        if (closed || busy) return;
        busy = true;
        try {
          const { items, updated_at } = await querySignalLog(env.DB, { limit: 50 });
          const signature = `${updated_at ?? ""}|${items.length}|${items[0]?.signal_id ?? ""}`;
          if (signature === lastSignature) return;
          lastSignature = signature;
          emit("signals", { schema: SCHEMA_VERSION, items, updated_at, heartbeat_sec: SSE_HEARTBEAT_MS / 1000 }, updated_at ?? undefined);
        } catch (err) {
          emit("error", { schema: SCHEMA_VERSION, error: "query_failed", message: err instanceof Error ? err.message : String(err), now: nowShanghai() });
        } finally {
          busy = false;
        }
      };
      emit("hello", {
        schema: SCHEMA_VERSION,
        service: SERVICE,
        version: env.WORKER_VERSION ?? VERSION,
        heartbeat_sec: SSE_HEARTBEAT_MS / 1000,
        poll_sec: SSE_POLL_MS / 1000,
        now: nowShanghai(),
      });
      void poll();
      heartbeatTimer = setInterval(() => push(`: heartbeat ${nowShanghai()}\n\n`), SSE_HEARTBEAT_MS);
      pollTimer = setInterval(() => {
        void poll();
      }, SSE_POLL_MS);
      lifetimeTimer = setTimeout(() => {
        emit("bye", { schema: SCHEMA_VERSION, reason: "max_lifetime", now: nowShanghai() });
        cleanup();
        try {
          controller.close();
        } catch {
          /* already closed */
        }
      }, SSE_MAX_LIFETIME_MS);
      request.signal.addEventListener("abort", () => {
        cleanup();
        try {
          controller.close();
        } catch {
          /* already closed */
        }
      });
    },
    cancel() {
      cleanup();
    },
  });

  return new Response(stream, {
    status: 200,
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    },
  });
}

// ---------------------------------------------------------------- static ----

async function handleStatic(request: Request, env: Env, path: string): Promise<Response> {
  if (env.ASSETS) {
    try {
      const assetResponse = await env.ASSETS.fetch(request);
      if (assetResponse.status !== 404) return assetResponse;
    } catch (err) {
      console.error(`[worker] ASSETS.fetch 失败：${err instanceof Error ? err.message : String(err)}`);
    }
  }
  if (path === "/" || path === "/index.html") {
    return jsonResponse({
      schema: SCHEMA_VERSION,
      ok: true,
      service: SERVICE,
      message: "Worker 已启动；静态看板产物尚未提供（cloudflare/pages/ 由 frontend-dev 产出）。",
      api: {
        config: "GET /api/config",
        verify: "POST /api/verify",
        health: "GET /api/health",
        overview: "GET /api/market-overview",
        market: "GET /api/market",
        kline: "GET /api/kline?symbol=600519",
        intraday: "GET /api/intraday?code=sh000001",
        signals: "GET /api/signals",
        stream: "GET /api/stream",
        ai: "GET /api/ai/insight",
      },
    });
  }
  return errorResponse(404, "not_found", `静态资源不存在：${path}`);
}

// ---------------------------------------------------------------- router ----

function handlePreflight(request: Request): Response {
  const origin = checkSameOrigin(request);
  if (!origin.ok) return errorResponse(403, "cross_origin_forbidden", "只允许同源请求");
  const headers: Record<string, string> = {
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type, authorization, x-ingest-token",
    "access-control-max-age": "86400",
  };
  if (origin.origin) {
    headers["access-control-allow-origin"] = origin.origin;
    headers["vary"] = "origin";
  }
  return new Response(null, { status: 204, headers });
}

/**
 * 安全门：
 * - 机器接口（POST /api/ingest、/api/daily-sync、/api/<name>/ingest）只认 token，不做 UA/Accept 反爬；
 * - 其余请求：UA 黑名单 / Accept / 同源 Origin -> 403；限流 -> 429；
 * - 人机验证在具体数据路由（cachedRoute / requireChallenge）里执行。
 */
async function securityGate(request: Request, env: Env, path: string): Promise<Response | null> {
  const isMachineIngest =
    request.method === "POST" &&
    (path === "/api/ingest" || path === "/api/daily-sync" || /^\/api\/[a-z][a-z0-9-]*\/ingest$/.test(path));
  if (!isMachineIngest) {
    const ua = checkUserAgent(request);
    if (!ua.ok) return forbiddenClient(ua.reason ?? "forbidden_user_agent", ua.message ?? "User-Agent 被拒绝");
    const accept = checkAcceptHeader(request);
    if (!accept.ok) return errorResponse(403, "bad_accept", accept.message ?? "Accept 头不合法", { reason: accept.reason });
    const origin = checkSameOrigin(request);
    if (!origin.ok) return errorResponse(403, "cross_origin_forbidden", "只允许同源请求");
  }
  const rate = await checkRateLimit(env, request);
  if (!rate.allowed) return rateLimitedResponse(rate);
  return null;
}

export default {
  async scheduled(event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(
      (async () => {
        try {
          const result = await handleScheduledCron(event.cron ?? "", env);
          console.log(`[cron] ${event.cron || "(default)"} 完成：${JSON.stringify({ ok: result["ok"] ?? true })}`);
        } catch (err) {
          console.error(`[cron] ${event.cron || "(default)"} 失败：${err instanceof Error ? err.message : String(err)}`);
        }
      })(),
    );
  },

  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    try {
      const url = new URL(request.url);
      const path = normalizePath(url.pathname);

      if (request.method === "OPTIONS") return applySecurityHeaders(handlePreflight(request));

      const blocked = await securityGate(request, env, path);
      if (blocked) return applySecurityHeaders(blocked);

      const routeWithGuard = async (allow: string[], fn: () => Promise<Response>): Promise<Response> => {
        const guard = methodGuard(request, allow);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await fn());
      };

      // --- 公开/安全 ---
      if (path === "/api/me") {
        return routeWithGuard(["GET"], async () =>
          jsonResponse({
            schema: SCHEMA_VERSION,
            ok: true,
            ip: clientIp(request),
            trusted: isTrustedIp(request, env),
            challenge_required: !isTrustedIp(request, env),
          }),
        );
      }
      if (path === "/api/config") return routeWithGuard(["GET"], async () => handleConfig(env));
      if (path === "/api/verify") return routeWithGuard(["POST"], async () => handleVerify(request, env));
      if (path === "/api/health") return routeWithGuard(["GET"], async () => handleHealth(env));

      // --- 数据接口（challenge + 边缘缓存） ---
      if (path === "/api/model") {
        return routeWithGuard(["GET"], async () =>
          applySecurityHeaders((await requireChallenge(request, env)) ?? handleModel()),
        );
      }
      if (path === "/api/signals") {
        return routeWithGuard(["GET"], async () => cachedRoute(request, env, 60, () => handleSignals(env, url)));
      }
      if (path === "/api/signal-log") {
        return routeWithGuard(["GET"], async () => cachedRoute(request, env, 60, () => handleSignalLogGet(request, env, url)));
      }
      if (path === "/api/stream") {
        return routeWithGuard(["GET"], async () => (await requireChallenge(request, env)) ?? handleStream(request, env));
      }
      if (path === "/api/stock") return routeWithGuard(["GET"], async () => handleStock(request, env, url));
      if (path === "/api/search") return routeWithGuard(["GET"], async () => {
        const challenge = await requireChallenge(request, env);
        if (challenge) return challenge;
        const q = (url.searchParams.get("q") ?? "").trim();
        if (!q) return jsonResponse({ schema: SCHEMA_VERSION, ok: true, query: q, items: [] });
        if (q.length > 24) return errorResponse(400, "invalid_query", "搜索关键词过长");
        const limitRaw = Number(url.searchParams.get("limit") ?? "12");
        const limit = Number.isFinite(limitRaw) ? Math.min(Math.max(Math.trunc(limitRaw), 1), 30) : 12;
        const items = await searchStocks(env, q, limit);
        return jsonResponse({ schema: SCHEMA_VERSION, ok: true, query: q, items });
      });

      // --- 市场全景 / 兼容 / 板块 / 分时 / K线 ---
      if (path === "/api/snapshot") {
        return routeWithGuard(["GET"], async () => {
          const cacheKey = "https://quant-edge-cache.local/api/snapshot";
          try {
            const cached = await caches.default.match(cacheKey);
            if (cached) return applySecurityHeaders(cached);
          } catch {
            /* ignore */
          }
          try {
            const resp = await fetch(
              "https://raw.githubusercontent.com/xiongkong520/personal-quant/main/reports/snapshot.json",
              { headers: { "user-agent": "quant-worker/1.0" }, signal: AbortSignal.timeout(12000) },
            );
            if (!resp.ok) return errorResponse(503, "data_unavailable", "快照暂不可用，请稍后重试");
            const body = await resp.text();
            const out = new Response(body, {
              headers: { "content-type": "application/json; charset=utf-8", "cache-control": "public, max-age=60" },
            });
            try {
              await caches.default.put(cacheKey, out.clone());
            } catch {
              /* ignore */
            }
            return applySecurityHeaders(out);
          } catch {
            return errorResponse(503, "data_unavailable", "快照暂不可用，请稍后重试");
          }
        });
      }
      if (path === "/api/market-overview") {
        return routeWithGuard(["GET"], async () =>
          cachedRoute(request, env, 60, () => handleMarketOverview(request, env, url), cacheDecorate(60)),
        );
      }
      if (path === "/api/market") {
        return routeWithGuard(["GET"], async () =>
          cachedRoute(request, env, 60, () => handleMarket(), cacheDecorate(60)),
        );
      }
      if (path === "/api/boards") {
        return routeWithGuard(["GET"], async () => cachedRoute(request, env, 120, () => handleBoards(url)));
      }
      if (path === "/api/intraday") {
        return routeWithGuard(["GET"], async () => cachedRoute(request, env, 30, () => handleIntraday(url)));
      }
      if (path === "/api/market/history") {
        return routeWithGuard(["GET"], async () => cachedRoute(request, env, 120, () => handleMarketHistory(request, env)));
      }
      if (path === "/api/kline") {
        return routeWithGuard(["GET"], async () => cachedRoute(request, env, 60, () => klineHandler(url)));
      }

      // --- 模拟盘 / 轻量表 ---
      if (path === "/api/paper") return routeWithGuard(["GET"], async () => handlePaper(request, env));
      if (path === "/api/account") return routeWithGuard(["GET"], async () => handleAccountGet(request, env));
      if (path === "/api/positions") return routeWithGuard(["GET"], async () => handlePositionsGet(request, env));
      if (path === "/api/orders") return routeWithGuard(["GET"], async () => handleOrdersGet(request, env, url));

      // --- 空态兼容（大表已删） ---
      if (path === "/api/events") return routeWithGuard(["GET"], async () => cachedRoute(request, env, 60, () => handleEvents(request, env)));
      if (path === "/api/regime") return routeWithGuard(["GET"], async () => cachedRoute(request, env, 60, () => handleRegime(request, env)));
      if (path === "/api/summary/latest") {
        return routeWithGuard(["GET"], async () => cachedRoute(request, env, 60, () => handleSummaryLatest(request, env)));
      }
      if (path === "/api/summaries") return routeWithGuard(["GET"], async () => handleSummaries(request, env));
      if (path === "/api/backtest") return routeWithGuard(["GET"], async () => handleBacktest(request, env));
      if (path === "/api/public/summary") {
        return routeWithGuard(["GET"], async () => cachedRoute(request, env, 60, () => handlePublicSummary()));
      }

      // --- AI ---
      if (path === "/api/ai/insight") {
        return routeWithGuard(["GET"], async () => {
          const challenge = await requireChallenge(request, env);
          if (challenge) return challenge;
          const insight = await getAiInsight(env);
          if (!insight) return jsonResponse({ schema: SCHEMA_VERSION, ok: false, message: "AI 洞察尚未生成" }, 404);
          return jsonResponse({ schema: SCHEMA_VERSION, ok: true, insight, updated_at: insight.generated_at });
        });
      }
      if (path === "/api/ai/generate") {
        return routeWithGuard(["POST"], async () => {
          const auth = requireMachineToken(request, env);
          if (auth) return auth;
          try {
            const insight = await generateAiInsight(env, true);
            return jsonResponse({ schema: SCHEMA_VERSION, ok: true, insight });
          } catch {
            return errorResponse(503, "ai_unavailable", "AI 服务暂不可用，请稍后重试");
          }
        });
      }

      // --- ingest 机器接口 ---
      if (path === "/api/ingest") return routeWithGuard(["POST"], async () => handleIngest(request, env));
      if (path === "/api/signal-log/ingest") return routeWithGuard(["POST"], async () => handleSignalLogIngest(request, env));
      if (path === "/api/account/ingest") return routeWithGuard(["POST"], async () => handleAccountIngest(request, env));
      if (path === "/api/positions/ingest") return routeWithGuard(["POST"], async () => handlePositionsIngest(request, env));
      if (path === "/api/orders/ingest") return routeWithGuard(["POST"], async () => handleOrdersIngest(request, env));
      if (path === "/api/stock-names/ingest") return routeWithGuard(["POST"], async () => handleStockNamesIngest(request, env));
      if (path === "/api/paper/ingest") return routeWithGuard(["POST"], async () => handlePaperIngest(request, env));
      if (path === "/api/daily-sync") {
        return routeWithGuard(["POST"], async () => {
          const auth = requireMachineToken(request, env);
          if (auth) return auth;
          let mode = (url.searchParams.get("session") ?? "").trim();
          if (!mode) {
            try {
              const body = (await request.json()) as Record<string, unknown> | null;
              const raw = body?.["session"] ?? body?.["mode"];
              if (typeof raw === "string") mode = raw.trim();
            } catch {
              /* default all */
            }
          }
          if (!mode) mode = "all";
          if (!["all", "close", "midday", "intraday"].includes(mode)) {
            return errorResponse(400, "invalid_mode", "session 只支持 all / close / midday / intraday");
          }
          const result = await runManualDailySync(env, mode as "all" | "close" | "midday" | "intraday");
          return jsonResponse(result);
        });
      }
      // 已废弃的旧 ingest：200 兼容响应，避免旧脚本 500
      if (path === "/api/kline/ingest") return routeWithGuard(["POST"], async () => deprecatedIngest("K 线入库", "云端改实时 /api/kline，不再落 D1"));
      if (path === "/api/market/ingest") return routeWithGuard(["POST"], async () => deprecatedIngest("市场快照入库", "市场全景改 /api/market-overview 实时+缓存"));
      if (path === "/api/regime/ingest") return routeWithGuard(["POST"], async () => deprecatedIngest("regime 入库", "regime 表已删除，改 /api/market-overview"));
      if (path === "/api/events/ingest") return routeWithGuard(["POST"], async () => deprecatedIngest("事件入库", "事件表已删除，事件数据改本地/GitHub Actions 或 AI"));
      if (path === "/api/backtest/ingest") return routeWithGuard(["POST"], async () => deprecatedIngest("回测入库", "回测由本地训练管道产出，云端不再存储"));
      if (path === "/api/market/refresh") {
        return routeWithGuard(["POST"], async () => {
          const auth = requireMachineToken(request, env);
          if (auth) return auth;
          const result = await refreshMarketLive(env, true);
          return jsonResponse({ schema: SCHEMA_VERSION, ok: result.ok, reason: result.reason ?? null });
        });
      }

      if (path.startsWith("/api/")) {
        return applySecurityHeaders(errorResponse(404, "not_found", `未知 API 端点：${path}`));
      }
      return applySecurityHeaders(await handleStatic(request, env, path));
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.error(`[worker] 未捕获异常：${message}`);
      return applySecurityHeaders(errorResponse(500, "internal_error", message));
    }
  },
};
