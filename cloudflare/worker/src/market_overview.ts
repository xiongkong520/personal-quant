/**
 * GET /api/market-overview（模块二）：市场全景聚合接口（契约见 docs/UPGRADE_PLAN.md §3.1）。
 *
 * 数据源（全部已验证 Worker/本机可达）：
 * - 5 大指数实时：腾讯 qt.gtimg.cn（GBK，用 TextDecoder("gbk") 解码）
 *   sh000300 / sh000688 / sz399006 / sh000905 / sh000852（另取 sh000001+sz399001 算两市成交额）
 * - 全市场宽度：乐咕乐股 market-activity（涨跌家数/涨停/跌停/活跃度）
 * - 炸板率：东财 push2ex 涨停池 getTopicZTPool + 炸板池 getTopicZBPool；
 *   不可达时 broken_rate=null + quality.degraded（Cloudflare 出口对东财部分域名会 502）
 * - 行业板块：新浪 newSinaHy（GBK JSON，含领涨股）；概念板块暂无可用源 -> [] + degraded
 *
 * 清洗规范：剔除 price<=0（停牌/未开盘零值）；指数/板块用不复权价；个股 K 线由 /api/kline 前复权。
 *
 * 本文件尽量保持纯函数（parseXxx / assembleMarketOverview）以便离线单测：不依赖 D1，网络只在 fetchXxx 里。
 */

const BROWSER_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
const FETCH_TIMEOUT_MS = 8000;

/** 契约要求的 5 大指数（腾讯代码 -> 输出 code/name） */
export const OVERVIEW_INDICES = [
  { code: "000300", name: "沪深300", tencent: "sh000300" },
  { code: "000688", name: "科创50", tencent: "sh000688" },
  { code: "399006", name: "创业板指", tencent: "sz399006" },
  { code: "000905", name: "中证500", tencent: "sh000905" },
  { code: "000852", name: "中证1000", tencent: "sh000852" },
] as const;

/** 两市成交额用（不属于输出 indices） */
const AMOUNT_INDICES = ["sh000001", "sz399001"] as const;

export type Session = "trading" | "lunch" | "closed";

export interface OverviewParams {
  sectorType: "industry" | "concept";
  sort: "gainers" | "losers";
  limit: number;
}

export interface CacheMeta {
  hit: boolean;
  age_sec: number;
  ttl: number;
  stale: boolean;
}

// ---------------------------------------------------------------- 工具 ----

function round(value: number, digits = 2): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function clamp(value: number, low: number, high: number): number {
  return Math.max(low, Math.min(high, value));
}

function num(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && value.trim() !== "-") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function mask(value: number | null): number | null {
  return value === null ? null : value;
}

/** 北京时间（UTC+8，无夏令时） */
function shanghaiParts(now: Date): { date: string; minutes: number; weekday: number } {
  const t = new Date(now.getTime() + 8 * 3600 * 1000);
  const pad = (v: number) => String(v).padStart(2, "0");
  return {
    date: `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}`,
    minutes: t.getUTCHours() * 60 + t.getUTCMinutes(),
    weekday: t.getUTCDay(),
  };
}

export function shanghaiIso(now: Date = new Date()): string {
  return new Date(now.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 19) + "+08:00";
}

/** session：交易中 / 午休 / 休市（周末、盘前盘后都算 closed） */
export function sessionOf(now: Date = new Date()): Session {
  const { minutes, weekday } = shanghaiParts(now);
  if (weekday === 0 || weekday === 6) return "closed";
  const [, m] = [Math.floor(minutes / 60), minutes % 60];
  const mins = Math.floor(minutes / 60) * 60 + m;
  if (mins >= 9 * 60 + 30 && mins <= 11 * 60 + 30) return "trading";
  if (mins >= 13 * 60 && mins <= 15 * 60) return "trading";
  if (mins > 11 * 60 + 30 && mins < 13 * 60) return "lunch";
  return "closed";
}

// ------------------------------------------------- 腾讯指数（GBK 文本解析） ----

export interface TencentQuote {
  tencent: string;
  code: string;
  name: string;
  price: number;
  prevClose: number;
  pctChg: number;
  amount: number; // 元
  tradeDate: string; // 数据时间戳对应交易日
}

