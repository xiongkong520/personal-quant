/**
 * 个人量化工作台 · Cloudflare Worker 入口（含安全层）
 *
 * 端点：
 *   GET  /api/config    公开：Turnstile sitekey + 验证说明（前端启动时读）
 *   POST /api/verify    公开：{token} -> Turnstile 校验通过后 Set-Cookie quant_pass（HMAC，1 小时）
 *   GET  /api/health    公开：健康检查（D1/模型/安全配置；监控请带浏览器 UA + Accept）
 *   GET  /api/signals   需 quant_pass：{schema, items, updated_at}；?symbols=&limit=&latest=1&status=
 *   GET  /api/stream    需 quant_pass：SSE 新信号推送，30s 心跳
 *   GET  /api/model     需 quant_pass：Worker 打包的 §5 模型权重（附加端点）
 *   POST /api/ingest    需 x-ingest-token / Bearer QUANT_INGEST_TOKEN：本地 Python 推送信号
 *   GET  /api/kline     需 quant_pass：个股日线（D1 klines，最多 250 根）?symbol=&limit=
 *   POST /api/kline/ingest  需 x-ingest-token：本地批量推送日线（幂等 upsert）
 *   GET  /api/regime    需 quant_pass：最新大盘状态/情绪/主线/仓位快照
 *   POST /api/regime/ingest 需 x-ingest-token：本地推送大盘状态快照
 *   其余路径                    静态资源（cloudflare/pages/ 由 frontend-dev 产出；空目录返回提示 JSON）
 *
 * 安全层（详见 src/security.ts）：
 *   - 反爬：UA 黑名单 + Accept 校验 → 403；跨域 Origin → 403/不带 CORS；安全响应头；
 *   - 限流：cf-connecting-ip 每分钟 60 次（env.RATE_LIMIT_PER_MIN 可调；KV 可选）→ 429 + Retry-After；
 *   - 人机验证：/api/signals、/api/stream、/api/model 要求 quant_pass cookie（未通过 401 + {"challenge":true}）。
 *   - /api/ingest 是 token 鉴权的机器接口，不受 UA/Accept 反爬限制（Python httpx 直接推送）。
 *
 * 所有密钥只从 env 读取（本地 .dev.vars / 部署 wrangler secret）；代码里只有 Cloudflare 官方测试密钥。
 */
import modelJson from "../models/model.json" with { type: "json" };

import { confidenceOf, directionOf, predictProba, validateModel, type QuantModel } from "./model.ts";
import { refreshMarketLive } from "./market_live.ts";
import {
  d1Health,
  insertJsonSnapshot,
  insertMarketSnapshot,
  insertSignals,
  queryEvents,
  queryLatestJsonSnapshot,
  queryLatestMarket,
  querySignals,
  queryKlines,
  queryStockName,
  upsertEvents,
  upsertKlines,
  upsertStockNames,
} from "./db.ts";
import type { KlineRow } from "./db.ts";
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
const VERSION = "0.2.0";
const SCHEMA_VERSION = 1;

/** SSE 心跳间隔（task 要求 30s）。 */
const SSE_HEARTBEAT_MS = 30_000;
/** SSE 轮询 D1 找新信号的间隔。 */
const SSE_POLL_MS = 5_000;
/** SSE 单连接最长存活时间，到点主动结束，EventSource 会自动重连。 */
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

/** 未通过人机验证：401 + challenge:true（前端据此弹 Turnstile 卡片，不当错误处理）。 */
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

/** 推送 token 只能来自环境变量；兼容旧名 INGEST_TOKEN。 */
function readIngestToken(env: Env): string {
  return (env.QUANT_INGEST_TOKEN ?? env.INGEST_TOKEN ?? "").trim();
}

// ---------------------------------------------------------------- health ----

async function handleHealth(env: Env): Promise<Response> {
  // 对外只暴露运行状态；数据库/后端实现细节不写入响应（需要时看服务端日志）
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    service: SERVICE,
    version: env.WORKER_VERSION ?? VERSION,
    now: nowShanghai(),
    model_version: QUANT_MODEL.version,
  });
}

