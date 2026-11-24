/**
 * 云端数据自更新（task-11 + Lead 追加需求）
 *
 * 三个 Cron 动作（Cloudflare 触发，不依赖 GitHub Actions / 本地设备）：
 *   1) 盘中 `* /5 1-7 * * 1-5`（UTC，已有）：refreshMarketLive（指数/情绪）+ refreshIntradayIndex（4 大指数分时 trends2）
 *   2) 午评 `35 3 * * 1-5`（UTC = 北京 11:35）：刷新盘中数据 + buildMarketSummary("midday")
 *   3) 收评 `5 7 * * 1-5`（UTC = 北京 15:05）：刷新收盘数据 + buildMarketSummary("close")
 *   4) 收盘同步 `40 7 * * 1-5`（UTC = 北京 15:40）：syncDailyClose：
 *        a. 4 大指数日线 -> 市场快照 market_snapshots
 *        b. 股票池（D1 stock_names 40 只，缺失用内置 40 只池）近 120 日 K 线 -> klines（串行 + 250ms 节流）
 *        c. 东财 7x24 快讯 -> 关键词利好利空评分 -> events（财联社签名接口不可用时降级，见 warnings）
 *        d. 东财板块 clist（行业/概念）-> boards；Worker 不可达时降级新浪行业（GBK，TextDecoder 解码）；再失败跳过
 *        e. 简化 regime（复刻 src/quant/data/regime.py 核心规则）-> regime_snapshots
 *   5) 临时验证 Cron `* * * * *`（部署验证时临时加入，验证完从 wrangler.local.toml 移除）= runManualDailySync("all")
 *
 * 设计原则：每一步独立 try/catch，失败只记 console.warn + errors，不抛出未处理异常；
 * 数据源全部带超时；串行请求避免东财限流。
 */
import { refreshMarketLive, fetchActivity, computeSentiment, indexDirection, nowIsoShanghai, isTradingSession, type Activity } from "./market_live.ts";
import {
  insertJsonSnapshot,
  insertMarketSnapshot,
  queryBoards,
  queryEvents,
  queryLatestMarket,
  queryStockSymbols,
  upsertBoards,
  upsertEvents,
  upsertIntradayIndex,
  upsertKlines,
  upsertMarketSummary,
  type BoardRow,
  type KlineRow,
} from "./db.ts";
import type { Env } from "./types.ts";

// ---------------------------------------------------------------- 常量 ----

const BROWSER_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
const FETCH_TIMEOUT_MS = 15_000;
const THROTTLE_MS = 250;
const STOCK_POOL_SIZE = 40;
const KLINE_DAYS = 120;

export const INDEXES = [
  { code: "sh000001", name: "上证指数", secid: "1.000001" },
  { code: "sz399001", name: "深证成指", secid: "0.399001" },
  { code: "sz399006", name: "创业板指", secid: "0.399006" },
  { code: "sh000300", name: "沪深300", secid: "1.000300" },
] as const;

/** 腾讯行情兜底：东财被限流时用 qt.gtimg.cn 拿指数最新价/昨收（GBK 解码）。 */
async function fetchTencentIndexQuote(index: (typeof INDEXES)[number]): Promise<IndexDaily> {
  const text = await fetchTextWithEncoding(`https://qt.gtimg.cn/q=${index.code}`, "gbk");
  const match = text.match(/="([^"]+)"/);
  if (!match) throw new Error(`${index.code} 腾讯行情解析失败`);
  const parts = match[1].split("~");
  const price = num(parts[3]);
  const prevClose = num(parts[4]);
  if (price === null || prevClose === null || prevClose === 0) throw new Error(`${index.code} 腾讯行情字段缺失`);
  const tradeDate = shanghaiDate();
  const amount = num(parts[37]);
  return {
    code: index.code,
    name: index.name,
    dates: [tradeDate, tradeDate],
    closes: [prevClose, price],
    amounts: [0, amount ?? 0],
    last: { date: tradeDate, close: round(price, 2), pctChg: round((price / prevClose - 1) * 100, 2) },
  };
}

/** stock_names 为空时的兜底股票池（40 只常见 A 股，保证 K 线覆盖） */
const FALLBACK_STOCK_POOL = [
  "600519", "000001", "300750", "600036", "601318", "600030", "000858", "002594", "601899", "600900",
  "601166", "000333", "000651", "002415", "600276", "600887", "601012", "688981", "002230", "300059",
  "601888", "600309", "000725", "002475", "300760", "688111", "601088", "600028", "601857", "600019",
  "600050", "601668", "000002", "001979", "600048", "601398", "601288", "601939", "600000", "601628",
];

/** 关键词利好/利空（与本地 src/quant/data/events.py 保持一致；score = clamp(sum/3, -1, 1)） */
const BULLISH: ReadonlyArray<readonly [string, number]> = [
  ["回购", 2], ["增持", 2], ["中标", 2], ["业绩预增", 2], ["净利润增长", 2],
  ["分红", 2], ["重大合同", 2], ["获批", 2], ["批准", 1], ["政策支持", 2],
  ["降准", 2], ["降息", 2], ["补贴", 1], ["注入", 1], ["扭亏", 2],
  ["超预期", 1], ["上调", 1], ["合作", 1], ["签约", 1], ["突破", 1],
  ["增长", 1], ["盈利", 1], ["恢复", 1], ["利好", 1],
];
const BEARISH: ReadonlyArray<readonly [string, number]> = [
  ["减持", -2], ["亏损", -2], ["预减", -2], ["立案", -2], ["调查", -1],
  ["处罚", -2], ["罚款", -2], ["退市", -2], ["违规", -2], ["违约", -2],
  ["下调", -1], ["商誉减值", -2], ["质押", -1], ["解禁", -1], ["诉讼", -1],
  ["停产", -2], ["风险警示", -2], ["ST", -1], ["下降", -1], ["下滑", -1],
  ["中止", -1], ["终止", -1], ["利空", -1], ["延期", -1],
];
const DISCLAIMER = "研究用途，不构成投资建议";

/** 大盘状态 → 中文标签 / 仓位建议（复刻 regime.py STATE_LABELS / STATE_POSITION） */
const STATE_LABELS: Record<string, string> = {
  offense: "进攻",
  lean_bull: "偏多",
  range: "震荡",
  lean_bear: "偏空",
  ice_watch: "冰点布局",
};
const STATE_POSITION: Record<string, { range: string; label: string; base: string; triggers: string[] }> = {
  offense: {
    range: "6-8成",
    label: "高仓位",
    base: "趋势与情绪共振向上，可按趋势行情参与，回撤容忍度适度提高",
    triggers: ["加仓条件：沪深300 站稳 20 日线且情绪分维持在 60 以上", "减仓条件：跌破 20 日线或单日跌停家数超过 30 家"],
  },
  lean_bull: {
    range: "4-6成",
    label: "中高仓位",
    base: "偏多结构下跟随主线板块，避免追高，保留机动仓位",
    triggers: ["加仓条件：主线板块 5 日涨幅与净流入同时位居前列", "减仓条件：状态分回落至 50 以下或情绪分跌破 40"],
  },
  range: {
    range: "2-4成",
    label: "中性仓位",
    base: "震荡市以低吸高抛为主，快进快出，不做趋势性加仓",
    triggers: ["加仓条件：指数放量站上 20 日线且情绪分回升至 55 以上", "减仓条件：反弹缩量或跌停家数明显多于涨停家数"],
  },
  lean_bear: {
    range: "0-2成",
    label: "低仓位",
    base: "均线空头排列且情绪偏冷，防守为主，等待企稳信号再提升仓位",
    triggers: ["加仓条件：涨跌宽度连续 3 日回升且指数止跌", "风控条件：指数再创新低或跌停家数超过 100 家时保持观望"],
  },
  ice_watch: {
    range: "0-2成",
    label: "试错仓位",
    base: "冰点区左侧布局窗口，只用小仓位试错修复，跌停潮未收敛前不加仓",
    triggers: ["试错条件：情绪分<20 且涨跌宽度<15% 时可小仓位试冰点修复（≤2成）", "加仓条件：宽度回升至 30% 以上且涨停家数多于跌停家数", "风控条件：指数再创新低或跌停家数超过 100 家时保持观望"],
  },
};
const STATE_ACTION: Record<string, string> = {
  offense: "趋势与情绪共振，可积极参与",
  lean_bull: "偏多结构，跟随主线、控制追高",
  range: "震荡格局，低吸为主、不宜趋势加仓",
  lean_bear: "趋势与情绪偏弱，防守为主",
  ice_watch: "冰点区左侧观察，等宽度回升再小仓位试错",
};

