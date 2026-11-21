/**
 * 信号 JSON 校验 / 归一化 + 推送鉴权工具。
 *
 * 输入 = 契约 §4 的信号 JSON（POST /api/ingest 的单个对象、数组或 {items:[...]}）。
 * 输出 = 可直接 UPSERT 进 D1 signals 表的一行（契约 §3.3）。
 *
 * 设计原则：
 *   - 严格校验会破坏契约不变量的字段（symbol / prob 和 / direction 与 argmax 一致 …）；
 *   - 对可推导/可归一化的字段（trade_date、expires_at、confidence、top_features 排序与截断、
 *     asof 时区）自动补全并返回 warnings，避免本地 Python 侧因为小瑕疵整批失败；
 *   - 绝不静默吞错：errors 非空则该条拒绝，并在响应里返回原因。
 */
import type { Direction, SignalRow, SignalSource, SignalStatus, TopFeature } from "./types.ts";

export const MAX_TOP_FEATURES = 5;
export const MAX_INGEST_BYTES = 1_000_000; // 1 MB / 次推送

const SYMBOL_RE = /^\d{6}$/;
const TRADE_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const SIGNAL_ID_RE = /^[A-Za-z0-9._:-]{1,120}$/;
const DIRECTIONS: readonly Direction[] = ["up", "flat", "down"];
const STATUSES: readonly SignalStatus[] = ["ok", "stale", "warming_up"];
const SOURCES: readonly SignalSource[] = ["local", "cloud"];

/** 当前时间格式化为 Asia/Shanghai（+08:00，无夏令时）ISO 字符串，精确到秒。 */
export function nowShanghai(now: Date = new Date()): string {
  const shifted = new Date(now.getTime() + 8 * 3600 * 1000);
  return `${shifted.toISOString().slice(0, 19)}+08:00`;
}

/** 任意合法 ISO 时间 -> 统一成 +08:00、精确到秒的 ISO；非法返回 null。 */
export function toShanghaiIso(value: unknown): string | null {
  if (typeof value !== "string" || value.trim() === "") return null;
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) return null;
  return nowShanghai(new Date(ms));
}

/** signal_id 里的 asof_compact：YYYYMMDDHHMMSS（UTC+8 墙钟）。 */
export function asofCompact(asofShanghai: string): string {
  return asofShanghai.slice(0, 19).replace(/[-:T]/g, "");
}

function round4(x: number): number {
  return Math.round(x * 1e4) / 1e4;
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number(value);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

function deriveExpiresAt(tradeDate: string): string {
  const base = new Date(`${tradeDate}T00:00:00+08:00`);
  if (!Number.isFinite(base.getTime())) return `${tradeDate}T15:00:00+08:00`;
  const next = new Date(base.getTime() + 24 * 3600 * 1000);
  const shifted = new Date(next.getTime() + 8 * 3600 * 1000);
  return `${shifted.toISOString().slice(0, 10)}T09:30:00+08:00`;
}

function normalizeTopFeatures(raw: unknown, warnings: string[]): TopFeature[] | null {
  if (raw === undefined || raw === null) return [];
  if (!Array.isArray(raw)) {
    warnings.push("top_features 不是数组，已按空数组处理");
    return [];
  }
  const cleaned: TopFeature[] = [];
  raw.forEach((item, idx) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      warnings.push(`top_features[${idx}] 不是对象，已跳过`);
      return;
    }
    const obj = item as Record<string, unknown>;
    const name = typeof obj.name === "string" ? obj.name.trim() : "";
    const value = asNumber(obj.value);
    const contrib = asNumber(obj.contrib);
    if (!name || value === null || contrib === null) {
      warnings.push(`top_features[${idx}] 缺少 name/value/contrib，已跳过`);
      return;
    }
    cleaned.push({ name, value, contrib });
  });
  cleaned.sort((a, b) => Math.abs(b.contrib) - Math.abs(a.contrib));
  if (cleaned.length > MAX_TOP_FEATURES) {
    warnings.push(`top_features 超过 ${MAX_TOP_FEATURES} 条，已按 |contrib| 降序截断`);
  }
  return cleaned.slice(0, MAX_TOP_FEATURES);
}

export interface NormalizeResult {
  row: SignalRow | null;
  warnings: string[];
  errors: string[];
}