/** GET /api/me：访客自己的 IP 与是否可信（开发测试用；不暴露内部实现）。 */
function handleMe(request: Request, env: Env): Response {
  const trusted = isTrustedIp(request, env);
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    ip: clientIp(request),
    trusted,
    challenge_required: !trusted,
  });
}

// ---------------------------------------------------------------- config ----

/** GET /api/config：前端启动时判断是否需要 Turnstile（始终 challenge=true，避免本地/生产行为不一致）。 */
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
    return errorResponse(503, "server_misconfigured", "验证服务暂不可用，请稍后重试");
  }
  const parsed = await readTokenField(request);
  if (parsed.error || !parsed.token) return errorResponse(400, "invalid_body", parsed.error ?? "缺少 token");

  const result = await verifyTurnstileToken(env, parsed.token, clientIp(request));
  if (!result.ok) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "challenge_failed", errors: result.errors }, 403);
  }
  const pass = await issuePassCookie(env);
  if (!pass) {
    return errorResponse(503, "server_misconfigured", "验证服务暂不可用，请稍后重试");
  }
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
      note: "模型权重已同步。",
      model: QUANT_MODEL,
      parity_probe: { zero_features: probe },
    },
    modelError === null ? 200 : 500,
  );
}

// -------------------------------------------------------------- signals ----

async function handleSignals(env: Env, url: URL): Promise<Response> {
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");

  const limitRaw = Number(url.searchParams.get("limit") ?? "200");
  const limit = Number.isFinite(limitRaw) ? Math.min(Math.max(Math.trunc(limitRaw), 1), 500) : 200;

  const symbolsRaw = (url.searchParams.get("symbols") ?? "").trim();
  const symbols = symbolsRaw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
  if (symbols.length > 50) return errorResponse(400, "invalid_symbols", "symbols 最多 50 个");
  const badSymbol = symbols.find((s) => !/^\d{6}$/.test(s));
  if (badSymbol) return errorResponse(400, "invalid_symbols", `symbols 必须是 6 位数字代码，收到 ${JSON.stringify(badSymbol)}`);

  const latest = ["1", "true", "yes"].includes((url.searchParams.get("latest") ?? "").toLowerCase());
  const statusRaw = url.searchParams.get("status");
  if (statusRaw && !["ok", "stale", "warming_up"].includes(statusRaw)) {
    return errorResponse(400, "invalid_status", "status 只支持 ok / stale / warming_up");
  }

  const { items, updated_at } = await querySignals(env.DB, { limit, symbols, latest, status: statusRaw ?? undefined });
  return jsonResponse({ schema: SCHEMA_VERSION, items, updated_at });
}

// --------------------------------------------------------------- ingest ----

async function handleIngest(request: Request, env: Env): Promise<Response> {
  const expected = readIngestToken(env);
  if (!expected) {
    return errorResponse(
      503,
      "ingest_token_not_configured",
      "未配置推送 token：本地在 cloudflare/worker/.dev.vars 写 QUANT_INGEST_TOKEN=...；部署用 wrangler secret put QUANT_INGEST_TOKEN",
    );
  }
  const provided = extractIngestToken(request);
  if (!provided) {
    return jsonResponse(
      { schema: SCHEMA_VERSION, ok: false, error: "unauthorized", message: "请求未授权" },
      401,
      { "www-authenticate": "Bearer" },
    );
  }
  if (!timingSafeEqual(provided, expected)) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "unauthorized", message: "token 无效" }, 401, {
      "www-authenticate": "Bearer",
    });
  }
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");

  const declaredLength = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(declaredLength) && declaredLength > MAX_INGEST_BYTES) {
    return errorResponse(413, "payload_too_large", `单次推送不能超过 ${MAX_INGEST_BYTES} 字节`, { limit_bytes: MAX_INGEST_BYTES });
  }

  let text: string;
  try {
    text = await request.text();
  } catch (err) {
    return errorResponse(400, "invalid_body", `读取请求体失败：${err instanceof Error ? err.message : String(err)}`);
  }
  if (text.length > MAX_INGEST_BYTES) {
    return errorResponse(413, "payload_too_large", `单次推送不能超过 ${MAX_INGEST_BYTES} 字节`, { limit_bytes: MAX_INGEST_BYTES });
  }

  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch (err) {
    return errorResponse(400, "invalid_json", `请求体不是合法 JSON：${err instanceof Error ? err.message : String(err)}`);
  }

  const parsed = parseIngestBody(body);
  if (parsed.error) return errorResponse(400, "invalid_body", parsed.error);

  const nowIso = nowShanghai();
  const rows: SignalRow[] = [];
  const acceptedWarnings: { index: number; warnings: string[] }[] = [];
  const rejected: { index: number; errors: string[]; warnings: string[] }[] = [];

  parsed.signals.forEach((raw, index) => {
    const result = normalizeSignal(raw, nowIso);
    if (result.row) {
      rows.push(result.row);
      if (result.warnings.length > 0) acceptedWarnings.push({ index, warnings: result.warnings });
    } else {
      rejected.push({ index, errors: result.errors, warnings: result.warnings });
    }
  });

  if (rows.length === 0) {
    return jsonResponse(
      {
        schema: SCHEMA_VERSION,
        ok: false,
        error: "all_signals_rejected",
        message: "所有信号都未通过校验",
        accepted: 0,
        rejected,
      },
      400,
    );
  }

  const inserted = await insertSignals(env.DB, rows);
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    accepted: inserted.accepted,
    rejected,
    warnings: acceptedWarnings,
    signal_ids: rows.map((r) => r.signal_id),
    updated_at: inserted.updated_at,
  });
}