// ---------------------------------------------------------------- 工具 ----

export interface SyncStep {
  name: string;
  ok: boolean;
  ms: number;
  error?: string;
  detail?: Record<string, unknown>;
}

interface StepOutcome<T> {
  value: T | null;
  step: SyncStep;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** 单步执行：捕获所有异常，记录耗时与结果；失败返回 null 让后续步骤决定是否降级。 */
async function runStep<T>(
  steps: SyncStep[],
  name: string,
  fn: () => Promise<T>,
  detail?: (value: T) => Record<string, unknown>,
): Promise<StepOutcome<T>> {
  const startedAt = Date.now();
  try {
    const value = await fn();
    const step: SyncStep = { name, ok: true, ms: Date.now() - startedAt };
    if (detail) step.detail = detail(value);
    steps.push(step);
    return { value, step };
  } catch (err) {
    const step: SyncStep = { name, ok: false, ms: Date.now() - startedAt, error: errorMessage(err) };
    steps.push(step);
    console.warn(`[daily_sync] step ${name} 失败：${step.error}`);
    return { value: null, step };
  }
}

async function fetchJson<T = unknown>(url: string, headers: Record<string, string> = {}, timeoutMs = FETCH_TIMEOUT_MS): Promise<T> {
  const resp = await fetch(url, {
    headers: { "User-Agent": BROWSER_UA, Accept: "application/json, text/plain, */*", ...headers },
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status} ${url.slice(0, 120)}`);
  return (await resp.json()) as T;
}

async function fetchTextWithEncoding(url: string, encoding = "gbk"): Promise<string> {
  const resp = await fetch(url, {
    headers: { "User-Agent": BROWSER_UA, Accept: "text/html,application/xhtml+xml,*/*" },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status} ${url.slice(0, 120)}`);
  const buffer = await resp.arrayBuffer();
  // 东财 push2his 为 JSON(UTF-8)；新浪行业/同花顺板块页为 GBK，用 TextDecoder 解码
  return new TextDecoder(encoding).decode(buffer);
}

function num(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && value.trim() !== "-") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function round(value: number, digits = 2): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function clamp(value: number, low: number, high: number): number {
  return Math.max(low, Math.min(high, value));
}

function mean(values: number[]): number {
  if (values.length === 0) return 0;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

function shanghaiDate(iso?: string): string {
  return (iso ?? nowIsoShanghai()).slice(0, 10);
}

function nowIso(): string {
  return nowIsoShanghai();
}

function djb2(text: string): string {
  let hash = 5381;
  for (let i = 0; i < text.length; i += 1) hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
  return hash.toString(36);
}

function ma(closes: number[], window: number): number | null {
  if (closes.length < window) return null;
  return mean(closes.slice(-window));
}

/** 百分位排序（越大越好）：第 1 → 100，最后 → 0；缺失返回 null，并列取平均名次。 */
function percentileScores(values: Array<number | null>): Array<number | null> {
  const valid = values
    .map((value, index) => ({ value, index }))
    .filter((item): item is { value: number; index: number } => item.value !== null);
  if (valid.length === 0) return values.map(() => null);
  const sorted = [...valid].sort((a, b) => a.value - b.value);
  const rank = new Map<number, number>();
  sorted.forEach((item, rankIndex) => rank.set(item.index, rankIndex));
  const out: Array<number | null> = values.map(() => null);
  for (const item of valid) {
    const r = rank.get(item.index) ?? 0;
    out[item.index] = valid.length > 1 ? round((r / (valid.length - 1)) * 100, 1) : 100;
  }
  return out;
}

function secidOf(symbol: string): string {
  if (symbol.startsWith("6") || symbol.startsWith("9")) return `1.${symbol}`;
  return `0.${symbol}`;
}

// ------------------------------------------------------- 数据源：指数日线 ----

interface IndexDaily {
  code: string;
  name: string;
  dates: string[];
  closes: number[];
  amounts: number[];
  last: { date: string; close: number; pctChg: number };
}

async function fetchIndexDaily(index: (typeof INDEXES)[number], limit = KLINE_DAYS): Promise<IndexDaily> {
  const params = new URLSearchParams({
    secid: index.secid,
    fields1: "f1,f2,f3,f4,f5,f6",
    fields2: "f51,f52,f53,f54,f55,f56,f57",
    klt: "101",
    fqt: "1",
    lmt: String(limit),
    end: "20500101",
  });
  const json = await fetchJson<{ data?: { klines?: string[]; preKPrice?: number } }>(
    `https://push2his.eastmoney.com/api/qt/stock/kline/get?${params.toString()}`,
    { Referer: "https://quote.eastmoney.com/" },
  );
  const lines = json?.data?.klines ?? [];
  if (lines.length < 2) throw new Error(`${index.code} 日线不足`);
  const dates: string[] = [];
  const closes: number[] = [];
  const amounts: number[] = [];
  for (const line of lines) {
    const parts = line.split(",");
    const date = parts[0];
    const close = num(parts[2]);
    const amount = num(parts[6]);
    if (!date || close === null) continue;
    dates.push(date);
    closes.push(close);
    amounts.push(amount === null ? 0 : amount);
  }
  if (closes.length < 2) throw new Error(`${index.code} 日线解析失败`);
  const lastIdx = closes.length - 1;
  return {
    code: index.code,
    name: index.name,
    dates,
    closes,
    amounts,
    last: {
      date: dates[lastIdx],
      close: round(closes[lastIdx], 2),
      pctChg: round((closes[lastIdx] / closes[lastIdx - 1] - 1) * 100, 2),
    },
  };
}

// ------------------------------------------------------- 数据源：个股 K 线 ----

async function fetchStockKlines(symbol: string, limit = KLINE_DAYS): Promise<KlineRow[]> {
  const params = new URLSearchParams({
    secid: secidOf(symbol),
    fields1: "f1,f2,f3,f4,f5,f6",
    fields2: "f51,f52,f53,f54,f55,f56,f57",
    klt: "101",
    fqt: "0", // 不复权，与本地 daily_bars 的 close 口径一致
    lmt: String(limit),
    end: "20500101",
  });
  const json = await fetchJson<{ data?: { klines?: string[] } }>(
    `https://push2his.eastmoney.com/api/qt/stock/kline/get?${params.toString()}`,
    { Referer: "https://quote.eastmoney.com/" },
  );
  const lines = json?.data?.klines ?? [];
  const rows: KlineRow[] = [];
  for (const line of lines) {
    const p = line.split(",");
    const tradeDate = p[0];
    const close = num(p[2]);
    if (!tradeDate || !/^\d{4}-\d{2}-\d{2}$/.test(tradeDate) || close === null) continue;
    rows.push({
      symbol,
      trade_date: tradeDate,
      open: num(p[1]),
      high: num(p[3]),
      low: num(p[4]),
      close,
      volume: num(p[5]),
      amount: num(p[6]),
    });
  }
  return rows;
}

// ------------------------------------------------------- 数据源：板块行情 ----

interface BoardFetchResult {
  rows: BoardRow[];
  source: string;
  errors: string[];
}

const BOARD_UT = "bd1d9ddb04089700cf9c27f6f7426281";

async function fetchEastmoneyBoards(type: "industry" | "concept"): Promise<BoardRow[]> {
  const params = new URLSearchParams({
    pn: "1",
    pz: "100",
    po: "1",
    np: "1",
    ut: BOARD_UT,
    fltt: "2",
    invt: "2",
    fid: "f3",
    fs: type === "industry" ? "m:90 t:2" : "m:90 t:3",
    fields: "f3,f12,f14,f62,f109,f164",
  });
  const json = await fetchJson<{ data?: { diff?: Array<Record<string, unknown>> } }>(
    `https://push2.eastmoney.com/api/qt/clist/get?${params.toString()}`,
    { Referer: "https://quote.eastmoney.com/center/boardlist.html" },
  );
  const diff = json?.data?.diff ?? [];
  if (!Array.isArray(diff) || diff.length === 0) throw new Error(`东财 ${type} 板块为空`);
  const updatedAt = nowIso();
  const rows: BoardRow[] = [];
  for (const item of diff) {
    const name = typeof item["f14"] === "string" ? (item["f14"] as string).trim() : "";
    if (!name) continue;
    const netInflowRaw = num(item["f62"]);
    rows.push({
      board_name: name,
      type,
      pct_1d: num(item["f3"]),
      pct_5d: num(item["f109"]),
      net_inflow: netInflowRaw === null ? null : round(netInflowRaw / 1e8, 3), // 元 → 亿元
      updated_at: updatedAt,
    });
  }
  if (rows.length === 0) throw new Error(`东财 ${type} 板块解析为空`);
  return rows;
}

/** 降级：新浪行业板块（GBK 页面，用 TextDecoder("gbk") 解码），只提供当日涨幅。 */
async function fetchSinaBoards(): Promise<BoardRow[]> {
  const text = await fetchTextWithEncoding("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php", "gbk");
  const match = text.match(/=\s*(\{[\s\S]*\})\s*;?\s*$/);
  if (!match) throw new Error("新浪行业页面结构不认识");
  const parsed = JSON.parse(match[1]) as Record<string, string>;
  const updatedAt = nowIso();
  const rows: BoardRow[] = [];
  for (const value of Object.values(parsed)) {
    const parts = String(value).split(",");
    const name = (parts[1] ?? "").trim();
    if (!name) continue;
    rows.push({
      board_name: name,
      type: "industry",
      pct_1d: num(parts[5]),
      pct_5d: null,
      net_inflow: null,
      updated_at: updatedAt,
    });
  }
  if (rows.length === 0) throw new Error("新浪行业解析为空");
  return rows;
}

async function collectBoards(): Promise<BoardFetchResult> {
  const rows: BoardRow[] = [];
  const errors: string[] = [];
  let source = "eastmoney:clist";
  for (const type of ["industry", "concept"] as const) {
    try {
      rows.push(...(await fetchEastmoneyBoards(type)));
    } catch (err) {
      errors.push(`eastmoney:${type}:${errorMessage(err)}`);
    }
    await sleep(THROTTLE_MS);
  }
  if (rows.length === 0) {
    try {
      rows.push(...(await fetchSinaBoards()));
      source = "sina:newSinaHy(gbk)";
    } catch (err) {
      errors.push(`sina:${errorMessage(err)}`);
    }
  }
  return { rows, source, errors };
}

// ------------------------------------------------------- 数据源：快讯事件 ----

interface EventItem {
  id: string;
  source: string;
  title: string;
  summary: string;
  url: string;
  published_at: string;
  sentiment: string;
  label: string;
  score: number;
  hits: Array<{ word: string; weight: number }>;
  symbols: string[];
}

function scoreEventText(text: string): { sentiment: string; label: string; score: number; hits: Array<{ word: string; weight: number }> } {
  let total = 0;
  const hits: Array<{ word: string; weight: number }> = [];
  for (const [word, weight] of [...BULLISH, ...BEARISH]) {
    if (text.includes(word)) {
      total += weight;
      hits.push({ word, weight });
    }
  }
  const score = total === 0 ? 0 : round(clamp(total / 3, -1, 1), 3);
  if (score >= 0.2) return { sentiment: "bullish", label: "利好", score, hits: hits.slice(0, 5) };
  if (score <= -0.2) return { sentiment: "bearish", label: "利空", score, hits: hits.slice(0, 5) };
  return { sentiment: "neutral", label: "中性", score, hits: hits.slice(0, 5) };
}

function extractSymbols(text: string): string[] {
  const out: string[] = [];
  for (const match of text.matchAll(/(?<!\d)(\d{6})(?!\d)/g)) {
    const code = match[1];
    if (!out.includes(code) && ["0", "3", "6"].includes(code[0])) out.push(code);
    if (out.length >= 3) break;
  }
  return out;
}

function normalizePublishedAt(raw: unknown): string {
  const text = typeof raw === "string" ? raw.trim() : "";
  const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::(\d{2}))?/.exec(text);
  if (m) return `${m[1]}T${m[2]}:${m[3] ?? "00"}+08:00`;
  return nowIso();
}

async function fetchEastmoneyNews(limit = 120): Promise<EventItem[]> {
  const params = new URLSearchParams({
    client: "web",
    biz: "web_724",
    fastColumn: "102",
    sortEnd: "",
    pageSize: String(Math.max(limit, 60)),
    req_trace: String(Date.now()),
  });
  const json = await fetchJson<{ data?: { fastNewsList?: Array<Record<string, unknown>> } }>(
    `https://np-weblist.eastmoney.com/comm/web/getFastNewsList?${params.toString()}`,
    { Referer: "https://kuaixun.eastmoney.com/7_24.html" },
  );
  const list = json?.data?.fastNewsList ?? [];
  if (!Array.isArray(list) || list.length === 0) throw new Error("东财快讯为空");
  const items: EventItem[] = [];
  for (const row of list) {
    const title = typeof row["title"] === "string" ? row["title"].trim() : "";
    if (!title) continue;
    const summary = typeof row["summary"] === "string" ? row["summary"].trim() : "";
    const scored = scoreEventText(`${title} ${summary}`);
    const code = typeof row["code"] === "string" ? row["code"] : "";
    items.push({
      id: `em-${djb2(title)}`,
      source: "东方财富",
      title,
      summary: summary.slice(0, 180),
      url: code ? `https://finance.eastmoney.com/a/${code}.html` : "",
      published_at: normalizePublishedAt(row["showTime"]),
      ...scored,
      symbols: extractSymbols(`${title} ${summary}`),
    });
    if (items.length >= limit) break;
  }
  return items;
}

// --------------------------------------------------- 数据源：盘中分时 ----

interface IntradayPoint {
  time: string;
  price: number;
  avg_price: number | null;
  volume: number;
}

interface IntradayData {
  code: string;
  name: string;
  trade_date: string;
  pre_close: number | null;
  points: IntradayPoint[];
}

async function fetchIndexIntraday(index: (typeof INDEXES)[number]): Promise<IntradayData> {
  const params = new URLSearchParams({
    secid: index.secid,
    fields1: "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
    fields2: "f51,f52,f53,f54,f55,f56,f57,f58",
    klt: "1",
    ndays: "1",
  });
  const json = await fetchJson<{ data?: { name?: string; preClose?: number; trends?: string[] } }>(
    `https://push2his.eastmoney.com/api/qt/stock/trends2/get?${params.toString()}`,
    { Referer: "https://quote.eastmoney.com/" },
  );
  const trends = json?.data?.trends ?? [];
  if (trends.length === 0) throw new Error(`${index.code} 分时为空`);
  const points: IntradayPoint[] = [];
  let tradeDate = "";
  for (const line of trends) {
    const p = line.split(",");
    const time = (p[0] ?? "").trim();
    const price = num(p[2]);
    if (!time || price === null) continue;
    if (!tradeDate) tradeDate = time.slice(0, 10);
    points.push({
      time: time.slice(11), // HH:MM
      price,
      avg_price: num(p[7]),
      volume: num(p[5]) ?? 0,
    });
  }
  if (points.length === 0 || !tradeDate) throw new Error(`${index.code} 分时解析为空`);
  return {
    code: index.code,
    name: json?.data?.name ?? index.name,
    trade_date: tradeDate,
    pre_close: num(json?.data?.preClose),
    points,
  };
}

// ------------------------------------------------------------ regime ----

interface RegimeContext {
  index: IndexDaily;
  emotion: Record<string, unknown>;
  boards: BoardRow[];
}

function computeTrend(index: IndexDaily): Record<string, unknown> {
  const closes = index.closes;
  const ma5 = ma(closes, 5);
  const ma20 = ma(closes, 20);
  const ma60 = ma(closes, 60);
  const close = closes[closes.length - 1];
  const ret5 = closes.length > 5 ? round((closes[closes.length - 1] / closes[closes.length - 6] - 1) * 100, 2) : null;
  const ret20 = closes.length > 20 ? round((closes[closes.length - 1] / closes[closes.length - 21] - 1) * 100, 2) : null;
  let maState = "震荡";
  if (ma5 !== null && ma20 !== null && ma60 !== null) {
    const aboveMa20 = close > ma20;
    if (ma5 > ma20 && ma20 > ma60 && aboveMa20) maState = "多头排列";
    else if (ma5 < ma20 && ma20 < ma60 && !aboveMa20) maState = "空头排列";
  }
  const amount = index.amounts[index.amounts.length - 1] ?? 0;
  const base = index.amounts.length > 21 ? mean(index.amounts.slice(-21, -1)) : 0;
  return {
    index: "沪深300",
    ma5: ma5 === null ? null : round(ma5, 2),
    ma20: ma20 === null ? null : round(ma20, 2),
    ma60: ma60 === null ? null : round(ma60, 2),
    ma_state: maState,
    ret5,
    ret20,
    close: round(close, 2),
    amount: amount > 0 ? round(amount / 1e8, 2) : null,
    amount_ratio: amount > 0 && base > 0 ? round(amount / base, 4) : null,
    vol_ratio: null,
    trade_date: index.last.date,
  };
}

function scoreTrend(trend: Record<string, unknown>): number {
  const ma5 = num(trend["ma5"]);
  const ma20 = num(trend["ma20"]);
  const ma60 = num(trend["ma60"]);
  const close = num(trend["close"]);
  const ret5 = num(trend["ret5"]);
  const ret20 = num(trend["ret20"]);
  if (ma5 === null || ma20 === null || ma60 === null) return 50;
  let score = 50;
  if (ma5 > ma20 && ma20 > ma60) score += 20;
  else if (ma5 > ma20) score += 10;
  else if (ma5 < ma20 && ma20 < ma60) score -= 20;
  else score -= 10;
  if (close !== null && ma20 > 0) score += clamp(((close / ma20 - 1) * 100) * 2, -10, 10);
  if (ret5 !== null) score += clamp(ret5 * 2.5, -15, 15);
  if (ret20 !== null) score += clamp(ret20, -10, 10);
  return round(clamp(score, 0, 100), 1);
}

function scoreVolume(trend: Record<string, unknown>): number {
  const ratioRaw = trend["amount_ratio"] ?? trend["vol_ratio"];
  const ratio = num(ratioRaw);
  if (ratio === null) return 50;
  const trendScore = scoreTrend(trend);
  const delta = trendScore >= 50 ? (ratio - 1) * 60 : (1 - ratio) * 60;
  return round(clamp(50 + delta, 0, 100), 1);
}

function isIceMarket(emotion: Record<string, unknown>): boolean {
  const score = num(emotion["score"]) ?? 50;
  const breadth = num(emotion["breadth"]) ?? 0.5;
  const limitUp = num(emotion["limit_up"]) ?? 0;
  const limitDown = num(emotion["limit_down"]) ?? 0;
  if (emotion["label"] === "冰点" || emotion["state"] === "ice") return true;
  if (score < 20 && breadth < 0.15) return true;
  return breadth < 0.2 && limitDown > 1.5 * limitUp && score < 30;
}

function stateFor(score: number, trend: Record<string, unknown>, emotion: Record<string, unknown>): string {
  if (isIceMarket(emotion)) return "ice_watch";
  if (score >= 70 && trend["ma_state"] === "多头排列" && (num(emotion["score"]) ?? 0) >= 55) return "offense";
  if (score >= 55) return "lean_bull";
  if (score >= 42) return "range";
  return "lean_bear";
}

function formatNum(value: unknown, digits = 1): string {
  const n = num(value);
  return n === null ? "N/A" : n.toFixed(digits);
}

function buildRegimePayload(ctx: RegimeContext): Record<string, unknown> {
  const trend = computeTrend(ctx.index);
  const trendScore = scoreTrend(trend);
  const volumeScore = scoreVolume(trend);
  const emotionScore = num(ctx.emotion["score"]) ?? 50;
  const score = round(clamp(0.45 * trendScore + 0.4 * emotionScore + 0.15 * volumeScore, 0, 100), 1);
  const state = stateFor(score, trend, ctx.emotion);
  const label = STATE_LABELS[state];
  const ma5 = num(trend["ma5"]);
  const ma20 = num(trend["ma20"]);
  const ma60 = num(trend["ma60"]);
  const close = num(trend["close"]);
  const breadth = num(ctx.emotion["breadth"]) ?? 0;
  const reasons: string[] = [];
  if (trend["ma_state"] === "多头排列") {
    reasons.push(`沪深300 均线多头排列（MA5 ${formatNum(ma5)} > MA20 ${formatNum(ma20)} > MA60 ${formatNum(ma60)}）${close !== null && ma20 ? `，收盘较 MA20 ${(((close / ma20) - 1) * 100).toFixed(2)}%` : ""}`);
  } else if (trend["ma_state"] === "空头排列") {
    reasons.push(`沪深300 均线空头排列（MA5 ${formatNum(ma5)} < MA20 ${formatNum(ma20)} < MA60 ${formatNum(ma60)}）${close !== null && ma20 ? `，收盘较 MA20 ${(((close / ma20) - 1) * 100).toFixed(2)}%` : ""}`);
  } else {
    reasons.push(`沪深300 均线交织（MA5 ${formatNum(ma5)} / MA20 ${formatNum(ma20)} / MA60 ${formatNum(ma60)}），方向未明`);
  }
  reasons.push(
    `近5日 ${trend["ret5"] === null ? "N/A" : `${(num(trend["ret5"]) as number) > 0 ? "+" : ""}${formatNum(trend["ret5"], 2)}%`}，近20日 ${trend["ret20"] === null ? "N/A" : `${(num(trend["ret20"]) as number) > 0 ? "+" : ""}${formatNum(trend["ret20"], 2)}%`}（${(num(trend["ret5"]) ?? 0) > 0 ? "动量偏强" : "动量偏弱"}）`,
  );
  reasons.push(
    `情绪分 ${emotionScore.toFixed(1)}（${String(ctx.emotion["label"] ?? "中性")}），涨跌家数宽度 ${(breadth * 100).toFixed(1)}%，涨停 ${ctx.emotion["limit_up"] ?? 0} 家 / 跌停 ${ctx.emotion["limit_down"] ?? 0} 家`,
  );
  reasons.push(
    `成交额 ${formatNum(trend["amount"], 0)} 亿元，较 20 日均值 ${formatNum(trend["amount_ratio"], 2)} 倍（${(num(trend["amount_ratio"]) ?? 1) > 1.05 ? "放量" : (num(trend["amount_ratio"]) ?? 1) < 0.95 ? "缩量" : "平量"}）`,
  );
  if (state === "ice_watch") reasons.push("冰点特征：宽度极低且跌停家数明显多于涨停，历史上常伴随短线修复窗口");
  reasons.push(`综合状态分 ${score.toFixed(1)} → ${label}（${STATE_ACTION[state]}）`);

  const leaderSource = ctx.boards.slice(0, 100);
  const r1 = percentileScores(leaderSource.map((b) => b.pct_1d));
  const r5 = percentileScores(leaderSource.map((b) => b.pct_5d));
  const rf = percentileScores(leaderSource.map((b) => b.net_inflow));
  const leaders = leaderSource
    .map((board, i) => {
      let weighted = 0;
      let weightSum = 0;
      if (r1[i] !== null) {
        weighted += 0.34 * (r1[i] as number);
        weightSum += 0.34;
      }
      if (r5[i] !== null) {
        weighted += 0.33 * (r5[i] as number);
        weightSum += 0.33;
      }
      if (rf[i] !== null) {
        weighted += 0.33 * (rf[i] as number);
        weightSum += 0.33;
      }
      const leaderScore = weightSum === 0 ? 0 : round((weighted / weightSum), 1);
      return {
        name: board.board_name,
        kind: board.type,
        pct_1d: board.pct_1d,
        pct_5d: board.pct_5d,
        net_inflow: board.net_inflow,
        net_inflow_5d: null,
        score: leaderScore,
        days: 0,
        tag: "领涨",
      };
    })
    .sort((a, b) => b.score - a.score)
    .slice(0, 5);

  const position = STATE_POSITION[state];
  const tradeDate = String(trend["trade_date"] ?? shanghaiDate());
  return {
    schema: 1,
    asof: nowIso(),
    trade_date: tradeDate,
    status: ctx.boards.length > 0 ? "ok" : "partial",
    market_state: {
      state,
      label,
      score,
      reasons: reasons.slice(0, 5),
      score_parts: { trend: trendScore, emotion: round(emotionScore, 1), volume: volumeScore },
    },
    trend,
    emotion: ctx.emotion,
    leaders,
    position: {
      range: position.range,
      label: position.label,
      rationale: `${position.base}（状态分 ${score.toFixed(1)}，情绪分 ${emotionScore.toFixed(1)}/${String(ctx.emotion["label"] ?? "中性")}，涨跌宽度 ${(breadth * 100).toFixed(1)}%）`,
      triggers: position.triggers,
      disclaimer: DISCLAIMER,
    },
    data_quality: {
      status: ctx.boards.length > 0 ? "ok" : "partial",
      errors: ctx.boards.length > 0 ? [] : ["boards_unavailable"],
      warnings: ["云端简化版：规则复刻本地 src/quant/data/regime.py 核心（趋势/情绪/量能加权 + 状态机 + 仓位映射），板块主线用东财板块涨幅/5日涨幅/净流入百分位近似"],
      sources: { index: "eastmoney(push2his)", emotion: "legulegu", sectors: ctx.boards.length > 0 ? "eastmoney(clist)" : "none" },
    },
    disclaimer: DISCLAIMER,
  };
}

// ------------------------------------------------------------ 摘要 ----

const POSITION_TEXT: Record<string, string> = {
  offense: "仓位可积极（6-8成），跟随趋势主线，跌破20日线再降仓",
  lean_bull: "仓位4-6成，跟随主线避免追高，保留机动仓位",
  range: "仓位2-4成，低吸高抛为主，不做趋势性加仓",
  lean_bear: "仓位0-2成，防守为主，等企稳信号再加仓",
  ice_watch: "仓位0-2成，只做小仓位冰点修复试错，跌停潮收敛前不重仓",
};

function verdictText(score: number, emotionLabel: string): string {
  if (score < 20) return `情绪分仅 ${score.toFixed(1)}，处于冰点区，市场属于防守/退潮格局，左侧只宜小仓位试错`;
  if (score < 40) return `情绪分 ${score.toFixed(1)}（${emotionLabel}），赚钱效应偏弱，市场以防守为主，等待企稳信号`;
  if (score < 55) return `情绪分 ${score.toFixed(1)}（${emotionLabel}），多空分化，市场处于震荡修复格局，不宜追高`;
  if (score < 70) return `情绪分 ${score.toFixed(1)}（${emotionLabel}），情绪偏暖，属于弱进攻/修复格局，跟随主线为主`;
  return `情绪分 ${score.toFixed(1)}（${emotionLabel}），情绪过热，进攻氛围浓但需防冲高回落`;
}

function watchText(sentimentScore: number, breadth: number, ma20: number | null, close: number | null): string {
  const widthTarget = "30%";
  let watch = `明日关注涨跌宽度能否回升至 ${widthTarget} 以上、涨停家数能否超过跌停家数`;
  if (ma20 !== null && close !== null) {
    watch += `，以及沪深300 能否${close >= ma20 ? "守住" : "站上"} 20 日线（${ma20.toFixed(2)}）`;
  }
  if (sentimentScore < 20) watch = `明日关注跌停家数能否收敛至 30 家以内、涨跌宽度能否从 ${(breadth * 100).toFixed(1)}% 回升，以及沪深300 能否止跌`;
  return watch;
}

function clampChars(text: string, min = 200, max = 320): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}

