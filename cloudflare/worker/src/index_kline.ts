/**
 * 指数日 K 线（GET /api/kline?symbol=sh000001 等）：实时从东方财富拉取并缓存 5 分钟。
 * 不写 D1、不依赖 db.ts / market_live.ts；网络失败返回 null 由调用方决定降级。
 */
import { INDEX_NAMES } from "./stock_pool.ts";

const SECIDS: Record<string, string> = {
  sh000001: "1.000001",
  sz399001: "0.399001",
  sz399006: "0.399006",
  sh000300: "1.000300",
};

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

async function fetchFromEastmoney(symbol: string): Promise<IndexKlineItem[] | null> {
  const secid = SECIDS[symbol];
  if (!secid) return null;
  const url =
    "https://push2his.eastmoney.com/api/qt/stock/kline/get" +
    `?secid=${secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&beg=20240101&end=20500101`;
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
    const closes: number[] = [];
    const items: IndexKlineItem[] = [];
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
        pct_chg: prev ? round((close / prev - 1) * 100) : null,
        ma5: idx >= 4 ? round(closes.slice(idx - 4, idx + 1).reduce((s, v) => s + v, 0) / 5, 3) : null,
        ma10: idx >= 9 ? round(closes.slice(idx - 9, idx + 1).reduce((s, v) => s + v, 0) / 10, 3) : null,
        ma20: idx >= 19 ? round(closes.slice(idx - 19, idx + 1).reduce((s, v) => s + v, 0) / 20, 3) : null,
      });
    }
    return items.length ? items : null;
  } catch {
    return null;
  }
}

export async function fetchIndexKline(symbol: string, limit: number): Promise<{
  name: string;
  items: IndexKlineItem[];
} | null> {
  if (!SECIDS[symbol]) return null;
  let items: IndexKlineItem[] | null = null;
  const cached = cache.get(symbol);
  if (cached && Date.now() - cached.at < CACHE_MS) {
    items = cached.items;
  } else {
    items = await fetchFromEastmoney(symbol);
    if (items) cache.set(symbol, { at: Date.now(), items });
  }
  if (!items) return null;
  const tail = items.slice(-Math.max(1, Math.min(limit, 250)));
  return { name: INDEX_NAMES[symbol] ?? symbol, items: tail };
}
