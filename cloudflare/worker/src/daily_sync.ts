/**
 * 云端数据自更新（v2 · D1 瘦身后）。
 *
 * 0009 迁移删除历史大表后，Worker Cron 不再抓 40 只 K 线 / 事件 / 板块 / regime 写 D1，
 * 而是改为**预热边缘缓存**（Cache API + D1 edge_cache 二级）：
 *   - Cron 定时刷新 /api/market-overview（5 指数 + 宽度 + 板块 Top10）与兼容接口 /api/market；
 *   - 用户/前端 60s 轮询直接命中缓存，不穿透外部行情源、不打爆东财/新浪；
 *   - 历史 K 线与回测数据由本地训练管道产出（Parquet / 本地服务），云端只保留实时视图。
 *
 * 保留旧函数名（syncDailyClose / buildMarketSummary / refreshIntradayIndex / runManualDailySync /
 * handleScheduledCron），供 index.ts 路由与 Cron 分发继续调用，行为改为轻量缓存预热。
 */
import { buildMarketOverview, toLegacyMarketSnapshot } from "./market_overview.ts";
import { putCacheEntry } from "./cache.ts";
import { nowIsoShanghai } from "./market_live.ts";
import type { Env } from "./types.ts";

export interface SyncStep {
  name: string;
  ok: boolean;
  ms: number;
  error?: string;
  detail?: Record<string, unknown>;
}

export interface DailySyncResult {
  ok: boolean;
  asof: string;
  trade_date: string;
  duration_ms: number;
  steps: SyncStep[];
  warnings: string[];
  asofs: Record<string, string | null>;
}

/** 部署后的公开地址（用于生成与前端一致的缓存 key；可用 env.WORKER_PUBLIC_URL 覆盖）。 */
function publicBase(env: Env): string {
  return (env.WORKER_PUBLIC_URL ?? "https://personal-quant-worker.xiongkong520.workers.dev").replace(/\/+$/, "");
}

async function timed<T>(steps: SyncStep[], name: string, fn: () => Promise<T>, detail?: (value: T) => Record<string, unknown>): Promise<T | null> {
  const startedAt = Date.now();
  try {
    const value = await fn();
    const step: SyncStep = { name, ok: true, ms: Date.now() - startedAt };
    if (detail) step.detail = detail(value);
    steps.push(step);
    return value;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    steps.push({ name, ok: false, ms: Date.now() - startedAt, error: message });
    console.warn(`[daily_sync] ${name} 失败：${message}`);
    return null;
  }
}

/**
 * 预热市场全景缓存（核心）：抓一次实时源 -> 写 /api/market-overview 与 /api/market 的 60s 缓存。
 * 返回 { trade_date, session, asof, indices, degraded }。
 */
export async function warmMarketOverview(env: Env): Promise<{
  ok: boolean;
  asof: string;
  trade_date: string;
  session: string;
  indices: number;
  degraded: boolean;
  errors: string[];
}> {
  if (!env.DB) throw new Error("D1 未绑定");
  const overview = await buildMarketOverview({});
  const base = publicBase(env);
  const asof = String(overview["asof"] ?? nowIsoShanghai());
  const tradeDate = String(overview["trade_date"] ?? nowIsoShanghai().slice(0, 10));
  const session = String(overview["session"] ?? "closed");
  const quality = (overview["quality"] ?? {}) as Record<string, unknown>;
  await putCacheEntry(`${base}/api/market-overview`, overview, 60, env.DB);

  const legacy = {
    schema: 1,
    ok: true,
    snapshot: toLegacyMarketSnapshot(overview),
    updated_at: asof,
  };
  await putCacheEntry(`${base}/api/market`, legacy, 60, env.DB);
  return {
    ok: true,
    asof,
    trade_date: tradeDate,
    session,
    indices: Array.isArray(overview["indices"]) ? (overview["indices"] as unknown[]).length : 0,
    degraded: Boolean(quality["degraded"]),
    errors: Array.isArray(quality["errors"]) ? (quality["errors"] as string[]).slice(0, 5) : [],
  };
}

/** 收盘同步（Cron `40 7 * * 1-5`）：轻量预热市场全景缓存。 */
export async function syncDailyClose(env: Env): Promise<DailySyncResult> {
  const startedAt = Date.now();
  const steps: SyncStep[] = [];
  const warnings: string[] = [];
  const asofs: Record<string, string | null> = { market_overview: null, market: null };
  const warm = await timed(steps, "warm_market_overview", async () => warmMarketOverview(env), (value) => ({ ...value }));
  if (warm) {
    asofs.market_overview = warm.asof;
    asofs.market = warm.asof;
    if (warm.degraded) warnings.push(`部分数据源降级：${warm.errors.join("; ")}`);
  }
  const ok = steps.some((step) => step.ok);
  return {
    ok,
    asof: nowIsoShanghai(),
    trade_date: warm?.trade_date ?? nowIsoShanghai().slice(0, 10),
    duration_ms: Date.now() - startedAt,
    steps,
    warnings,
    asofs,
  };
}

/** 盘中分时：D1 瘦身后不再落库；/api/intraday 改为按需实时拉取 + 30s 边缘缓存。 */
export async function refreshIntradayIndex(_env: Env, _force = false): Promise<{ ok: boolean; updated: number; skipped: string[]; errors: string[] }> {
  return { ok: false, updated: 0, skipped: ["on_demand_api_intraday"], errors: [] };
}

/** 午评/收评：轻量表方案下由 AI/前端按市场全景实时生成，云端不再落库摘要。 */
export async function buildMarketSummary(_env: Env, session: "midday" | "close"): Promise<{ ok: boolean; trade_date: string; asof: string; summary_text: string; duration_ms: number; reason?: string }> {
  return {
    ok: false,
    trade_date: nowIsoShanghai().slice(0, 10),
    asof: nowIsoShanghai(),
    summary_text: "",
    duration_ms: 0,
    reason: `summary_moved_to_cache_or_ai (${session})`,
  };
}

/** 盘中缓存刷新（Cron `* /5`）。 */
export async function runIntradayRefresh(env: Env): Promise<Record<string, unknown>> {
  try {
    const warm = await warmMarketOverview(env);
    return { ok: true, warm };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.warn(`[daily_sync] 盘中缓存预热失败：${message}`);
    return { ok: false, error: message };
  }
}

/** 手动触发（POST /api/daily-sync）：预热市场全景缓存（支持 mode 参数兼容旧脚本）。 */
export async function runManualDailySync(env: Env, mode: "all" | "close" | "midday" | "intraday" = "all"): Promise<Record<string, unknown>> {
  const startedAt = Date.now();
  const steps: SyncStep[] = [];
  const warm = await timed(steps, "warm_market_overview", async () => warmMarketOverview(env), (value) => ({ ...value }));
  return {
    schema: 1,
    ok: steps.every((step) => step.ok),
    mode,
    started_at: nowIsoShanghai(),
    finished_at: nowIsoShanghai(),
    duration_ms: Date.now() - startedAt,
    steps,
    result: warm,
  };
}

/** Cron 分发（index.ts scheduled 调用）：所有 cron 统一做缓存预热，保留旧 cron 表达式不变。 */
export async function handleScheduledCron(cron: string, env: Env): Promise<Record<string, unknown>> {
  const result = await runManualDailySync(env, "all");
  console.log(
    `[cron] ${cron || "(default)"} 缓存预热：${JSON.stringify({
      ok: result["ok"],
      steps: (result["steps"] as SyncStep[]).map((step) => ({ name: step.name, ok: step.ok, ms: step.ms, error: step.error })),
    })}`,
  );
  return result;
}