function buildSummaryData(
  session: "midday" | "close",
  snapshot: Record<string, unknown> | null,
  boards: BoardRow[],
  indexDailies: IndexDaily[],
  events: Array<Record<string, unknown>>,
): { text: string; payload: Record<string, unknown> } {
  if (!snapshot) throw new Error("没有市场快照，无法生成摘要");
  const indices = Array.isArray(snapshot["indices"]) ? (snapshot["indices"] as Array<Record<string, unknown>>) : [];
  const sentiment = (snapshot["sentiment"] as Record<string, unknown>) ?? {};
  const activity = (snapshot["activity"] as Record<string, unknown>) ?? {};
  const tradeDate = String(snapshot["trade_date"] ?? shanghaiDate());
  const score = num(sentiment["score"]) ?? 50;
  const label = String(sentiment["label"] ?? "中性");
  const breadth = num(sentiment["breadth"]) ?? 0.5;
  const pct = (code: string): string => {
    const item = indices.find((x) => x["code"] === code);
    let v = num(item?.["pct_chg"]);
    if (v === null) {
      const daily = indexDailies.find((d) => d.code === code);
      v = daily ? daily.last.pctChg : null;
    }
    return v === null ? "N/A" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
  };
  const up = num(activity["up"]) ?? 0;
  const down = num(activity["down"]) ?? 0;
  const limitUp = num(activity["limit_up"]) ?? 0;
  const limitDown = num(activity["limit_down"]) ?? 0;

  const sh = indexDailies.find((d) => d.code === "sh000001");
  const sz = indexDailies.find((d) => d.code === "sz399001");
  const hs300 = indexDailies.find((d) => d.code === "sh000300");
  let amountText = "成交额数据不足";
  if (sh && sz) {
    const shAmt = sh.amounts[sh.amounts.length - 1] ?? 0;
    const szAmt = sz.amounts[sz.amounts.length - 1] ?? 0;
    const shBase = mean(sh.amounts.slice(-21, -1));
    const szBase = mean(sz.amounts.slice(-21, -1));
    const total = (shAmt + szAmt) / 1e8;
    const base = (shBase + szBase) / 1e8;
    const ratio = base > 0 ? total / base : null;
    const tone = ratio === null ? "" : ratio > 1.05 ? "放量" : ratio < 0.95 ? "缩量" : "平量";
    amountText = `两市成交额约 ${total.toFixed(0)} 亿元${ratio === null ? "" : `（较20日均值 ${ratio.toFixed(2)} 倍，${tone}）`}`;
  }

  const boardRows = boards.filter((b) => b.pct_1d !== null).sort((a, b) => (b.pct_1d as number) - (a.pct_1d as number));
  const topBoards = boardRows.slice(0, 3);
  const boardText = topBoards.length > 0 ? `${topBoards.map((b) => `${b.board_name}(${(b.pct_1d as number).toFixed(2)}%)`).join("、")} 领涨` : "板块数据暂缺";
  const inflowBoard = boards.filter((b) => b.net_inflow !== null).sort((a, b) => (b.net_inflow as number) - (a.net_inflow as number))[0];
  const inflowText = inflowBoard ? `，${inflowBoard.board_name} 资金净流入 ${(inflowBoard.net_inflow as number).toFixed(1)} 亿居前` : "";
  const eventCounts = events.reduce<{ bullish: number; bearish: number; neutral: number }>(
    (acc, item) => {
      const s = String(item["sentiment"] ?? "neutral");
      if (s === "bullish") acc.bullish += 1;
      else if (s === "bearish") acc.bearish += 1;
      else acc.neutral += 1;
      return acc;
    },
    { bullish: 0, bearish: 0, neutral: 0 },
  );

  const avgPct = mean(
    indices
      .map((x) => num(x["pct_chg"]))
      .filter((v): v is number => v !== null),
  );
  const dirText = avgPct > 0.5 ? "指数全线偏强" : avgPct < -0.5 ? "指数整体承压" : "指数涨跌互现";
  const judgement = `${dirText}，${verdictText(score, label)}`;
  const state =
    score < 20 ? "ice_watch" : score < 42 ? "lean_bear" : score < 55 ? "range" : score < 70 ? "lean_bull" : "offense";
  const watch = watchText(score, breadth, num(hs300 ? ma(hs300.closes, 20) : null), num(hs300?.last.close ?? null));
  const sessionLabel = session === "midday" ? "午评" : "收评";
  const positionText = POSITION_TEXT[state] ?? POSITION_TEXT.range;

  let text =
    `${sessionLabel}：上证 ${pct("sh000001")}、深成 ${pct("sz399001")}、创业板 ${pct("sz399006")}、沪深300 ${pct("sh000300")}，` +
    `${amountText}；上涨 ${up} 家、下跌 ${down} 家，涨停 ${limitUp} 家 / 跌停 ${limitDown} 家，情绪分 ${score.toFixed(1)}（${label}）。` +
    `板块方面，${boardText}${inflowText}；${judgement}。建议${positionText}。${watch}。${DISCLAIMER}。`;
  if (text.length < 200) {
    text += `当前状态判为「${STATE_LABELS[state] ?? "震荡"}」，${STATE_ACTION[state] ?? "保持观察"}；操作上以纪律和仓位控制优先，不追涨杀跌。${DISCLAIMER}。`;
  }
  text = clampChars(text);

  return {
    text,
    payload: {
      schema: 1,
      session,
      session_label: sessionLabel,
      trade_date: tradeDate,
      asof: nowIso(),
      generated_by: "worker-cron",
      indices: indices.map((x) => ({ code: x["code"], name: x["name"], price: x["price"], pct_chg: x["pct_chg"] })),
      activity: { up, down, limit_up: limitUp, limit_down: limitDown, active_pct: activity["active_pct"] ?? null },
      sentiment: { score, label, state: sentiment["state"] ?? null, breadth },
      boards_top: topBoards.map((b) => ({ name: b.board_name, type: b.type, pct_1d: b.pct_1d, pct_5d: b.pct_5d, net_inflow: b.net_inflow })),
      events: eventCounts,
      judgement,
      watch,
      position: { state, label: STATE_LABELS[state] ?? "震荡", text: positionText },
      disclaimer: DISCLAIMER,
    },
  };
}