// ---------------------------------------------------------------- stock ----

/** GET /api/stock?symbol=600519：个股快照（D1 中已推送的最新信号 + 名称）。 */
async function handleStock(request: Request, env: Env, url: URL): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  const symbol = (url.searchParams.get("symbol") ?? "").trim();
  if (!/^\d{6}$/.test(symbol)) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, message: "请输入 6 位股票代码" }, 400);
  }
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const result = await querySignals(env.DB, { symbols: [symbol], latest: true, limit: 1 });
  const name = (await queryStockName(env.DB, symbol)) ?? "";
  if (result.items.length === 0) {
    return jsonResponse(
      { schema: SCHEMA_VERSION, ok: false, symbol, name, message: "该代码暂无数据（当前仅覆盖已推送标的）" },
      404,
    );
  }
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    symbol,
    name,
    signal: result.items[0],
    updated_at: result.updated_at,
  });
}

/** POST /api/stock-names/ingest：本地推送代码→名称映射（x-ingest-token）。 */
async function handleStockNamesIngest(request: Request, env: Env): Promise<Response> {
  const token = readIngestToken(env);
  if (!token) return errorResponse(503, "server_misconfigured", "服务暂不可用，请稍后重试");
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const got = extractIngestToken(request);
  if (!got || !timingSafeEqual(got, token)) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "unauthorized", message: "请求未授权" }, 401);
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return errorResponse(400, "invalid_json", "请求体不是合法 JSON");
  }
  const items = (body as { items?: unknown })?.items;
  if (!Array.isArray(items)) return errorResponse(400, "invalid_body", "缺少 items");
  const rows = items
    .filter((item): item is { symbol: string; name: string } =>
      Boolean(item) && typeof (item as { symbol?: unknown }).symbol === "string" && typeof (item as { name?: unknown }).name === "string")
    .map((item) => ({ symbol: item.symbol, name: item.name }));
  const accepted = await upsertStockNames(env.DB, rows);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, accepted });
}

// -------------------------------------------------------- events / reports ----

/** POST /api/events/ingest：本地推送重大事件（利好/利空评分）。 */
async function handleEventsIngest(request: Request, env: Env): Promise<Response> {
  const token = readIngestToken(env);
  if (!token) return errorResponse(503, "server_misconfigured", "服务暂不可用，请稍后重试");
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const got = extractIngestToken(request);
  if (!got || !timingSafeEqual(got, token)) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "unauthorized", message: "请求未授权" }, 401);
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return errorResponse(400, "invalid_json", "请求体不是合法 JSON");
  }
  const items = (body as { items?: unknown })?.items;
  if (!Array.isArray(items)) return errorResponse(400, "invalid_body", "缺少 items");
  const accepted = await upsertEvents(env.DB, items as Array<Record<string, unknown>>);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, accepted });
}

