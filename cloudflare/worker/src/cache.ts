/**
 * 边缘缓存（模块一）：Cache API 一级 + D1 二级回退。
 *
 * 目标：
 * - 前端 60s 轮询只读缓存聚合快照，不穿透直连外部行情源；
 * - 上游限流 / 休市时返回「最近一次有效快照」（last-known-good），标注 STALE；
 * - 统一响应头：X-Cache: HIT|MISS|STALE、X-Cache-Age（秒）。
 *
 * 为什么需要 D1 回退（2026-11 实测）：
 * Cloudflare 官方文档明确 `*.workers.dev` 部署上的 Cache API 操作 **no impact**
 * （只有自定义域名 / Pages Functions 才真正生效）。本站当前入口是 workers.dev，
 * 因此仅用 Cache API 时连续两次请求都是 MISS。这里增加 D1 `edge_cache` 表作为二级缓存，
 * 保证 workers.dev 也能 MISS→HIT；自定义域名接入后 Cache API 会优先生效（仍保留 D1 兜底）。
 *
 * - fresh 快照 TTL = ttlSec（默认 60s）；
 * - last-known-good（lkg）保留 24h，仅在上游失败时兜底读取；
 * - 非 200（challenge/参数错误）通过 PassthroughError 原样上抛，不写缓存、不拿 lkg 冒充。
 */
const CACHE_ORIGIN = "https://quant-edge-cache.local";
const LKG_TTL_SEC = 86400;
const GC_INTERVAL_MS = 3600_000;

export interface CachedResult {
  data: unknown;
  cacheState: "HIT" | "MISS" | "STALE";
  ageSec: number;
}

/**
 * 透传错误：loader 返回非 200（401 challenge / 400 参数错误 / 429 等）时抛出，
 * withEdgeCache 必须原样上抛，**不能**用 last-known-good 快照冒充响应
 * （否则未通过人机验证的请求会拿到缓存数据）。
 */
export class PassthroughError extends Error {
  readonly response: Response;
  constructor(response: Response) {
    super(`passthrough:${response.status}`);
    this.name = "PassthroughError";
    this.response = response;
  }
}

function cacheKey(url: string, suffix = ""): Request {
  const parsed = new URL(url);
  return new Request(`${CACHE_ORIGIN}${parsed.pathname}${parsed.search}${suffix}`, { method: "GET" });
}

function d1FreshKey(url: string): string {
  return `fresh:${url}`;
}

function d1LkgKey(url: string): string {
  return `lkg:${url}`;
}

// ---------------------------------------------------------------- Cache API ----

async function readCacheApi(request: Request): Promise<{ data: unknown; ageSec: number } | null> {
  try {
    const cache = caches.default;
    const hit = await cache.match(request);
    if (!hit) return null;
    const payload = await hit.json<unknown>();
    const cachedAt = Number(hit.headers.get("x-cached-at") || 0);
    const ageSec = cachedAt > 0 ? Math.max(0, Math.floor((Date.now() - cachedAt) / 1000)) : 0;
    return { data: payload, ageSec };
  } catch {
    return null;
  }
}

async function writeCacheApi(request: Request, data: unknown, ttlSec: number): Promise<void> {
  try {
    const cache = caches.default;
    const body = JSON.stringify(data);
    const headers = new Headers({
      "content-type": "application/json; charset=utf-8",
      "cache-control": `public, max-age=${Math.max(ttlSec, 1)}`,
      "x-cached-at": String(Date.now()),
    });
    await cache.put(request, new Response(body, { headers }));
  } catch {
    // workers.dev 上 Cache API no-op / put 抛错都不影响主流程（D1 二级缓存兜底）
  }
}

// ------------------------------------------------------------- D1 二级缓存 ----

interface D1CacheRow {
  payload: string;
  cached_at: number;
  expires_at: number;
}