// ------------------------------------------------------------ 编排 ----

export interface DailySyncResult {
  ok: boolean;
  asof: string;
  trade_date: string;
  duration_ms: number;
  steps: SyncStep[];
  warnings: string[];
  asofs: Record<string, string | null>;
}

/** 生成收盘市场快照（4 大指数日线 + 乐咕涨跌家数）。 */
async function buildCloseSnapshot(env: Env): Promise<{
  snapshot: Record<string, unknown>;
  indexDailies: IndexDaily[];
  activity: Activity | null;
  warnings: string[];
}> {
  const warnings: string[] = [];
  const activity = await fetchActivity();
  if (!activity) warnings.push("legulegu 涨跌家数不可用");
  const indexDailies: IndexDaily[] = [];
  for (const index of INDEXES) {
    try {
      indexDailies.push(await fetchIndexDaily(index));
    } catch (err) {
      warnings.push(`${index.code}:${errorMessage(err)}`);
      try {
        indexDailies.push(await fetchTencentIndexQuote(index));
        warnings.push(`${index.code}:腾讯行情兜底`);
      } catch (err2) {
        warnings.push(`${index.code} 腾讯兜底失败:${errorMessage(err2)}`);
      }
    }
    await sleep(THROTTLE_MS);
  }
  if (indexDailies.length === 0) throw new Error(`指数日线全部失败：${warnings.join("; ")}`);

  const indices = indexDailies.map((daily) => {
    const d = indexDirection(daily.closes);
    return {
      code: daily.code,
      name: daily.name,
      price: daily.last.close,
      pct_chg: daily.last.pctChg,
      direction: d.direction,
      prob: d.prob,
      confidence: d.confidence,
      ma_state: d.ma_state,
    };
  });
  const avg = {
    up: mean(indices.map((x) => x.prob.up)),
    flat: mean(indices.map((x) => x.prob.flat)),
    down: mean(indices.map((x) => x.prob.down)),
  };
  const order = ["up", "flat", "down"] as const;
  const values = [avg.up, avg.flat, avg.down];
  let bestIdx = 0;
  values.forEach((v, i) => {
    if (v > values[bestIdx]) bestIdx = i;
  });
  const sentiment = activity
    ? computeSentiment(activity)
    : { score: 50, state: "neutral", label: "中性", breadth: 0.5, limit_ratio: 0.5, active_pct: 0 };
  const tradeDate = indexDailies[0].last.date || activity?.asof?.slice(0, 10) || shanghaiDate();
  const snapshot: Record<string, unknown> = {
    schema: 1,
    asof: `${tradeDate}T15:00:00+08:00`,
    trade_date: tradeDate,
    status: activity ? "ok" : "partial",
    live: false,
    session: "close",
    source: "worker-cron:daily-sync",
    errors: warnings,
    indices,
    activity: activity ?? {
      up: 0,
      down: 0,
      flat: 0,
      suspended: 0,
      limit_up: 0,
      limit_down: 0,
      active_pct: 0,
      asof: "",
    },
    sentiment,
    market_direction: {
      direction: order[bestIdx],
      prob: {
        up: round(avg.up, 4),
        flat: round(avg.flat, 4),
        down: round(avg.down, 4),
      },
      confidence: round(values[bestIdx], 4),
    },
  };
  await insertMarketSnapshot(env.DB, snapshot);
  return { snapshot, indexDailies, activity, warnings };
}