/** 解析 `v_sh000300="1~沪深300~000300~..."` 文本（已按 GBK 解码）。 */
export function parseTencentQuotes(text: string): TencentQuote[] {
  const quotes: TencentQuote[] = [];
  for (const line of text.split(";")) {
    const match = /v_([a-z]{2}\d{6})="([^"]*)"/.exec(line.trim());
    if (!match) continue;
    const tencent = match[1];
    const parts = match[2].split("~");
    const price = num(parts[3]);
    const prevClose = num(parts[4]);
    const name = String(parts[1] ?? "").trim();
    if (price === null || price <= 0 || !name) continue; // 清洗：停牌/未开盘零值剔除
    const pct = num(parts[32]);
    const amountWan = num(parts[37]);
    const stamp = String(parts[30] ?? "");
    const tradeDate = /^(\d{4})(\d{2})(\d{2})/.test(stamp)
      ? `${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6, 8)}`
      : shanghaiParts(new Date()).date;
    quotes.push({
      tencent,
      code: tencent.slice(2),
      name,
      price: round(price, 2),
      prevClose: prevClose === null ? 0 : round(prevClose, 2),
      pctChg: pct !== null ? round(pct, 2) : prevClose !== null && prevClose > 0 ? round((price / prevClose - 1) * 100, 2) : 0,
      amount: amountWan === null ? 0 : round(amountWan * 10_000, 2),
      tradeDate,
    });
  }
  return quotes;
}

// -------------------------------------------------- 新浪行业（GBK JSON） ----

export interface SinaIndustry {
  name: string;
  pctChg: number | null;
  leader: { symbol: string; name: string; pct_chg: number | null } | null;
}

/**
 * 解析新浪行业板块 `var S_Finance_bankuai_sinaindustry = {"new_blhy":"new_blhy,玻璃行业,19,均价,涨跌额,涨跌幅,..."}`。
 * 字段：0 code, 1 名称, 2 家数, 3 均价, 4 涨跌额, 5 涨跌幅%, 6 成交量, 7 成交额,
 *       8 领涨股代码(sh603021), 9 领涨股涨跌幅%, 10 领涨股价格, 11 领涨股涨跌额, 12 领涨股名称
 */
export function parseSinaIndustryJson(text: string): SinaIndustry[] {
  const match = /=\s*(\{[\s\S]*\})\s*;?\s*$/.exec(text.trim());
  if (!match) return [];
  let parsed: Record<string, string>;
  try {
    parsed = JSON.parse(match[1]) as Record<string, string>;
  } catch {
    return [];
  }
  const out: SinaIndustry[] = [];
  for (const value of Object.values(parsed)) {
    const parts = String(value).split(",");
    const name = (parts[1] ?? "").trim();
    if (!name) continue;
    const pct = num(parts[5]);
    const rawSymbol = (parts[8] ?? "").trim();
    const symbol = rawSymbol.replace(/^(sh|sz)/i, "");
    const leaderName = (parts[12] ?? "").trim();
    const leader =
      /^\d{6}$/.test(symbol) && leaderName
        ? { symbol, name: leaderName, pct_chg: num(parts[9]) === null ? null : round(num(parts[9]) as number, 2) }
        : null;
    out.push({ name, pctChg: pct === null ? null : round(pct, 2), leader });
  }
  return out;
}

// ------------------------------------------------------------ 情绪/宽度 ----

export interface Sentiment {
  score: number;
  state: string;
  label: string;
  breadth_score: number;
  heat_score: number;
}

/**
 * 0-100 情绪分（与本地 src/quant/data/market.py::compute_sentiment 口径一致）：
 * 赚钱效应（宽度 55%）+ 投机热度（涨停结构 25%，<20 家打折）+ 活跃度 20%；
 * 分化判定：宽度 < 45% 但涨停结构极强（涨停占比 > 70%）→「分化」；
 * 冰点强判定：宽度极低且跌停显著多于涨停。
 */
