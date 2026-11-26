/**
 * AI 代理通道（百炼 OpenAI 兼容接口：qwen4-max / kimi-k3.1-pro）。
 *
 * 用途（模块一/五的 AI 能力）：
 * - 事件利好利空语义分析（替代/补充关键词规则）；
 * - 每日收评 AI 文案（基于真实快照数据，不编造）；
 * - 结果写 Cache API（24h）+ D1 edge_cache 兜底（24h）：Cloudflare 限制 *.workers.dev 上 Cache API no impact，
 *   仅靠 Cache API 会出现「POST generate 成功但 GET insight 404」，故 D1 二级存储保证 workers.dev 可用。
 *
 * 触发：POST /api/ai/generate（x-ingest-token 保护），由 Worker Cron 或本地脚本调用。
 */
import { queryEvents, queryLatestJsonSnapshot, queryLatestMarket } from "./db.ts";
import { nowIsoShanghai } from "./market_live.ts";
import type { Env } from "./types.ts";

const BAILIAN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions";
const AI_CACHE_KEY = "https://quant-edge-cache.local/api/ai/insight";
const AI_D1_KEY = "ai:insight";
const AI_CACHE_TTL = 86400;
const AI_D1_TTL_SEC = 86400;

export interface AiInsight {
  schema: 1;
  generated_at: string;
  model: string;
  event_analysis: Record<string, unknown>;
  summary: string;
}

export async function callBailian(
  env: Env,
  prompt: string,
  model = "qwen4-max",
  maxTokens = 1200,
  temperature = 0.3,
): Promise<string> {
  const key = env.DASHSCOPE_API_KEY ?? "";
  if (!key) throw new Error("DASHSCOPE_API_KEY 未配置");
  const resp = await fetch(BAILIAN_URL, {
    method: "POST",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
    body: JSON.stringify({
      model,
      messages: [{ role: "user", content: prompt }],
      max_tokens: maxTokens,
      temperature,
    }),
    signal: AbortSignal.timeout(90000),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`百炼 ${resp.status}: ${text.slice(0, 200)}`);
  }
  const payload = (await resp.json()) as { choices?: Array<{ message?: { content?: string } }> };
  return String(payload.choices?.[0]?.message?.content ?? "").trim();
}

function parseLooseJson(text: string): Record<string, unknown> {
  let cleaned = text.trim();
  if (cleaned.startsWith("```")) {
    cleaned = cleaned.replace(/^```[a-zA-Z]*\s*/, "").replace(/```\s*$/, "").trim();
  }
  const start = cleaned.indexOf("{");
  const end = cleaned.lastIndexOf("}");
  if (start >= 0 && end > start) {
    try {
      return JSON.parse(cleaned.slice(start, end + 1)) as Record<string, unknown>;
    } catch {
      // 截断的 JSON：尝试补全常见的未闭合结构后重试一次
      try {
        return JSON.parse(cleaned.slice(start, end + 1) + '"]}') as Record<string, unknown>;
      } catch {
        /* ignore */
      }
    }
  }
  return { raw: text };
}

/** D1 二级缓存（workers.dev 上 Cache API no-op 的兜底）。 */
async function readAiInsightDb(env: Env): Promise<AiInsight | null> {
  if (!env.DB) return null;
  try {
    const row = await env.DB
      .prepare(`SELECT payload FROM edge_cache WHERE cache_key = ? AND expires_at > ? LIMIT 1`)
      .bind(AI_D1_KEY, Date.now())
      .first<{ payload: string }>();
    if (!row) return null;
    return JSON.parse(row.payload) as AiInsight;
  } catch {
    return null;
  }
}

async function writeAiInsightDb(env: Env, insight: AiInsight): Promise<void> {
  if (!env.DB) return;
  try {
    const now = Date.now();
    await env.DB
      .prepare(
        `INSERT INTO edge_cache (cache_key, payload, cached_at, expires_at) VALUES (?, ?, ?, ?)
         ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload,
           cached_at = excluded.cached_at, expires_at = excluded.expires_at`,
      )
      .bind(AI_D1_KEY, JSON.stringify(insight), now, now + AI_D1_TTL_SEC * 1000)
      .run();
  } catch {
    // 缓存失败不影响返回
  }
}