/** 收盘同步主流程（15:40 Cron）：市场快照 -> K 线 -> 事件 -> 板块 -> 简化 regime。 */
export async function syncDailyClose(env: Env): Promise<DailySyncResult> {
  const startedAt = Date.now();
  const steps: SyncStep[] = [];
  const warnings: string[] = [];
  const asofs: Record<string, string | null> = { market: null, klines: null, events: null, boards: null, regime: null };

  // a) 指数日线 -> 市场快照
  const marketOutcome = await runStep(steps, "market_snapshot", async () => buildCloseSnapshot(env), (value) => ({
    trade_date: value.snapshot["trade_date"],
    indices: value.indexDailies.length,
    activity: value.activity !== null,
    warnings: value.warnings,
  }));
  const market = marketOutcome.value;
  if (market) {
    asofs.market = String(market.snapshot["asof"]);
    warnings.push(...market.warnings);
  } else {
    warnings.push("market_snapshot 失败，后续 regime 将降级");
  }

  // b) 个股 K 线（股票池 40 只，串行 + 250ms）
  const klineOutcome = await runStep(
    steps,
    "klines",
    async () => {
      let symbols = await queryStockSymbols(env.DB, STOCK_POOL_SIZE);
      let poolSource = "db:stock_names";
      if (symbols.length < STOCK_POOL_SIZE) {
        const fallback = FALLBACK_STOCK_POOL.filter((s) => !symbols.includes(s));
        symbols = [...symbols, ...fallback].slice(0, STOCK_POOL_SIZE);
        poolSource = symbols.length > 0 ? "db+fallback" : "fallback";
      }
      const failed: string[] = [];
      let symbolsDone = 0;
      let rowsWritten = 0;
      for (const symbol of symbols) {
        try {
          const rows = await fetchStockKlines(symbol);
          if (rows.length === 0) throw new Error("empty");
          rowsWritten += await upsertKlines(env.DB, rows);
          symbolsDone += 1;
          asofs.klines = rows[rows.length - 1].trade_date;
        } catch (err) {
          failed.push(`${symbol}:${errorMessage(err)}`);
        }
        await sleep(THROTTLE_MS);
      }
      if (symbolsDone === 0) throw new Error(`K 线全部失败：${failed.slice(0, 3).join("; ")}`);
      return { symbols: symbols.length, symbols_done: symbolsDone, rows: rowsWritten, failed: failed.slice(0, 5), pool_source: poolSource };
    },
    (value) => ({ ...value }),
  );
  if (klineOutcome.value) {
    if (klineOutcome.value.failed.length > 0) warnings.push(`klines 部分失败：${klineOutcome.value.failed.join("; ")}`);
  }

  // c) 事件（东财 7x24 快讯；财联社签名接口不可用时降级）
  const eventsOutcome = await runStep(
    steps,
    "events",
    async () => {
      const items = await fetchEastmoneyNews(120);
      const accepted = await upsertEvents(env.DB, items as unknown as Array<Record<string, unknown>>);
      const counts = items.reduce<{ bullish: number; bearish: number; neutral: number }>(
        (acc, item) => {
          if (item.sentiment === "bullish") acc.bullish += 1;
          else if (item.sentiment === "bearish") acc.bearish += 1;
          else acc.neutral += 1;
          return acc;
        },
        { bullish: 0, bearish: 0, neutral: 0 },
      );
      asofs.events = items[0]?.published_at ?? nowIso();
      return { fetched: items.length, accepted, ...counts };
    },
    (value) => ({ ...value }),
  );
  if (!eventsOutcome.value) warnings.push("events 失败（东财快讯不可用；财联社接口需签名，云端跳过）");

  // d) 板块行情（东财 clist；失败降级新浪 GBK）
  const boardsOutcome = await runStep(
    steps,
    "boards",
    async () => {
      const result = await collectBoards();
      if (result.errors.length > 0) warnings.push(...result.errors);
      if (result.rows.length === 0) throw new Error(`板块全部失败：${result.errors.join("; ")}`);
      const count = await upsertBoards(env.DB, result.rows);
      asofs.boards = result.rows[0]?.updated_at ?? nowIso();
      return { count, source: result.source, industry: result.rows.filter((r) => r.type === "industry").length, concept: result.rows.filter((r) => r.type === "concept").length };
    },
    (value) => ({ ...value }),
  );

  // e) 简化 regime（依赖市场快照的沪深300 + 情绪 + 板块）
  await runStep(
    steps,
    "regime",
    async () => {
      if (!market) throw new Error("缺少指数/情绪数据，无法生成 regime");
      const hs300 = market.indexDailies.find((d) => d.code === "sh000300") ?? market.indexDailies[0];
      const sentiment = (market.snapshot["sentiment"] as Record<string, unknown>) ?? {};
      const emotion = {
        score: num(sentiment["score"]) ?? 50,
        label: String(sentiment["label"] ?? "中性"),
        state: String(sentiment["state"] ?? "neutral"),
        breadth: num(sentiment["breadth"]) ?? 0.5,
        limit_up: num(market.activity?.limit_up) ?? 0,
        limit_down: num(market.activity?.limit_down) ?? 0,
        limit_ratio: num(sentiment["limit_ratio"]) ?? 0.5,
        active_pct: num(sentiment["active_pct"]) ?? 0,
      };
      const regime = buildRegimePayload({ index: hs300, emotion, boards: boardsOutcome.value ? (await queryBoardsForRegime(env)) : [] });
      await insertJsonSnapshot(env.DB, "regime_snapshots", regime);
      asofs.regime = String(regime["asof"]);
      return { asof: asofs.regime, trade_date: regime["trade_date"], state: (regime["market_state"] as Record<string, unknown>)["label"] };
    },
    (value) => ({ ...value }),
  );

  const ok = steps.some((s) => s.ok);
  const tradeDate = market ? String(market.snapshot["trade_date"]) : shanghaiDate();
  return {
    ok,
    asof: nowIso(),
    trade_date: tradeDate,
    duration_ms: Date.now() - startedAt,
    steps,
    warnings,
    asofs,
  };
}

