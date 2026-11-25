/**
 * 盘中实时刷新（Worker Cron 触发）：
 * - 指数行情：东方财富历史 K 线接口（JSON，盘中返回最新价）
 * - 涨跌家数/涨停跌停/活跃度：乐咕赚钱效应页面（HTML → 纯文本正则解析）
 * - 计算情绪分与冰点，写 D1 market_snapshots（Pages 前端 /api/market 实时读取）
 *
 * 注意：Worker 只负责「写」，Pages Functions 只负责「读」，共享同一个 D1。
 */
import { insertMarketSnapshot } from "./db.ts";
import type { Env } from "./types.ts";

export interface IndexQuote {
  code: string;
  name: string;
  price: number;
  pct_chg: number;
  closes: number[];
}

export const INDEXES = [
  { code: "sh000001", name: "上证指数", secid: "1.000001" },
  { code: "sz399001", name: "深证成指", secid: "0.399001" },
  { code: "sz399006", name: "创业板指", secid: "0.399006" },
  { code: "sh000300", name: "沪深300", secid: "1.000300" },
];

/** 腾讯行情兜底（东财被限流时）：GBK 解码，返回最新价/昨收。 */
async function fetchTencentQuote(code: string): Promise<{ price: number; prevClose: number } | null> {
  try {
    const resp = await fetch(`https://qt.gtimg.cn/q=${code}`, {
      headers: { "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)" },
    });
    if (!resp.ok) return null;
    const buffer = await resp.arrayBuffer();
    let text: string;
    try {
      text = new TextDecoder("gbk").decode(buffer);
    } catch {
      text = new TextDecoder("utf-8").decode(buffer);
    }
    const match = text.match(/="([^"]+)"/);
    if (!match) return null;
    const parts = match[1].split("~");
    const price = Number(parts[3]);
    const prevClose = Number(parts[4]);
    if (!Number.isFinite(price) || !Number.isFinite(prevClose) || prevClose === 0) return null;
    return { price, prevClose };
  } catch {
    return null;
  }
}

export async function fetchIndex(cl: { code: string; name: string; secid: string }): Promise<IndexQuote | null> {
  const url =
    "https://push2his.eastmoney.com/api/qt/stock/kline/get" +
    `?secid=${cl.secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1&beg=20240101&end=20500101`;
  try {
    const resp = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)" },
    });
    if (!resp.ok) return null;
    const payload = (await resp.json()) as { data?: { klines?: string[] } };
    const klines = payload?.data?.klines ?? [];
    if (klines.length < 2) return null;
    const closes = klines.map((line) => Number(line.split(",")[2]));
    const price = closes[closes.length - 1];
    const prev = closes[closes.length - 2];
    return {
      code: cl.code,
      name: cl.name,
      price,
      pct_chg: prev ? (price / prev - 1) * 100 : 0,
      closes,
    };
  } catch (err) {
    console.warn(`[market_live] index kline 失败 ${cl.code}: ${err instanceof Error ? err.message : String(err)}`);
    return null;
  }
}

export interface Activity {
  up: number;
  down: number;
  flat: number;
  suspended: number;
  limit_up: number;
  limit_down: number;
  active_pct: number;
  asof: string;
}

export async function fetchActivity(): Promise<Activity | null> {
  try {
    const resp = await fetch("https://legulegu.com/stockdata/market-activity", {
      headers: {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        Referer: "https://legulegu.com/stockdata/market-activity",
      },
    });
    if (!resp.ok) return null;
    const html = await resp.text();
    const plain = html
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;|&#160;/g, " ")
      .replace(/\s+/g, " ");
    const m = plain.match(
      /活跃度\s*([0-9.]+)%\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})[\s\S]*?上涨\s*(\d+)\s*下跌\s*(\d+)\s*平盘\s*(\d+)\s*涨停\s*(\d+)\s*跌停\s*(\d+)\s*停牌\s*(\d+)/,
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
      suspended: Number(m[9]),
    };
  } catch {
    return null;
  }
}

export function computeSentiment(a: Activity): Record<string, unknown> {
  const total = Math.max(1, a.up + a.down);
  const breadth = a.up / total;
  const limitRatio = a.limit_up / Math.max(1, a.limit_up + a.limit_down);
  const activityScore = Math.min(1, a.active_pct / 15);
  const heatScore = limitRatio * Math.min(1, a.limit_up / 20);
  let score = Math.round(100 * (0.55 * breadth + 0.25 * heatScore + 0.2 * activityScore) * 10) / 10;
  score = Math.max(0, Math.min(100, score));
  let state = "neutral";
  let label = "中性";
  const states: Array<[number, string, string]> = [
    [20, "ice", "冰点"],
    [40, "cold", "偏冷"],
    [60, "neutral", "中性"],
    [80, "warm", "偏热"],
    [100, "hot", "过热"],
  ];
  for (const [threshold, st, lb] of states) {
    if (score <= threshold) {
      state = st;
      label = lb;
      break;
    }
  }
  let divergent = breadth < 0.45 && limitRatio > 0.7;
  if (state !== "ice" && breadth < 0.15 && a.limit_down > a.limit_up) {
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
    breadth: Math.round(breadth * 10000) / 10000,
    limit_ratio: Math.round(limitRatio * 10000) / 10000,
    active_pct: a.active_pct,
    breadth_score: Math.round(breadth * 1000) / 10,
    heat_score: Math.round(heatScore * 1000) / 10,
    divergent,
  };
}