/** GET /api/events?limit=60：最新重大事件（人机验证 / 可信 IP）。 */
async function handleEvents(request: Request, env: Env, url: URL): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const limit = Number(url.searchParams.get("limit") ?? "60");
  const items = await queryEvents(env.DB, Number.isFinite(limit) ? Math.trunc(limit) : 60);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, items, updated_at: nowShanghai() });
}

/** POST /api/backtest/ingest 或 /api/paper/ingest：推送快照。 */
async function handleSnapshotIngest(request: Request, env: Env, table: "backtest_reports" | "paper_snapshots"): Promise<Response> {
  const token = readIngestToken(env);
  if (!token) return errorResponse(503, "server_misconfigured", "服务暂不可用，请稍后重试");
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const got = extractIngestToken(request);
  if (!got || !timingSafeEqual(got, token)) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "unauthorized", message: "请求未授权" }, 401);
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return errorResponse(400, "invalid_json", "请求体不是合法 JSON");
  }
  const raw = body as Record<string, unknown> | null;
  const snapshot = raw && typeof raw["snapshot"] === "object" && raw["snapshot"] !== null ? raw["snapshot"] : raw;
  if (!snapshot || typeof snapshot !== "object") return errorResponse(400, "invalid_body", "缺少快照内容");
  const saved = await insertJsonSnapshot(env.DB, table, snapshot as Record<string, unknown>);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, accepted: 1, asof: saved.asof });
}

/** GET /api/backtest 或 /api/paper：最新快照（人机验证 / 可信 IP）。 */
async function handleSnapshotGet(request: Request, env: Env, table: "backtest_reports" | "paper_snapshots"): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const snapshot = await queryLatestJsonSnapshot(env.DB, table);
  if (!snapshot) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, message: "数据暂未同步" }, 404);
  }
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, snapshot, updated_at: nowShanghai() }, 200);
}

// ---------------------------------------------------------- public summary ----

/** GET /api/public/summary：公开只读大盘摘要（不含敏感数据，供移动端插件/第三方展示）。 */
async function handlePublicSummary(env: Env): Promise<Response> {
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const market = await queryLatestMarket(env.DB);
  const events = await queryEvents(env.DB, 3);
  const snapshot = (market?.snapshot ?? {}) as Record<string, unknown>;
  const indices = Array.isArray(snapshot["indices"]) ? (snapshot["indices"] as Array<Record<string, unknown>>) : [];
  const sentiment = (snapshot["sentiment"] ?? {}) as Record<string, unknown>;
  const direction = (snapshot["market_direction"] ?? {}) as Record<string, unknown>;
  const score = Number(sentiment["score"] ?? 50);
  const dir = String(direction["direction"] ?? "flat");
  let state = "range";
  let label = "震荡";
  let range = "2-4成";
  if (score < 20 || (score < 30 && dir === "down")) {
    state = "ice_watch";
    label = "冰点布局";
    range = "0-2成";
  } else if (score < 40 || dir === "down") {
    state = "lean_bear";
    label = "偏空";
    range = "0-2成";
  } else if (score > 80 && dir === "up") {
    state = "offense";
    label = "进攻";
    range = "6-8成";
  } else if (score > 60 && dir === "up") {
    state = "lean_bull";
    label = "偏多";
    range = "4-6成";
  }
  return jsonResponse({
    schema: 1,
    asof: market?.updated_at ?? nowShanghai(),
    status: snapshot["asof"] ? "ok" : "empty",
    market: {
      indices: indices.map((item) => ({
        code: item["code"],
        name: item["name"],
        price: item["price"],
        pct_chg: item["pct_chg"],
        direction: item["direction"],
      })),
      sentiment: { score: sentiment["score"], label: sentiment["label"], breadth: sentiment["breadth"] },
      market_direction: direction,
    },
    advice: {
      state,
      label,
      position_range: range,
      rationale: `情绪 ${score} · 指数方向 ${dir}`,
      disclaimer: "研究用途，不构成投资建议",
    },
    events: events.map((item) => ({
      title: item["title"],
      label: item["label"],
      sentiment: item["sentiment"],
      published_at: item["published_at"],
    })),
  });
}

// ---------------------------------------------------------------- market ----