/** regime 需要刚写入的板块数据（从 D1 读回，避免内存与写入不一致）。 */
async function queryBoardsForRegime(env: Env): Promise<BoardRow[]> {
  try {
    return await queryBoards(env.DB, { limit: 200 });
  } catch (err) {
    console.warn(`[daily_sync] 读取 boards 失败：${errorMessage(err)}`);
    return [];
  }
}

/** 盘中：4 大指数分时（trends2）→ intraday_index（每次覆盖当日分钟序列）。 */
export async function refreshIntradayIndex(env: Env, force = false): Promise<{ ok: boolean; updated: number; skipped: string[]; errors: string[] }> {
  const result = { ok: false, updated: 0, skipped: [] as string[], errors: [] as string[] };
  if (!force && !isTradingSession()) {
    result.skipped.push("off_session");
    return result;
  }
  const today = shanghaiDate();
  for (const index of INDEXES) {
    try {
      const data = await fetchIndexIntraday(index);
      if (!force && data.trade_date !== today) {
        result.skipped.push(`${index.code}:stale_date(${data.trade_date})`);
        continue;
      }
      await upsertIntradayIndex(env.DB, index.code, data.trade_date, { schema: 1, ...data }, nowIso());
      result.updated += 1;
    } catch (err) {
      result.errors.push(`${index.code}:${errorMessage(err)}`);
    }
    await sleep(THROTTLE_MS);
  }
  result.ok = result.updated > 0;
  return result;
}