export function indexDirection(closes: number[]): {
  direction: string;
  prob: { up: number; flat: number; down: number };
  confidence: number;
  ma_state: string;
} {
  const ma = (window: number): number | null => {
    if (closes.length < window) return null;
    const slice = closes.slice(-window);
    return slice.reduce((a, b) => a + b, 0) / slice.length;
  };
  const ma5 = ma(5);
  const ma20 = ma(20);
  const ma60 = ma(60);
  const ret5 = closes.length > 5 ? closes[closes.length - 1] / closes[closes.length - 6] - 1 : 0;
  let score = 0;
  if (ma5 !== null && ma20 !== null && ma60 !== null) {
    if (ma5 > ma20 && ma20 > ma60) score += 1;
    else if (ma5 < ma20 && ma20 < ma60) score -= 1;
  }
  if (ret5 > 0.01) score += 0.8;
  else if (ret5 < -0.01) score -= 0.8;
  const z = [score, 0, -score];
  const max = Math.max(...z);
  const exp = z.map((v) => Math.exp(v - max));
  const sum = exp.reduce((a, b) => a + b, 0);
  const prob = { up: exp[0] / sum, flat: exp[1] / sum, down: exp[2] / sum };
  const order = ["up", "flat", "down"];
  let idx = 0;
  let best = prob.up;
  if (prob.flat > best) {
    best = prob.flat;
    idx = 1;
  }
  if (prob.down > best) {
    best = prob.down;
    idx = 2;
  }
  const maState = score > 0.8 ? "多头排列" : score < -0.8 ? "空头排列" : "震荡";
  return {
    direction: order[idx],
    prob: {
      up: Math.round(prob.up * 10000) / 10000,
      flat: Math.round(prob.flat * 10000) / 10000,
      down: Math.round(prob.down * 10000) / 10000,
    },
    confidence: Math.round(best * 10000) / 10000,
    ma_state: maState,
  };
}

export function shanghaiNow(): Date {
  // 中国无夏令时，UTC+8 固定
  return new Date(Date.now() + 8 * 3600 * 1000);
}

export function isTradingSession(): boolean {
  const now = shanghaiNow();
  const day = now.getUTCDay(); // 0=周日
  if (day === 0 || day === 6) return false;
  const minutes = now.getUTCHours() * 60 + now.getUTCMinutes();
  return (minutes >= 9 * 60 + 30 && minutes <= 11 * 60 + 30) || (minutes >= 13 * 60 && minutes <= 15 * 60);
}

export function nowIsoShanghai(): string {
  const now = shanghaiNow();
  const pad = (v: number) => String(v).padStart(2, "0");
  return (
    `${now.getUTCFullYear()}-${pad(now.getUTCMonth() + 1)}-${pad(now.getUTCDate())}` +
    `T${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}:${pad(now.getUTCSeconds())}+08:00`
  );
}

/** Cron 处理：盘中刷新市场快照；非交易时段直接跳过（force=true 时忽略时段，用于手动刷新）。 */
export async function refreshMarketLive(env: Env, force = false): Promise<{ ok: boolean; reason?: string }> {
  if (!env.DB) return { ok: false, reason: "no_db" };
  if (!force && !isTradingSession() && (env.FORCE_LIVE_REFRESH ?? "") !== "1") {
    return { ok: false, reason: "off_session" };
  }

  // 东财接口并发会被限流：改为串行 + 250ms 节流
  const activity = await fetchActivity();
  const quotes: IndexQuote[] = [];
  for (const item of INDEXES) {
    let quote = await fetchIndex(item);
    if (!quote) {
      const fallback = await fetchTencentQuote(item.code);
      if (fallback) {
        quote = {
          code: item.code,
          name: item.name,
          price: fallback.price,
          pct_chg: (fallback.price / fallback.prevClose - 1) * 100,
          closes: [fallback.prevClose, fallback.price],
        };
      }
    }
    if (quote) quotes.push(quote);
    await new Promise((resolve) => setTimeout(resolve, 250));
  }

  const indices = quotes
    .map((quote) => {
      const d = indexDirection(quote.closes);
      return {
        code: quote.code,
        name: quote.name,
        price: Math.round(quote.price * 100) / 100,
        pct_chg: Math.round(quote.pct_chg * 100) / 100,
        direction: d.direction,
        prob: d.prob,
        confidence: d.confidence,
        ma_state: d.ma_state,
      };
    });

  if (!activity && indices.length === 0) return { ok: false, reason: "no_data" };

  const sentiment = activity
    ? computeSentiment(activity)
    : { score: 50, state: "neutral", label: "中性", breadth: 0.5, limit_ratio: 0.5, active_pct: 0 };
  const avg = indices.length
    ? {
        up: indices.reduce((sum, item) => sum + item.prob.up, 0) / indices.length,
        flat: indices.reduce((sum, item) => sum + item.prob.flat, 0) / indices.length,
        down: indices.reduce((sum, item) => sum + item.prob.down, 0) / indices.length,
      }
    : { up: 0.33, flat: 0.34, down: 0.33 };
  const order = ["up", "flat", "down"] as const;
  const values = [avg.up, avg.flat, avg.down];
  let bestIdx = 0;
  values.forEach((value, i) => {
    if (value > values[bestIdx]) bestIdx = i;
  });
  const marketDirection = {
    direction: order[bestIdx],
    prob: {
      up: Math.round(avg.up * 10000) / 10000,
      flat: Math.round(avg.flat * 10000) / 10000,
      down: Math.round(avg.down * 10000) / 10000,
    },
    confidence: Math.round(values[bestIdx] * 10000) / 10000,
  };

  const tradeDate = activity?.asof?.slice(0, 10) ?? nowIsoShanghai().slice(0, 10);
  const snapshot = {
    schema: 1,
    asof: nowIsoShanghai(),
    trade_date: tradeDate,
    status: activity && indices.length ? "ok" : "partial",
    live: true,
    errors: [],
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
    market_direction: marketDirection,
  };

  await insertMarketSnapshot(env.DB, snapshot);
  return { ok: true };
}