/** POST /api/market/refresh：手动触发盘中刷新（x-ingest-token 保护，防滥用）。 */
async function handleMarketRefresh(request: Request, env: Env): Promise<Response> {
  const token = readIngestToken(env);
  if (!token) return errorResponse(503, "server_misconfigured", "服务暂不可用，请稍后重试");
  const got = extractIngestToken(request);
  if (!got || !timingSafeEqual(got, token)) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "unauthorized", message: "请求未授权" }, 401);
  }
  const result = await refreshMarketLive(env, true);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: result.ok, reason: result.reason ?? null }, 200);
}

/** POST /api/market/ingest：本地推送市场整体快照（x-ingest-token 鉴权）。 */
async function handleMarketIngest(request: Request, env: Env): Promise<Response> {
  const token = readIngestToken(env);
  if (!token) return errorResponse(503, "server_misconfigured", "服务暂不可用，请稍后重试");
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const got = extractIngestToken(request);
  if (!got || !timingSafeEqual(got, token)) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "unauthorized", message: "请求未授权" }, 401);
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return errorResponse(400, "invalid_json", "请求体不是合法 JSON");
  }
  const raw = body as Record<string, unknown> | null;
  const snapshot = raw && typeof raw["snapshot"] === "object" && raw["snapshot"] !== null ? raw["snapshot"] : raw;
  if (!snapshot || typeof snapshot !== "object") {
    return errorResponse(400, "invalid_body", "缺少市场快照内容");
  }
  const saved = await insertMarketSnapshot(env.DB, snapshot as Record<string, unknown>);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, accepted: 1, asof: saved.asof });
}

/** GET /api/market：最新市场整体快照（人机验证 / 可信 IP）。 */
async function handleMarket(request: Request, env: Env): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const latest = await queryLatestMarket(env.DB);
  if (!latest) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: true, snapshot: null, updated_at: null }, 200);
  }
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    snapshot: latest.snapshot,
    updated_at: latest.updated_at,
  });
}

// ------------------------------------------------------------------ kline ----

/** 单次推送最多行数（控制请求体大小；本地按 200 行/批推送）。 */
const KLINE_INGEST_MAX_ROWS = 2000;
/** GET 单次最多返回根数（任务要求 ≤250）。 */
const KLINE_QUERY_MAX_ROWS = 250;

function asKlineNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

/** POST /api/kline/ingest：本地批量推送日线（x-ingest-token 鉴权，幂等 upsert）。 */
async function handleKlineIngest(request: Request, env: Env): Promise<Response> {
  const token = readIngestToken(env);
  if (!token) return errorResponse(503, "server_misconfigured", "服务暂不可用，请稍后重试");
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const got = extractIngestToken(request);
  if (!got || !timingSafeEqual(got, token)) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "unauthorized", message: "请求未授权" }, 401);
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return errorResponse(400, "invalid_json", "请求体不是合法 JSON");
  }
  const rawItems = Array.isArray(body)
    ? body
    : (body as { items?: unknown } | null)?.items;
  if (!Array.isArray(rawItems)) return errorResponse(400, "invalid_body", "缺少 items");
  if (rawItems.length > KLINE_INGEST_MAX_ROWS) {
    return errorResponse(400, "too_many_rows", `单次最多 ${KLINE_INGEST_MAX_ROWS} 行`);
  }
  const rows: KlineRow[] = [];
  let skipped = 0;
  for (const item of rawItems) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      skipped += 1;
      continue;
    }
    const obj = item as Record<string, unknown>;
    const symbol = typeof obj["symbol"] === "string" ? obj["symbol"].trim() : "";
    const tradeDate = typeof obj["trade_date"] === "string" ? obj["trade_date"].trim() : "";
    const close = asKlineNumber(obj["close"]);
    if (!/^\d{6}$/.test(symbol) || !/^\d{4}-\d{2}-\d{2}$/.test(tradeDate) || close === null) {
      skipped += 1;
      continue;
    }
    rows.push({
      symbol,
      trade_date: tradeDate,
      open: asKlineNumber(obj["open"]),
      high: asKlineNumber(obj["high"]),
      low: asKlineNumber(obj["low"]),
      close,
      volume: asKlineNumber(obj["volume"]),
      amount: asKlineNumber(obj["amount"]),
    });
  }
  const accepted = await upsertKlines(env.DB, rows);
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, accepted, skipped });
}