/** 校验并归一化一条 §4 信号。 */
export function normalizeSignal(input: unknown, nowIso: string = nowShanghai()): NormalizeResult {
  const warnings: string[] = [];
  const errors: string[] = [];

  if (!input || typeof input !== "object" || Array.isArray(input)) {
    return { row: null, warnings, errors: ["信号必须是 JSON 对象"] };
  }
  const src = input as Record<string, unknown>;

  const symbol = typeof src.symbol === "string" ? src.symbol.trim() : "";
  if (!SYMBOL_RE.test(symbol)) {
    errors.push(`symbol 必须是 6 位数字 A 股代码，收到 ${JSON.stringify(src.symbol)}`);
  }

  const asof = toShanghaiIso(src.asof);
  if (!asof) errors.push(`asof 必须是带时区的 ISO 时间，收到 ${JSON.stringify(src.asof)}`);

  let tradeDate = typeof src.trade_date === "string" ? src.trade_date.trim() : "";
  if (!tradeDate && asof) {
    tradeDate = asof.slice(0, 10);
    warnings.push("trade_date 缺失，已从 asof 推导");
  }
  if (!TRADE_DATE_RE.test(tradeDate) || !Number.isFinite(Date.parse(`${tradeDate}T00:00:00+08:00`))) {
    errors.push(`trade_date 必须是 YYYY-MM-DD，收到 ${JSON.stringify(src.trade_date)}`);
  }

  const price = asNumber(src.price);
  if (price === null || price <= 0) errors.push(`price 必须是正数，收到 ${JSON.stringify(src.price)}`);

  let prob: { up: number; flat: number; down: number } | null = null;
  const probRaw = src.prob ?? src.probabilities;
  if (probRaw && typeof probRaw === "object" && !Array.isArray(probRaw)) {
    const p = probRaw as Record<string, unknown>;
    const up = asNumber(p.up);
    const flat = asNumber(p.flat);
    const down = asNumber(p.down);
    if (up === null || flat === null || down === null) {
      errors.push("prob 必须包含数值字段 up/flat/down");
    } else if ([up, flat, down].some((v) => v < 0 || v > 1)) {
      errors.push(`prob 必须在 [0,1] 内，收到 up=${up} flat=${flat} down=${down}`);
    } else {
      const sum = up + flat + down;
      if (Math.abs(sum - 1) <= 1e-6) {
        prob = { up, flat, down };
      } else if (Math.abs(sum - 1) <= 1e-3) {
        const norm = { up: up / sum, flat: flat / sum, down: down / sum };
        prob = norm;
        warnings.push(`prob 和=${sum}，超出 §3.3 的 ±1e-6，已重新归一化`);
      } else {
        errors.push(`prob 和必须为 1（±1e-6），收到 ${sum}`);
      }
    }
  } else {
    errors.push("prob 缺失（需要 {up,flat,down}）");
  }

  const directionRaw = typeof src.direction === "string" ? src.direction.trim() : "";
  let direction: Direction | null = null;
  if (!DIRECTIONS.includes(directionRaw as Direction)) {
    errors.push(`direction 必须是 up/flat/down，收到 ${JSON.stringify(src.direction)}`);
  } else {
    direction = directionRaw as Direction;
    if (prob) {
      const maxProb = Math.max(prob.up, prob.flat, prob.down);
      if (prob[direction] < maxProb - 1e-9) {
        errors.push(`direction=${direction} 与 prob 最大类不一致（argmax=${argmax(prob)}）`);
      }
    }
  }

  // confidence 以 max(prob) 为权威值（§3.3），保留 4 位；与推送值不一致时 warning。
  let confidence = 0;
  if (prob) {
    confidence = round4(Math.max(prob.up, prob.flat, prob.down));
    const provided = asNumber(src.confidence);
    if (provided !== null && Math.abs(provided - confidence) > 1e-4) {
      warnings.push(`confidence=${provided} 与 max(prob)=${confidence} 不一致，已按 §3.3 取 max(prob)`);
    }
  }

  const horizon = typeof src.horizon === "string" && src.horizon.trim() ? src.horizon.trim() : "1d";
  if (src.horizon === undefined) warnings.push("horizon 缺失，默认 1d");

  let expiresAt = toShanghaiIso(src.expires_at);
  if (!expiresAt) {
    if (tradeDate) {
      expiresAt = deriveExpiresAt(tradeDate);
      warnings.push("expires_at 缺失或非法，已按 trade_date 次日 09:30(+08:00) 推导");
    } else {
      errors.push(`expires_at 必须是带时区的 ISO 时间，收到 ${JSON.stringify(src.expires_at)}`);
    }
  }

  const modelVersion = typeof src.model_version === "string" ? src.model_version.trim() : "";
  if (!modelVersion) errors.push("model_version 必须是非空字符串");

  const statusRaw = src.status === undefined ? "ok" : typeof src.status === "string" ? src.status.trim() : "";
  if (!STATUSES.includes(statusRaw as SignalStatus)) {
    errors.push(`status 必须是 ok/stale/warming_up，收到 ${JSON.stringify(src.status)}`);
  }

  const topFeatures = normalizeTopFeatures(src.top_features, warnings);

  const sourceRaw = src.source === undefined ? "local" : typeof src.source === "string" ? src.source.trim() : "";
  if (!SOURCES.includes(sourceRaw as SignalSource)) {
    errors.push(`source 必须是 local/cloud，收到 ${JSON.stringify(src.source)}`);
  }

  const createdAt = toShanghaiIso(src.created_at) ?? nowIso;

  let signalId = typeof src.signal_id === "string" ? src.signal_id.trim() : "";
  if (!signalId) {
    if (symbol && tradeDate && asof) {
      signalId = `${symbol}-${tradeDate}-${asofCompact(asof)}`;
      warnings.push(`signal_id 缺失，已按 §3.3 规则推导：${signalId}`);
    } else {
      errors.push("signal_id 缺失且无法推导（需要 symbol + trade_date + asof）");
    }
  } else if (!SIGNAL_ID_RE.test(signalId)) {
    errors.push(`signal_id 只允许字母/数字/._:- 且 <=120 字符，收到 ${JSON.stringify(signalId)}`);
  }

  if (errors.length > 0 || !prob || !direction || !asof || price === null || topFeatures === null || expiresAt === null) {
    return { row: null, warnings, errors };
  }

  return {
    row: {
      signal_id: signalId,
      symbol,
      asof,
      trade_date: tradeDate,
      price,
      direction,
      prob_up: prob.up,
      prob_flat: prob.flat,
      prob_down: prob.down,
      confidence,
      horizon,
      expires_at: expiresAt,
      model_version: modelVersion,
      top_features: JSON.stringify(topFeatures),
      source: sourceRaw as SignalSource,
      status: statusRaw as SignalStatus,
      created_at: createdAt,
    },
    warnings,
    errors: [],
  };
}

