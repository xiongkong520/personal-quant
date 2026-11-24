/**
 * 全市场搜索（GET /api/search?q=）：
 * - 代码前缀（600519 / 600）
 * - 名称包含（茅台）
 * - 拼音首字母（GZMT / gz）
 * 数据来自 D1 stock_names（本地 Python 推送）+ 静态股票池补名称/拼音；
 * 不修改 db.ts / market_live.ts 核心逻辑。
 */
import { STOCK_POOL } from "./stock_pool.ts";
import type { Env } from "./types.ts";

export interface SearchItem {
  symbol: string;
  name: string;
  initials?: string;
}

interface NameRow {
  symbol: string;
  name: string;
}

let nameCache: { at: number; rows: NameRow[] } | null = null;
const NAME_CACHE_MS = 60_000;

async function loadNameRows(env: Env): Promise<NameRow[]> {
  if (nameCache && Date.now() - nameCache.at < NAME_CACHE_MS) return nameCache.rows;
  let rows: NameRow[] = [];
  if (env.DB) {
    try {
      const result = await env.DB.prepare("SELECT symbol, name FROM stock_names LIMIT 500").all<NameRow>();
      rows = (result.results ?? []).filter((r) => /^\d{6}$/.test(r.symbol) && r.name);
    } catch {
      rows = [];
    }
  }
  nameCache = { at: Date.now(), rows };
  return rows;
}

/** 对一条查询打分（越小越靠前；-1 表示不匹配）。 */
function scoreOf(query: string, symbol: string, name: string, initials: string): number {
  const q = query.trim();
  if (!q) return -1;
  const upper = q.toUpperCase();
  if (symbol === q) return 0;
  if (/^\d+$/.test(q) && symbol.startsWith(q)) return 1;
  if (name === q) return 2;
  if (name.startsWith(q)) return 3;
  if (name.includes(q)) return 4;
  if (!/^\d+$/.test(q) && initials.toUpperCase().startsWith(upper)) return 5;
  if (!/^\d+$/.test(q) && initials.toUpperCase().includes(upper)) return 6;
  return -1;
}

export async function searchStocks(env: Env, query: string, limit = 12): Promise<SearchItem[]> {
  const q = query.trim();
  if (!q) return [];
  const rows = await loadNameRows(env);
  const names = new Map<string, string>();
  for (const row of rows) names.set(row.symbol, row.name);
  const initials = new Map<string, string>();
  for (const item of STOCK_POOL) {
    if (!names.has(item.symbol)) names.set(item.symbol, item.name);
    initials.set(item.symbol, item.initials);
  }
  const scored: Array<{ score: number; symbol: string; name: string; initials?: string }> = [];
  for (const [symbol, name] of names) {
    const ini = initials.get(symbol) ?? "";
    const score = scoreOf(q, symbol, name, ini);
    if (score >= 0) scored.push({ score, symbol, name, initials: ini || undefined });
  }
  scored.sort((a, b) => a.score - b.score || a.symbol.localeCompare(b.symbol));
  return scored.slice(0, Math.max(1, Math.min(limit, 30))).map((item) => ({
    symbol: item.symbol,
    name: item.name,
    initials: item.initials,
  }));
}
