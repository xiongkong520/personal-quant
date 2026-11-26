/**
 * 实时 K 线（GET /api/kline）：指数 / 个股都实时从东财拉取并缓存 5 分钟。
 * D1 瘦身后不再落库历史 K 线（历史行情在本地 Parquet），云端只做实时视图。
 */
import { INDEX_NAMES } from "./stock_pool.ts";

const SECIDS: Record<string, string> = {
  sh000001: "1.000001",
  sz399001: "0.399001",
  sz399006: "0.399006",
  sh000300: "1.000300",
  sh000688: "1.000688",
  sh000905: "1.000905",
  sh000852: "1.000852",
};

const INDEX_CODE_SET = new Set(Object.keys(SECIDS));

export interface IndexKlineItem {
  symbol: string;
  trade_date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  amount: number | null;
  pct_chg: number | null;
  ma5: number | null;
  ma10: number | null;
  ma20: number | null;
}

interface CachedKline {
  at: number;
  items: IndexKlineItem[];
}

const cache = new Map<string, CachedKline>();
const CACHE_MS = 5 * 60_000;

function round(value: number, digits = 2): number {
  const pow = 10 ** digits;
  return Math.round(value * pow) / pow;
}

function ma(closes: number[], window: number): number | null {
  if (closes.length < window) return null;
  const slice = closes.slice(-window);
  return round(slice.reduce((a, b) => a + b, 0) / slice.length, 2);
}

function secidOf(symbol: string): string | null {
  if (SECIDS[symbol]) return SECIDS[symbol];
  if (/^sh\d{6}$/.test(symbol)) return `1.${symbol.slice(2)}`;
  if (/^sz\d{6}$/.test(symbol)) return `0.${symbol.slice(2)}`;
  if (/^bj\d{6}$/.test(symbol)) return `0.${symbol.slice(2)}`;
  if (/^\d{6}$/.test(symbol)) {
    // 东财 secid：6/9 开头为沪市，其余深市/北交所
    return symbol.startsWith("6") || symbol.startsWith("9") ? `1.${symbol}` : `0.${symbol}`;
  }
  return null;
}

async function fetchKlineRaw(symbol: string, fqt: number): Promise<IndexKlineItem[] | null> {
  const secid = secidOf(symbol);
  if (!secid) return null;
  const url =
    "https://push2his.eastmoney.com/api/qt/stock/kline/get" +
    `?secid=${secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=${fqt}&beg=20240101&end=20500101`;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 6000);
    const resp = await fetch(url, {
      signal: controller.signal,
      headers: { "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)" },
    }).finally(() => clearTimeout(timer));
    if (!resp.ok) return null;
    const payload = (await resp.json()) as { data?: { klines?: string[] } };
    const lines = payload?.data?.klines ?? [];
    const items: IndexKlineItem[] = [];
    const closes: number[] = [];
    for (const line of lines) {
      const parts = line.split(",");
      if (parts.length < 7) continue;
      const date = parts[0];
      const open = Number(parts[1]);
      const close = Number(parts[2]);
      const high = Number(parts[3]);
      const low = Number(parts[4]);
      const volume = Number(parts[5]);
      const amount = Number(parts[6]);
      if (!date || !Number.isFinite(close)) continue;
      closes.push(close);
      const idx = closes.length - 1;
      const prev = idx > 0 ? closes[idx - 1] : null;
      items.push({
        symbol,
        trade_date: date,
        open: Number.isFinite(open) ? round(open) : null,
        high: Number.isFinite(high) ? round(high) : null,
        low: Number.isFinite(low) ? round(low) : null,
        close: round(close),
        volume: Number.isFinite(volume) ? volume : null,
        amount: Number.isFinite(amount) ? amount : null,
        pct_chg: prev && prev > 0 ? round((close / prev - 1) * 100) : null,
        ma5: null,
        ma10: null,
        ma20: null,
      });
    }
    if (items.length === 0) return null;
    for (const item of items) {
      item.ma5 = ma(closes.slice(0, closes.indexOf(item.close as number) + 1), 5);
      item.ma10 = ma(closes.slice(0, closes.indexOf(item.close as number) + 1), 10);
      item.ma20 = ma(closes.slice(0, closes.indexOf(item.close as number) + 1), 20);
    }
    return items;
  } catch {
    return null;
  }
}

/** 指数 K 线（兼容保留：sh000001/sz399001/sz399006/sh000300/sh000688/sh000905/sh000852）。 */
export async function fetchIndexKline(symbol: string, limit = 120): Promise<IndexKlineItem[] | null> {
  if (!INDEX_CODE_SET.has(symbol)) return null;
  return fetchRealtimeKline(symbol, limit, 1);
}

/** 个股/指数通用实时 K 线（个股前复权 fqt=1，与"个股 K 线用前复权"口径一致）。 */
export async function fetchRealtimeKline(symbol: string, limit = 120, fqt = 1): Promise<IndexKlineItem[] | null> {
  const cap = Math.min(Math.max(Math.trunc(limit), 1), 250);
  const cached = cache.get(`${symbol}:${fqt}:${cap}`);
  if (cached && Date.now() - cached.at < CACHE_MS) return cached.items;
  const items = await fetchKlineRaw(symbol, fqt);
  if (!items || items.length === 0) return null;
  const sliced = items.slice(-cap);
  cache.set(`${symbol}:${fqt}:${cap}`, { at: Date.now(), items: sliced });
  return sliced;
}

/** 指数名称（/api/kline 返回 name 用）。 */
export function indexName(symbol: string): string {
  return INDEX_NAMES[symbol] ?? symbol;
}

export function isIndexSymbol(symbol: string): boolean {
  return INDEX_CODE_SET.has(symbol) || /^(sh|sz)\d{6}$/.test(symbol);
}