/** 午评/收评：先刷新市场快照，再取指数成交额/板块/事件生成模板化摘要。 */
export async function buildMarketSummary(env: Env, session: "midday" | "close"): Promise<{ ok: boolean; trade_date: string; asof: string; summary_text: string; duration_ms: number }> {
  const startedAt = Date.now();
  // 1) 刷新市场快照（force=true：摘要必须基于最新数据；失败则用 D1 里已有快照）
  try {
    await refreshMarketLive(env, true);
  } catch (err) {
    console.warn(`[daily_sync] summary 前刷新 market 失败：${errorMessage(err)}`);
  }
  const latest = await queryLatestMarket(env.DB);
  const snapshot = latest ? latest.snapshot : null;
  if (!snapshot) throw new Error("没有市场快照，无法生成摘要");

  // 2) 指数成交额（上证+深成）+ 沪深300 均线：独立拉 3 条日线
  const indexDailies: IndexDaily[] = [];
  for (const index of [INDEXES[0], INDEXES[1], INDEXES[3]]) {
    try {
      indexDailies.push(await fetchIndexDaily(index));
    } catch (err) {
      console.warn(`[daily_sync] summary 指数日线失败 ${index.code}: ${errorMessage(err)}`);
      try {
        indexDailies.push(await fetchTencentIndexQuote(index));
      } catch (err2) {
        console.warn(`[daily_sync] summary 腾讯兜底失败 ${index.code}: ${errorMessage(err2)}`);
      }
    }
    await sleep(THROTTLE_MS);
  }

  // 3) 板块（东财；失败降级新浪；再失败用 D1 已存板块）
  let boards: BoardRow[] = [];
  const boardResult = await collectBoards().catch((err) => ({ rows: [] as BoardRow[], source: "none", errors: [errorMessage(err)] }));
  boards = boardResult.rows;
  if (boards.length === 0) {
    try {
      boards = await queryBoardsForRegime(env);
    } catch {
      boards = [];
    }
  }

  // 4) 事件（D1 最新快讯，只取最近 20 条做情绪统计）
  let events: Array<Record<string, unknown>> = [];
  try {
    events = await queryEvents(env.DB, 20);
  } catch {
    events = [];
  }

  const { text, payload } = buildSummaryData(session, snapshot, boards, indexDailies, events);
  const tradeDate = String(snapshot["trade_date"] ?? shanghaiDate());
  const asof = nowIso();
  await upsertMarketSummary(env.DB, {
    trade_date: tradeDate,
    session,
    asof,
    summary_text: text,
    payload,
    created_at: new Date().toISOString(),
  });
  return { ok: true, trade_date: tradeDate, asof, summary_text: text, duration_ms: Date.now() - startedAt };
}