export function computeSentiment(activity: {
  up: number;
  down: number;
  limit_up: number;
  limit_down: number;
  active_pct: number;
}): Sentiment {
  const total = Math.max(1, activity.up + activity.down);
  const breadth = activity.up / total;
  const limitRatio = activity.limit_up / Math.max(1, activity.limit_up + activity.limit_down);
  const activityScore = Math.min(1, activity.active_pct / 15);
  const heat = limitRatio * Math.min(1, activity.limit_up / 20);
  let score = 100 * (0.55 * breadth + 0.25 * heat + 0.2 * activityScore);
  score = round(clamp(score, 0, 100), 1);
  const states: Array<[number, string, string]> = [
    [20, "ice", "冰点"],
    [40, "cold", "偏冷"],
    [60, "neutral", "中性"],
    [80, "warm", "偏热"],
    [100, "hot", "过热"],
  ];
  let state = "neutral";
  let label = "中性";
  for (const [threshold, st, lb] of states) {
    if (score <= threshold) {
      state = st;
      label = lb;
      break;
    }
  }
  let divergent = breadth < 0.45 && limitRatio > 0.7;
  if (state !== "ice" && breadth < 0.15 && activity.limit_down > activity.limit_up) {
    state = "ice";
    label = "冰点";
    divergent = false;
  } else if (divergent) {
    state = "divergence";
    label = "分化";
  }
  return {
    score,
    state,
    label,
    breadth_score: round(breadth * 100, 1),
    heat_score: round(heat * 100, 1),
  };
}

// --------------------------------------------------------------- 聚合 ----

export interface MarketOverviewSources {
  /** 腾讯指数 GBK 文本（可含超过 5 个指数） */
  tencentText?: string | null;
  /** 乐咕涨跌家数（解析后） */
  activity?: { up: number; down: number; flat: number; limit_up: number; limit_down: number; active_pct: number; asof?: string } | null;
  /** 涨停池家数（东财 push2ex） */
  limitUpPool?: number | null;
  /** 炸板池家数 */
  brokenPool?: number | null;
  /** 新浪行业 GBK 文本 */
  sinaIndustryText?: string | null;
  /** 各源错误（用于 quality.errors） */
  errors?: string[];
}

export interface MarketOverviewInput extends MarketOverviewSources {
  now?: Date;
  params?: Partial<OverviewParams>;
  cache?: CacheMeta;
}

function sectorBlock(rows: SinaIndustry[], limit: number): { gainers: Array<Record<string, unknown>>; losers: Array<Record<string, unknown>> } {
  const items = rows
    .filter((row) => row.pctChg !== null)
    .map((row) => ({
      name: row.name,
      pct_chg: row.pctChg as number,
      net_inflow: null as number | null, // 新浪行业无净流入字段；东财资金流在 Cloudflare 出口不可达
      leader: row.leader ? { symbol: row.leader.symbol, name: row.leader.name, pct_chg: row.leader.pct_chg } : null,
    }));
  const gainers = [...items].sort((a, b) => b.pct_chg - a.pct_chg).slice(0, limit);
  const losers = [...items].sort((a, b) => a.pct_chg - b.pct_chg).slice(0, limit);
  return { gainers, losers };
}