function argmax(prob: { up: number; flat: number; down: number }): Direction {
  let best: Direction = "flat";
  let bestP = -Infinity;
  for (const k of DIRECTIONS) {
    if (prob[k] > bestP) {
      bestP = prob[k];
      best = k;
    }
  }
  return best;
}

/** 解析 POST /api/ingest 请求体：单个 §4 信号 / 数组 / {items:[...]} 都接受。 */
export function parseIngestBody(body: unknown): { signals: unknown[]; error?: string } {
  if (Array.isArray(body)) return { signals: body };
  if (body && typeof body === "object") {
    const obj = body as Record<string, unknown>;
    const list = obj.items ?? obj.signals ?? obj.data;
    if (Array.isArray(list)) return { signals: list };
    if ("symbol" in obj) return { signals: [obj] };
    return { signals: [], error: "无法识别的请求体：需要单个信号对象、信号数组或 {items:[...]}" };
  }
  return { signals: [], error: "请求体必须是 JSON 对象或数组" };
}

/** 常量时间字符串比较（避免 token 逐字符短路泄漏）。 */
export function timingSafeEqual(a: string, b: string): boolean {
  const enc = new TextEncoder();
  const ab = enc.encode(a);
  const bb = enc.encode(b);
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i += 1) diff |= ab[i] ^ bb[i];
  return diff === 0;
}

/** 从请求头取推送 token：Authorization: Bearer xxx 或 X-Ingest-Token: xxx。 */
export function extractIngestToken(request: Request): string {
  const auth = request.headers.get("authorization") ?? "";
  const m = /^Bearer\s+(.+)$/i.exec(auth.trim());
  if (m) return m[1].trim();
  return (request.headers.get("x-ingest-token") ?? "").trim();
}
