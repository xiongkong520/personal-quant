/** 共享类型：Cloudflare Worker 侧（契约 §3.3 / §4）。 */

export interface Env {
  /** D1 binding，见 wrangler.toml [[d1_databases]] */
  DB: D1Database;
  /** Pages 静态产物（Assets binding），见 wrangler.toml [assets] */
  ASSETS?: Fetcher;
  /** 推送鉴权 token，只能来自环境变量 / wrangler secret / .dev.vars，绝不写进代码 */
  QUANT_INGEST_TOKEN?: string;
  /** 兼容旧名（优先级低于 QUANT_INGEST_TOKEN） */
  INGEST_TOKEN?: string;
  /** 可选：覆盖 worker 版本号（默认 0.1.0） */
  WORKER_VERSION?: string;
  /** 限流（次/分钟/客户端 IP），默认 120；测试可调小/调大 */
  RATE_LIMIT_PER_MIN?: string;
  /** 测试用：设为 "1" 时忽略交易时段限制强制刷新（正式环境不要设置） */
  FORCE_LIVE_REFRESH?: string;
  /** 收盘同步时批量缓存 K 线的股票数（默认 6；0=跳过，全部按需实时拉取） */
  KLINE_SYNC_LIMIT?: string;
  /** 百炼 DashScope API key（AI 代理通道） */
  DASHSCOPE_API_KEY?: string;
  /** 可信 IP 白名单（逗号分隔；支持精确 IP 与 a.b.c.* 前缀通配）：
   *  这些 IP 免人机验证、免限流，用于开发测试与自动化验收 */
  TRUSTED_IPS?: string;
  /** 可选：跨 isolate 限流用 KV namespace；未绑定时用内存 Map */
  RATE_LIMIT_KV?: KVNamespace;
  /** Turnstile：sitekey 是公开值（可写 wrangler.toml [vars]），secret 必须用 wrangler secret 保存 */
  TURNSTILE_SITEKEY?: string;
  TURNSTILE_SECRET?: string;
  /** 测试/私有部署用：覆盖 siteverify 地址（本地 mock 用；生产不要设置） */
  TURNSTILE_VERIFY_URL?: string;
  /** HMAC 签名 cookie 的密钥（wrangler secret put COOKIE_KEY），未配置则 /api/verify 返回 503 */
  COOKIE_KEY?: string;
}

export type Direction = "up" | "flat" | "down";
export type SignalStatus = "ok" | "stale" | "warming_up";
export type SignalSource = "local" | "cloud";

export interface TopFeature {
  name: string;
  value: number;
  contrib: number;
}

export interface Proba {
  up: number;
  flat: number;
  down: number;
}

/** D1 signals 表一行（契约 §3.3，少 paper_* 内部字段）。 */
export interface SignalRow {
  signal_id: string;
  symbol: string;
  asof: string;
  trade_date: string;
  price: number;
  direction: Direction;
  prob_up: number;
  prob_flat: number;
  prob_down: number;
  confidence: number;
  horizon: string;
  expires_at: string;
  model_version: string;
  top_features: string;
  source: SignalSource;
  status: SignalStatus;
  created_at: string;
}

/** 对外的 §4 信号 JSON（GET /api/signals 的 items 元素）。 */
export interface SignalItem {
  signal_id: string;
  symbol: string;
  /** 展示名不落库（§3.3 无 name 列），前端用 symbol 映射；推送方传了 name 也不会被持久化。 */
  asof: string;
  trade_date: string;
  price: number;
  direction: Direction;
  prob: Proba;
  confidence: number;
  horizon: string;
  expires_at: string;
  model_version: string;
  status: SignalStatus;
  top_features: TopFeature[];
  source: SignalSource;
  created_at: string;
}