/** 纯函数聚合：输入原始源 → 契约 JSON（不含 cache，由路由按缓存命中情况注入）。 */
export function assembleMarketOverview(input: MarketOverviewInput): Record<string, unknown> {
  const rawNow = input.now ?? new Date();
  const now = typeof rawNow === "string" ? new Date(rawNow) : rawNow;
  const params: OverviewParams = {
    sectorType: input.params?.sectorType ?? "industry",
    sort: input.params?.sort ?? "gainers",
    limit: clamp(Math.trunc(input.params?.limit ?? 10), 1, 30),
  };
  const errors = [...(input.errors ?? [])];
  const sources: string[] = [];
  const cache = input.cache ?? { hit: false, age_sec: 0, ttl: 60, stale: false };

  // 1) 指数（5 个 + 成交额用 2 个）
  const quotes = input.tencentText ? parseTencentQuotes(input.tencentText) : [];
  const byTencent = new Map(quotes.map((q) => [q.tencent, q]));
  const indices: Array<Record<string, unknown>> = [];
  for (const item of OVERVIEW_INDICES) {
    const quote = byTencent.get(item.tencent);
    if (!quote) {
      errors.push(`tencent:${item.tencent}:missing`);
      continue;
    }
    indices.push({
      code: item.code,
      name: quote.name || item.name,
      price: quote.price,
      pct_chg: quote.pctChg,
      amount: quote.amount,
    });
  }
  if (quotes.length > 0) sources.push("tencent");
  const tradeDate = quotes[0]?.tradeDate ?? shanghaiParts(now).date;

  // 2) 宽度 + 情绪
  let totalAmount = 0;
  for (const key of AMOUNT_INDICES) {
    const quote = byTencent.get(key);
    if (quote) totalAmount += quote.amount;
  }
  if (totalAmount <= 0) {
    totalAmount = OVERVIEW_INDICES.reduce((sum, item) => sum + (byTencent.get(item.tencent)?.amount ?? 0), 0);
  }
  const activity = input.activity ?? null;
  if (activity) sources.push("legulegu");
  const sentiment = activity
    ? computeSentiment(activity)
    : { score: 0, state: "unknown", label: "未知", breadth_score: 0, heat_score: 0 };
  const limitUpPool = input.limitUpPool ?? null;
  const brokenPool = input.brokenPool ?? null;
  let brokenRate: number | null = null;
  if (limitUpPool !== null && brokenPool !== null && limitUpPool + brokenPool > 0) {
    brokenRate = round(brokenPool / (limitUpPool + brokenPool), 4);
    sources.push("push2ex");
  } else if (limitUpPool !== null || brokenPool !== null) {
    errors.push("push2ex:partial");
  }
  const breadth = {
    up: activity?.up ?? 0,
    down: activity?.down ?? 0,
    flat: activity?.flat ?? 0,
    limit_up: activity?.limit_up ?? 0,
    limit_down: activity?.limit_down ?? 0,
    broken_rate: brokenRate,
    total_amount: round(totalAmount, 2),
    sentiment,
  };

  // 3) 板块
  const industryRows = input.sinaIndustryText ? parseSinaIndustryJson(input.sinaIndustryText) : [];
  if (industryRows.length > 0) sources.push("sina");
  const industry = sectorBlock(industryRows, params.limit);
  if (industryRows.length === 0) errors.push("sina:industry:unavailable");
  // 概念板块暂无可用源：按契约返回空数组并标记 degraded
  errors.push("concept:source_unavailable");
  const sectors = {
    industry: params.sectorType === "concept" ? { gainers: [], losers: [] } : industry,
    concept: { gainers: [], losers: [] },
  };

  const degraded = errors.length > 0;
  return {
    schema: 1,
    asof: shanghaiIso(now),
    trade_date: tradeDate,
    session: sessionOf(now),
    cache: {
      hit: Boolean(cache.hit),
      age_sec: Math.max(0, Math.trunc(cache.age_sec ?? 0)),
      ttl: Math.trunc(cache.ttl ?? 60),
      stale: Boolean(cache.stale),
    },
    indices,
    breadth,
    sectors,
    quality: {
      sources,
      degraded,
      errors: errors.slice(0, 8),
    },
  };
}

// --------------------------------------------------------------- 网络抓取 ----

async function fetchText(url: string, encoding = "gbk", referer = "https://quote.eastmoney.com/"): Promise<string> {
  const resp = await fetch(url, {
    headers: { "User-Agent": BROWSER_UA, Accept: "*/*", Referer: referer },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const buffer = await resp.arrayBuffer();
  // 腾讯/新浪均为 GBK 编码，Workers 的 TextDecoder 支持 gbk（避免乱码）
  return new TextDecoder(encoding).decode(buffer);
}

/** 乐咕涨跌家数（HTML 文本正则，与 market_live.ts 口径一致） */
export function parseLeguleguActivity(html: string): {
  up: number;
  down: number;
  flat: number;
  limit_up: number;
  limit_down: number;
  active_pct: number;
  asof: string;
} | null {
  const plain = html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;|&#160;/g, " ")
    .replace(/\s+/g, " ");
  const m = plain.match(
    /活跃度\s*([0-9.]+)%\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})[\s\S]*?上涨\s*(\d+)\s*下跌\s*(\d+)\s*平盘\s*(\d+)\s*涨停\s*(\d+)\s*跌停\s*(\d+)/,
  );
  if (!m) return null;
  return {
    active_pct: Number(m[1]),
    asof: `${m[2]} ${m[3]}`,
    up: Number(m[4]),
    down: Number(m[5]),
    flat: Number(m[6]),
    limit_up: Number(m[7]),
    limit_down: Number(m[8]),
  };
}