/** 盘中 Cron：市值快照 + 分时。 */
export async function runIntradayRefresh(env: Env): Promise<Record<string, unknown>> {
  const market = await refreshMarketLive(env);
  const intraday = await refreshIntradayIndex(env);
  return { ok: market.ok || intraday.ok, market, intraday };
}

/** 手动/临时 cron 全量触发：收盘同步 + 分时 + 午评/收评。 */
export async function runManualDailySync(env: Env, mode: "all" | "close" | "midday" | "intraday" = "all"): Promise<Record<string, unknown>> {
  const startedAt = Date.now();
  const steps: SyncStep[] = [];
  const output: Record<string, unknown> = { schema: 1, mode, started_at: nowIso() };

  if (mode === "all" || mode === "close") {
    const sync = await syncDailyClose(env);
    steps.push(...sync.steps);
    output.close_sync = { ok: sync.ok, trade_date: sync.trade_date, asofs: sync.asofs, warnings: sync.warnings };
  }
  if (mode === "all" || mode === "close" || mode === "midday") {
    const intradayOutcome = await runStep(steps, "intraday_index", async () => {
      const r = await refreshIntradayIndex(env, true);
      if (!r.ok) throw new Error(`intraday 全部失败：${r.errors.join("; ")}`);
      return r;
    }, (value) => ({ updated: value.updated, skipped: value.skipped, errors: value.errors }));
    if (intradayOutcome.value) output.intraday = intradayOutcome.value;
  }
  if (mode === "all" || mode === "midday") {
    const middayOutcome = await runStep(steps, "summary_midday", async () => buildMarketSummary(env, "midday"), (value) => ({
      trade_date: value.trade_date,
      asof: value.asof,
      chars: value.summary_text.length,
      duration_ms: value.duration_ms,
    }));
    if (middayOutcome.value) output.midday = middayOutcome.value;
  }
  if (mode === "all" || mode === "close" || mode === "intraday") {
    const closeOutcome = await runStep(
      steps,
      "summary_close",
      async () => {
        const result = await buildMarketSummary(env, "close");
        return result;
      },
      (value) => ({ trade_date: value.trade_date, asof: value.asof, chars: value.summary_text.length, duration_ms: value.duration_ms }),
    );
    if (closeOutcome.value) output.close = closeOutcome.value;
  }

  const ok = steps.every((s) => s.ok);
  output.ok = ok;
  output.steps = steps;
  output.duration_ms = Date.now() - startedAt;
  output.finished_at = nowIso();
  return output;
}

/** Cron 分发入口（index.ts scheduled 调用；未知 cron 走盘中默认分支）。 */
export async function handleScheduledCron(cron: string, env: Env): Promise<Record<string, unknown>> {
  if (cron === "40 7 * * 1-5") return (await syncDailyClose(env)) as unknown as Record<string, unknown>;
  if (cron === "35 3 * * 1-5") {
    const market = await runIntradayRefresh(env);
    const summary = await runStep([], "summary_midday", async () => buildMarketSummary(env, "midday"));
    return { ok: true, market, summary: summary.value };
  }
  if (cron === "5 7 * * 1-5") {
    const market = await runIntradayRefresh(env);
    const summary = await runStep([], "summary_close", async () => buildMarketSummary(env, "close"));
    return { ok: true, market, summary: summary.value };
  }
  if (cron === "* * * * *") {
    // 临时验证 cron：部署后手动加，验证各表/接口后从 wrangler.local.toml 移除
    const result = await runManualDailySync(env, "all");
    console.log(`[daily_sync] 临时验证 cron 完成：${JSON.stringify({ ok: result["ok"], steps: (result["steps"] as SyncStep[]).map((s) => ({ name: s.name, ok: s.ok, ms: s.ms, error: s.error })) })}`);
    return result;
  }
  // 默认：盘中 */5 刷新（保留原逻辑）
  return await runIntradayRefresh(env);
}