/** GET /api/kline?symbol=600519&limit=120：最近日线（人机验证 / 可信 IP）。 */
async function handleKline(request: Request, env: Env, url: URL): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const symbol = (url.searchParams.get("symbol") ?? "").trim();
  if (!/^\d{6}$/.test(symbol)) return errorResponse(400, "invalid_symbol", "请输入 6 位股票代码");
  const limitRaw = Number(url.searchParams.get("limit") ?? url.searchParams.get("days") ?? "120");
  const limit = Number.isFinite(limitRaw)
    ? Math.min(Math.max(Math.trunc(limitRaw), 1), KLINE_QUERY_MAX_ROWS)
    : 120;
  const items = await queryKlines(env.DB, symbol, limit);
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    symbol,
    limit,
    count: items.length,
    items,
    updated_at: nowShanghai(),
  });
}

// ----------------------------------------------------------------- regime ----

/** POST /api/regime/ingest：本地推送大盘状态快照（x-ingest-token 鉴权）。 */
async function handleRegimeIngest(request: Request, env: Env): Promise<Response> {
  const token = readIngestToken(env);
  if (!token) return errorResponse(503, "server_misconfigured", "服务暂不可用，请稍后重试");
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const got = extractIngestToken(request);
  if (!got || !timingSafeEqual(got, token)) {
    return jsonResponse({ schema: SCHEMA_VERSION, ok: false, error: "unauthorized", message: "请求未授权" }, 401);
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return errorResponse(400, "invalid_json", "请求体不是合法 JSON");
  }
  const raw = body as Record<string, unknown> | null;
  const snapshot =
    raw && typeof raw["snapshot"] === "object" && raw["snapshot"] !== null ? raw["snapshot"] : raw;
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
    return errorResponse(400, "invalid_body", "缺少大盘状态快照内容");
  }
  const saved = await insertJsonSnapshot(env.DB, "regime_snapshots", snapshot as Record<string, unknown>);
  const state = (snapshot as Record<string, unknown>)["market_state"];
  const label = state && typeof state === "object" ? (state as Record<string, unknown>)["label"] : null;
  return jsonResponse({ schema: SCHEMA_VERSION, ok: true, accepted: 1, asof: saved.asof, state: label ?? null });
}

/** GET /api/regime：最新大盘状态快照（人机验证 / 可信 IP）。 */
async function handleRegime(request: Request, env: Env): Promise<Response> {
  const guard = await requireChallenge(request, env);
  if (guard) return guard;
  if (!env.DB) return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  const snapshot = await queryLatestJsonSnapshot(env.DB, "regime_snapshots");
  return jsonResponse({
    schema: SCHEMA_VERSION,
    ok: true,
    snapshot,
    updated_at: snapshot ? nowShanghai() : null,
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

  if (!env.DB) {
    return errorResponse(503, "data_unavailable", "数据读取失败，请稍后重试");
  }

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
          const { items, updated_at } = await querySignals(env.DB, { limit: 50 });
          const signature = `${updated_at ?? ""}|${items.length}|${items[0]?.signal_id ?? ""}`;
          if (signature === lastSignature) return;
          lastSignature = signature;
          emit("signals", { schema: SCHEMA_VERSION, items, updated_at, heartbeat_sec: SSE_HEARTBEAT_MS / 1000 }, updated_at ?? undefined);
        } catch (err) {
          emit("error", { schema: SCHEMA_VERSION, error: "query_failed", message: "数据读取失败，请稍后重试", now: nowShanghai() });
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

/**
 * 静态资源：Pages 产物由 frontend-dev 写在 cloudflare/pages/，通过 wrangler.toml [assets] 挂载。
 * 本地 dev 若 pages/ 目录还空着（或没有 ASSETS binding），返回提示 JSON。
 */
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
      message: "页面资源暂不可用，请稍后重试。",
      api: {
        config: "GET /api/config",
        verify: "POST /api/verify",
        health: "GET /api/health",
        signals: "GET /api/signals",
        stream: "GET /api/stream",
        ingest: "POST /api/ingest",
        model: "GET /api/model",
      },
    });
  }
  return errorResponse(404, "not_found", `静态资源不存在：${path}`);
}

// ---------------------------------------------------------------- router ----