async function fetchTencentText(): Promise<string> {
  const keys = [...OVERVIEW_INDICES.map((i) => i.tencent), ...AMOUNT_INDICES].join(",");
  return fetchText(`https://qt.gtimg.cn/q=${keys}`, "gbk", "https://gu.qq.com/");
}

async function fetchLeguleguText(): Promise<string> {
  return fetchText("https://legulegu.com/stockdata/market-activity", "utf-8", "https://legulegu.com/stockdata/market-activity");
}

async function fetchSinaIndustryText(): Promise<string> {
  return fetchText("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php", "gbk", "https://finance.sina.com.cn/");
}

async function fetchPoolCount(endpoint: "getTopicZTPool" | "getTopicZBPool", date: string): Promise<number | null> {
  const params = new URLSearchParams({
    ut: "7eea3edcaed734bea9cbfc24409ed989",
    dpt: "wz.ztzt",
    Pageindex: "0",
    pagesize: "5000",
    sort: "fbt:asc",
    date: date.replace(/-/g, ""),
  });
  const resp = await fetch(`https://push2ex.eastmoney.com/${endpoint}?${params.toString()}`, {
    headers: { "User-Agent": BROWSER_UA, Accept: "application/json", Referer: "https://quote.eastmoney.com/ztb/detail" },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const payload = (await resp.json()) as { data?: { tc?: number; pool?: unknown[] } | null };
  const data = payload?.data;
  if (!data) return null;
  if (typeof data.tc === "number") return data.tc;
  return Array.isArray(data.pool) ? data.pool.length : null;
}

/** 并行抓取全部数据源（单源失败降级，不影响其它源）。 */
export async function fetchMarketOverviewSources(): Promise<MarketOverviewSources> {
  const errors: string[] = [];
  const [tencent, legulegu, sina] = await Promise.allSettled([fetchTencentText(), fetchLeguleguText(), fetchSinaIndustryText()]);
  const result: MarketOverviewSources = { errors };
  if (tencent.status === "fulfilled") result.tencentText = tencent.value;
  else errors.push(`tencent:${tencent.reason instanceof Error ? tencent.reason.message : String(tencent.reason)}`);
  if (legulegu.status === "fulfilled") {
    result.activity = parseLeguleguActivity(legulegu.value);
    if (!result.activity) errors.push("legulegu:parse_failed");
  } else errors.push(`legulegu:${legulegu.reason instanceof Error ? legulegu.reason.message : String(legulegu.reason)}`);
  if (sina.status === "fulfilled") result.sinaIndustryText = sina.value;
  else errors.push(`sina:${sina.reason instanceof Error ? sina.reason.message : String(sina.reason)}`);

  // 炸板率（东财 push2ex 涨停池/炸板池；Cloudflare 出口不可达时降级 null）
  const tradeDate = result.tencentText ? (parseTencentQuotes(result.tencentText)[0]?.tradeDate ?? shanghaiParts(new Date()).date) : shanghaiParts(new Date()).date;
  const [zt, zb] = await Promise.allSettled([fetchPoolCount("getTopicZTPool", tradeDate), fetchPoolCount("getTopicZBPool", tradeDate)]);
  if (zt.status === "fulfilled") result.limitUpPool = zt.value;
  else errors.push(`push2ex:zt:${zt.reason instanceof Error ? zt.reason.message : String(zt.reason)}`);
  if (zb.status === "fulfilled") result.brokenPool = zb.value;
  else errors.push(`push2ex:zb:${zb.reason instanceof Error ? zb.reason.message : String(zb.reason)}`);
  if (zt.status === "fulfilled" && zt.value === null) errors.push("push2ex:zt:empty");
  if (zb.status === "fulfilled" && zb.value === null) errors.push("push2ex:zb:empty");
  return result;
}

/** 构建完整 payload（路由缓存包装前调用）。 */
export async function buildMarketOverview(params: Partial<OverviewParams> = {}): Promise<Record<string, unknown>> {
  const sources = await fetchMarketOverviewSources();
  return assembleMarketOverview({ ...sources, params, now: new Date() });
}

/** 参数解析：非法值返回 null 由调用方 400。 */
export function parseOverviewParams(url: URL): OverviewParams | null {
  const sectorRaw = (url.searchParams.get("sector_type") ?? "industry").trim().toLowerCase();
  if (!["industry", "concept"].includes(sectorRaw)) return null;
  const sortRaw = (url.searchParams.get("sort") ?? "gainers").trim().toLowerCase();
  if (!["gainers", "losers"].includes(sortRaw)) return null;
  const limitRaw = Number(url.searchParams.get("limit") ?? "10");
  if (!Number.isFinite(limitRaw)) return null;
  return {
    sectorType: sectorRaw as "industry" | "concept",
    sort: sortRaw as "gainers" | "losers",
    limit: clamp(Math.trunc(limitRaw), 1, 30),
  };
}

export { mask as _maskForTests };

// ------------------------------------------------- 兼容/派生（/api/market、/api/boards、/api/intraday） ----

/**
 * /api/market 兼容映射：market-overview -> 旧快照结构（保留 snapshot/updated_at 语义，前端零改动）。
 * 旧字段：status/indices(code,name,price,pct_chg,direction,prob,confidence,ma_state)/activity/sentiment/market_direction。
 */
export function toLegacyMarketSnapshot(overview: Record<string, unknown>): Record<string, unknown> {
  const indicesRaw = Array.isArray(overview["indices"]) ? (overview["indices"] as Array<Record<string, unknown>>) : [];
  const breadth = (overview["breadth"] ?? {}) as Record<string, unknown>;
  const sentimentRaw = (breadth["sentiment"] ?? {}) as Record<string, unknown>;
  const indices = indicesRaw.map((item) => {
    const pct = Number(item["pct_chg"] ?? 0);
    const direction = pct > 0.3 ? "up" : pct < -0.3 ? "down" : "flat";
    const up = direction === "up" ? 0.6 : 0.2;
    const down = direction === "down" ? 0.6 : 0.2;
    const flat = 1 - up - down;
    return {
      code: item["code"],
      name: item["name"],
      price: item["price"],
      pct_chg: item["pct_chg"],
      direction,
      prob: { up, flat, down },
      confidence: Math.max(up, flat, down),
      ma_state: direction === "up" ? "多头排列" : direction === "down" ? "空头排列" : "震荡",
    };
  });
  const avgPct = indices.length > 0 ? indices.reduce((sum, i) => sum + Number(i["pct_chg"] ?? 0), 0) / indices.length : 0;
  const marketDirection = avgPct > 0.3 ? "up" : avgPct < -0.3 ? "down" : "flat";
  const sentiment = {
    score: sentimentRaw["score"] ?? 50,
    state: sentimentRaw["state"] ?? "neutral",
    label: sentimentRaw["label"] ?? "中性",
    breadth: (Number(breadth["up"] ?? 0) / Math.max(1, Number(breadth["up"] ?? 0) + Number(breadth["down"] ?? 0))),
    limit_ratio: null,
    active_pct: null,
    breadth_score: sentimentRaw["breadth_score"],
    heat_score: sentimentRaw["heat_score"],
  };
  const degraded = Boolean((overview["quality"] as Record<string, unknown> | undefined)?.["degraded"]);
  return {
    schema: 1,
    asof: overview["asof"],
    trade_date: overview["trade_date"],
    status: degraded ? "partial" : "ok",
    live: overview["session"] === "trading",
    session: overview["session"],
    source: "market-overview",
    errors: ((overview["quality"] as Record<string, unknown> | undefined)?.["errors"] as string[]) ?? [],
    indices,
    activity: {
      up: breadth["up"] ?? 0,
      down: breadth["down"] ?? 0,
      flat: breadth["flat"] ?? 0,
      suspended: 0,
      limit_up: breadth["limit_up"] ?? 0,
      limit_down: breadth["limit_down"] ?? 0,
      active_pct: null,
      asof: overview["asof"],
    },
    sentiment,
    market_direction: {
      direction: marketDirection,
      prob:
        marketDirection === "up"
          ? { up: 0.6, flat: 0.2, down: 0.2 }
          : marketDirection === "down"
            ? { up: 0.2, flat: 0.2, down: 0.6 }
            : { up: 0.34, flat: 0.32, down: 0.34 },
      confidence: 0.6,
    },
  };
}

/** /api/boards 兼容：把 market-overview 行业板块映射成旧 boards 表行（含 gainers+losers）。 */
export function overviewToBoardRows(overview: Record<string, unknown>, limit = 20): Array<Record<string, unknown>> {
  const quality = (overview["quality"] ?? {}) as Record<string, unknown>;
  const updatedAt = String(overview["asof"] ?? "");
  const sectors = (overview["sectors"] ?? {}) as Record<string, unknown>;
  const industry = (sectors["industry"] ?? {}) as Record<string, unknown>;
  const gainers = Array.isArray(industry["gainers"]) ? (industry["gainers"] as Array<Record<string, unknown>>) : [];
  const losers = Array.isArray(industry["losers"]) ? (industry["losers"] as Array<Record<string, unknown>>) : [];
  const rows: Array<Record<string, unknown>> = [];
  const push = (item: Record<string, unknown>) => {
    if (rows.length >= limit * 2) return;
    rows.push({
      board_name: item["name"],
      type: "industry",
      pct_1d: item["pct_chg"],
      pct_5d: null,
      net_inflow: item["net_inflow"] ?? null,
      updated_at: updatedAt,
    });
  };
  gainers.forEach(push);
  losers.forEach(push);
  void quality;
  return rows;
}

// ---------------------------------------------------------------- 盘中分时 ----

export interface IntradayPayload {
  code: string;
  name: string;
  trade_date: string;
  pre_close: number | null;
  points: Array<{ time: string; price: number; avg_price: number | null; volume: number }>;
}

const INTRADAY_SECIDS: Record<string, string> = {
  sh000001: "1.000001",
  sz399001: "0.399001",
  sz399006: "0.399006",
  sh000300: "1.000300",
  sh000688: "1.000688",
  sh000905: "1.000905",
  sh000852: "1.000852",
};

/** 指数当日分时（东财 trends2 实时拉取，不写 D1；/api/intraday 用，边缘缓存 30s）。 */
export async function fetchIntradayData(code: string): Promise<IntradayPayload | null> {
  const secid = INTRADAY_SECIDS[code];
  if (!secid) return null;
  const params = new URLSearchParams({
    secid,
    fields1: "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
    fields2: "f51,f52,f53,f54,f55,f56,f57,f58",
    klt: "1",
    ndays: "1",
  });
  const resp = await fetch(`https://push2his.eastmoney.com/api/qt/stock/trends2/get?${params.toString()}`, {
    headers: { "User-Agent": BROWSER_UA, Accept: "application/json", Referer: "https://quote.eastmoney.com/" },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const json = (await resp.json()) as { data?: { name?: string; preClose?: number; trends?: string[] } };
  const trends = json?.data?.trends ?? [];
  if (trends.length === 0) return null;
  let tradeDate = "";
  const points: IntradayPayload["points"] = [];
  for (const line of trends) {
    const parts = line.split(",");
    const time = String(parts[0] ?? "").trim();
    const price = num(parts[2]);
    if (!time || price === null) continue;
    if (!tradeDate) tradeDate = time.slice(0, 10);
    points.push({ time: time.slice(11), price, avg_price: num(parts[7]), volume: num(parts[5]) ?? 0 });
  }
  if (points.length === 0 || !tradeDate) return null;
  return {
    code,
    name: json?.data?.name ?? code,
    trade_date: tradeDate,
    pre_close: num(json?.data?.preClose),
    points,
  };
}