async function readD1(db: D1Database | undefined, key: string, now: number): Promise<{ data: unknown; ageSec: number } | null> {
  if (!db) return null;
  try {
    const row = await db
      .prepare(`SELECT payload, cached_at, expires_at FROM edge_cache WHERE cache_key = ? AND expires_at > ? LIMIT 1`)
      .bind(key, now)
      .first<D1CacheRow>();
    if (!row) return null;
    return { data: JSON.parse(row.payload) as unknown, ageSec: Math.max(0, Math.floor((now - Number(row.cached_at)) / 1000)) };
  } catch {
    return null;
  }
}

async function writeD1(db: D1Database | undefined, key: string, data: unknown, ttlSec: number): Promise<void> {
  if (!db) return;
  try {
    const now = Date.now();
    await db
      .prepare(
        `INSERT INTO edge_cache (cache_key, payload, cached_at, expires_at) VALUES (?, ?, ?, ?)
         ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload,
           cached_at = excluded.cached_at, expires_at = excluded.expires_at`,
      )
      .bind(key, JSON.stringify(data), now, now + Math.max(ttlSec, 1) * 1000)
      .run();
  } catch {
    // 缓存失败不影响主流程
  }
}

async function gcD1(db: D1Database | undefined): Promise<void> {
  if (!db || Math.random() > 1 / 20) return;
  try {
    await db.prepare(`DELETE FROM edge_cache WHERE expires_at < ?`).bind(Date.now() - GC_INTERVAL_MS).run();
  } catch {
    // 忽略清理失败
  }
}

// ---------------------------------------------------------------- 对外 API ----

/**
 * 读取缓存或执行 loader。
 * - 命中 fresh 缓存（Cache API 或 D1）：HIT；
 * - 未命中：执行 loader；成功则写 fresh + lkg（MISS）；
 * - loader 失败：读 lkg 兜底（STALE），仍无则抛出原错误；
 * - loader 返回非 200（PassthroughError）：不读缓存、直接上抛。
 */
export async function withEdgeCache<T>(
  request: Request,
  ttlSec: number,
  loader: () => Promise<T>,
  db?: D1Database,
): Promise<CachedResult> {
  const url = request.url;
  const freshApiKey = cacheKey(url);
  const lkgApiKey = cacheKey(url, "&lkg=1");
  const now = Date.now();

  const apiFresh = await readCacheApi(freshApiKey);
  if (apiFresh) return { data: apiFresh.data as T, cacheState: "HIT", ageSec: apiFresh.ageSec };

  const d1Fresh = await readD1(db, d1FreshKey(url), now);
  if (d1Fresh) return { data: d1Fresh.data as T, cacheState: "HIT", ageSec: d1Fresh.ageSec };

  try {
    const data = await loader();
    await Promise.all([
      writeCacheApi(freshApiKey, data, ttlSec),
      writeCacheApi(lkgApiKey, data, LKG_TTL_SEC),
      writeD1(db, d1FreshKey(url), data, ttlSec),
      writeD1(db, d1LkgKey(url), data, LKG_TTL_SEC),
    ]);
    void gcD1(db);
    return { data, cacheState: "MISS", ageSec: 0 };
  } catch (err) {
    if (err instanceof PassthroughError) throw err;
    const apiLkg = await readCacheApi(lkgApiKey);
    if (apiLkg) return { data: apiLkg.data as T, cacheState: "STALE", ageSec: apiLkg.ageSec };
    const d1Lkg = await readD1(db, d1LkgKey(url), Date.now());
    if (d1Lkg) return { data: d1Lkg.data as T, cacheState: "STALE", ageSec: d1Lkg.ageSec };
    throw err;
  }
}

/** 给缓存响应补统一响应头（保留原 body 与 content-type）。 */
export function applyCacheHeaders(response: Response, result: CachedResult): Response {
  const headers = new Headers(response.headers);
  headers.set("x-cache", result.cacheState);
  headers.set("x-cache-age", String(result.ageSec));
  headers.set("cache-control", "no-store");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

/** 构造 JSON 响应（供 loader 返回统一结构）。 */
export function jsonResult(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