/** 预检：只允许同源。 */
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
 * 安全门（在路由前执行）：
 *   1) 机器接口 POST /api/ingest 只认 token，不受人机验证限制；
 *   2) 人机验证：/api/signals、/api/stream、/api/model 需要 quant_pass cookie
 *      （Turnstile siteverify 通过后签发）；未验证返回 401 + challenge:true；
 *   3) 限流（按 IP 每分钟 N 次，默认 120）-> 429 + Retry-After。
 * 注：UA/Accept 黑名单已按用户要求取消（不误伤正常浏览器），
 *     边缘防护请在「自定义域名 + Cloudflare 免费套餐 WAF/Bot Fight Mode」层配置。
 */
async function securityGate(request: Request, env: Env, path: string): Promise<Response | null> {
  // 可信 IP 白名单：免人机验证与限流（开发测试/自动化验收，见 env.TRUSTED_IPS）
  if (isTrustedIp(request, env)) return null;
  const isMachineIngest = path === "/api/ingest" && request.method === "POST";
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
  /** Cron 触发：盘中刷新市场快照（写 D1，Pages 前端读取） */
  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(refreshMarketLive(env));
  },
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    try {
      const url = new URL(request.url);
      const path = normalizePath(url.pathname);

      if (request.method === "OPTIONS") return applySecurityHeaders(handlePreflight(request));

      const blocked = await securityGate(request, env, path);
      if (blocked) return applySecurityHeaders(blocked);

      if (path === "/api/me") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(handleMe(request, env));
      }
      if (path === "/api/config") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(handleConfig(env));
      }
      if (path === "/api/verify") {
        const guard = methodGuard(request, ["POST"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleVerify(request, env));
      }
      if (path === "/api/health") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleHealth(env));
      }

      if (path === "/api/model") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        const challenge = await requireChallenge(request, env);
        return applySecurityHeaders(challenge ?? handleModel());
      }
      if (path === "/api/signals") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        const challenge = await requireChallenge(request, env);
        if (challenge) return applySecurityHeaders(challenge);
        return applySecurityHeaders(await handleSignals(env, url));
      }
      if (path === "/api/stream") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        const challenge = await requireChallenge(request, env);
        if (challenge) return applySecurityHeaders(challenge);
        return applySecurityHeaders(handleStream(request, env));
      }
      if (path === "/api/stock") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleStock(request, env, url));
      }
      if (path === "/api/stock-names/ingest") {
        const guard = methodGuard(request, ["POST"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleStockNamesIngest(request, env));
      }
      if (path === "/api/public/summary") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handlePublicSummary(env));
      }
      if (path === "/api/events") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleEvents(request, env, url));
      }
      if (path === "/api/events/ingest") {
        const guard = methodGuard(request, ["POST"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleEventsIngest(request, env));
      }
      if (path === "/api/backtest") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleSnapshotGet(request, env, "backtest_reports"));
      }
      if (path === "/api/backtest/ingest") {
        const guard = methodGuard(request, ["POST"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleSnapshotIngest(request, env, "backtest_reports"));
      }
      if (path === "/api/paper") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleSnapshotGet(request, env, "paper_snapshots"));
      }
      if (path === "/api/paper/ingest") {
        const guard = methodGuard(request, ["POST"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleSnapshotIngest(request, env, "paper_snapshots"));
      }
      if (path === "/api/kline") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleKline(request, env, url));
      }
      if (path === "/api/kline/ingest") {
        const guard = methodGuard(request, ["POST"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleKlineIngest(request, env));
      }
      if (path === "/api/regime") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleRegime(request, env));
      }
      if (path === "/api/regime/ingest") {
        const guard = methodGuard(request, ["POST"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleRegimeIngest(request, env));
      }
      if (path === "/api/market/refresh") {
        const guard = methodGuard(request, ["POST"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleMarketRefresh(request, env));
      }
      if (path === "/api/market") {
        const guard = methodGuard(request, ["GET"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleMarket(request, env));
      }
      if (path === "/api/market/ingest") {
        const guard = methodGuard(request, ["POST"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleMarketIngest(request, env));
      }
      if (path === "/api/ingest") {
        const guard = methodGuard(request, ["POST"]);
        if (guard) return applySecurityHeaders(guard);
        return applySecurityHeaders(await handleIngest(request, env));
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