/** 生成 AI 洞察（事件分析 + 收评），写缓存。 */
export async function generateAiInsight(env: Env, force = false): Promise<AiInsight> {
  if (!env.DB) throw new Error("D1 未绑定");
  if (!force) {
    const cached = await getAiInsight(env);
    if (cached) return cached;
  }
  const events = await queryEvents(env.DB, 12);
  const market = await queryLatestMarket(env.DB);
  const regime = await queryLatestJsonSnapshot(env.DB, "regime_snapshots");
  const snapshot = (market?.snapshot ?? {}) as Record<string, unknown>;
  const sentiment = (snapshot["sentiment"] ?? {}) as Record<string, unknown>;
  const activity = (snapshot["activity"] ?? {}) as Record<string, unknown>;
  const indices = Array.isArray(snapshot["indices"]) ? (snapshot["indices"] as Array<Record<string, unknown>>) : [];

  const eventList = events.map((item) => ({
    title: String(item["title"] ?? "").slice(0, 80),
    rule: String(item["label"] ?? "中性"),
  }));
  const eventPrompt =
    "你是 A 股事件驱动分析助手。对以下今日事件做语义分析。" +
    "只输出 JSON（不要 markdown）：" +
    '{"bullish":[{"title":"...","reason":"一句话","sectors":["板块"]}],' +
    '"bearish":[{"title":"...","reason":"一句话","sectors":["板块"]}],' +
    '"summary":"60字内整体事件情绪总结","watch":["明日关注点1","明日关注点2"]}。' +
    "要求：基于事件本身判断，不夸大；不确定归中性；不构成投资建议。事件：" +
    JSON.stringify(eventList);
  let eventAnalysis: Record<string, unknown> = {};
  try {
    eventAnalysis = parseLooseJson(await callBailian(env, eventPrompt, "qwen4-max", 1400));
  } catch (err) {
    eventAnalysis = { error: err instanceof Error ? err.message : String(err) };
  }

  const idxText = indices
    .slice(0, 4)
    .map((i) => `${String(i["name"] ?? "")} ${String(i["price"] ?? "-")}（${Number(i["pct_chg"] ?? 0).toFixed(2)}%）`)
    .join("；");
  const em = regime?.emotion && typeof regime.emotion === "object" ? (regime.emotion as Record<string, unknown>) : {};
  const state = regime?.market_state && typeof regime.market_state === "object" ? (regime.market_state as Record<string, unknown>) : {};
  const summaryPrompt =
    "你是 A 股盘后复盘分析师。基于以下数据写一段收评（180-260 字），要求：" +
    "1)先说指数与量能；2)说涨跌家数/涨停跌停/情绪（只用给的数据）；3)说主线与资金方向；" +
    "4)给一句明确判断（进攻/防守/分化/退潮）与明日关注点；5)专业简洁，不模棱两可；6)不写免责声明。" +
    `\n指数：${idxText}\n涨跌家数：涨 ${String(activity["up"] ?? "-")} / 跌 ${String(activity["down"] ?? "-")}，` +
    `涨停 ${String(activity["limit_up"] ?? "-")} / 跌停 ${String(activity["limit_down"] ?? "-")}\n` +
    `情绪：${String(sentiment["label"] ?? "-")}（${String(sentiment["score"] ?? "-")}）` +
    `赚钱效应 ${String(sentiment["breadth_score"] ?? "-")}% · 投机热度 ${String(sentiment["heat_score"] ?? "-")}%\n` +
    `大盘状态：${JSON.stringify(state).slice(0, 300)}\n主线：${JSON.stringify(regime?.leaders ?? []).slice(0, 300)}`;
  let summary = "";
  try {
    summary = await callBailian(env, summaryPrompt, "qwen4-max", 800);
  } catch (err) {
    summary = `AI 收评生成失败（${err instanceof Error ? err.message : String(err)}）`;
  }

  const insight: AiInsight = {
    schema: 1,
    generated_at: nowIsoShanghai(),
    model: "qwen4-max",
    event_analysis: eventAnalysis,
    summary,
  };
  await Promise.all([
    caches.default
      .put(
        AI_CACHE_KEY,
        new Response(JSON.stringify(insight), {
          headers: { "content-type": "application/json; charset=utf-8", "cache-control": "public, max-age=86400" },
        }),
      )
      .catch(() => undefined),
    writeAiInsightDb(env, insight),
  ]);
  return insight;
}

export async function getAiInsight(env?: Env): Promise<AiInsight | null> {
  try {
    const cached = await caches.default.match(AI_CACHE_KEY);
    if (cached) return (await cached.json()) as AiInsight;
  } catch {
    // workers.dev 上 Cache API no-op，继续走 D1 兜底
  }
  return env ? readAiInsightDb(env) : null;
}

export const AI_CACHE_TTL_SEC = AI_CACHE_TTL;


/** 外部（本地/CI）推送 AI 报告并写入缓存（token 保护，见 /api/ai/ingest）。 */
export async function ingestAiInsight(insight: AiInsight, env?: Env): Promise<void> {
  const payload = {
    schema: 1,
    generated_at: insight.generated_at || nowIsoShanghai(),
    model: insight.model || "qwen4-max",
    event_analysis: insight.event_analysis ?? {},
    summary: insight.summary ?? "",
  } satisfies AiInsight;
  await caches.default.put(
    AI_CACHE_KEY,
    new Response(JSON.stringify(payload), {
      headers: { "content-type": "application/json; charset=utf-8", "cache-control": "public, max-age=86400" },
    }),
  );
  if (env) await writeAiInsightDb(env, payload);
}
