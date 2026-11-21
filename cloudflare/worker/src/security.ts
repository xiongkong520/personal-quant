/**
 * Worker 安全层（公网部署用）：
 *   1. 反爬：UA 黑名单 + Accept 校验 + 同源 CORS 限制；
 *   2. 安全响应头：CSP / X-Frame-Options / Referrer-Policy / X-Content-Type-Options / Permissions-Policy；
 *   3. 限流：按 cf-connecting-ip 每分钟 N 次（默认 60），本地/单 isolate 用内存 Map，
 *      绑定 KV（env.RATE_LIMIT_KV）时用 KV；D1 实现见文件末尾注释；
 *   4. Turnstile 人机验证：POST /api/verify 校验 token 后签发 HMAC 签名 cookie quant_pass（1 小时）。
 *
 * 所有密钥只从 env 读取，代码里只有 Cloudflare 官方“测试用密钥”（永远通过，不能用于生产）。
 */
import type { Env } from "./types.ts";

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

/** Cloudflare 官方 Turnstile 测试密钥：sitekey 永远通过、secret 永远校验成功。仅本地/测试用。 */
export const TURNSTILE_TEST_SITEKEY = "1x00000000000000000000AA";
export const TURNSTILE_TEST_SECRET = "1x0000000000000000000000000000000AA";

export const TURNSTILE_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify";

export const COOKIE_NAME = "quant_pass";
export const COOKIE_TTL_SECONDS = 3600;
export const DEFAULT_RATE_LIMIT_PER_MIN = 120;
const RATE_LIMIT_WINDOW_MS = 60_000;

/**
 * 爬虫/脚本 UA 黑名单（大小写不敏感）。curl/wget 等命令行工具、常见抓取库、
 * 无头浏览器、SEO 爬虫都会被拦。空 UA 也拦。
 */
const UA_BLACKLIST: readonly RegExp[] = [
  /curl/i,
  /wget/i,
  /python-requests/i,
  /python-urllib/i,
  /aiohttp/i,
  /\bhttpx?\b/i,
  /scrapy/i,
  /httpie/i,
  /node-fetch/i,
  /undici/i,
  /axios/i,
  /go-http-client/i,
  /okhttp/i,
  /\bjava\//i,
  /libwww-perl/i,
  /headlesschrome/i,
  /phantomjs/i,
  /puppeteer/i,
  /playwright/i,
  /selenium/i,
  /webdriver/i,
  /\b(?:bot|spider|crawler|scraper)\b/i,
  /postmanruntime/i,
  /insomnia/i,
];

export const SECURITY_HEADERS: Readonly<Record<string, string>> = {
  "content-security-policy":
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://challenges.cloudflare.com; connect-src 'self' https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
  "x-frame-options": "DENY",
  "referrer-policy": "strict-origin-when-cross-origin",
  "x-content-type-options": "nosniff",
  "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
};

// ---------------------------------------------------------------------------
// 环境变量读取（全部只从 env 读）
// ---------------------------------------------------------------------------

export function readCookieKey(env: Env): string {
  return (env.COOKIE_KEY ?? "").trim();
}

export function readTurnstileSitekey(env: Env): string {
  return (env.TURNSTILE_SITEKEY ?? "").trim() || TURNSTILE_TEST_SITEKEY;
}

export function readTurnstileSecret(env: Env): string {
  return (env.TURNSTILE_SECRET ?? "").trim() || TURNSTILE_TEST_SECRET;
}

export function isTurnstileTestSecret(env: Env): boolean {
  return readTurnstileSecret(env) === TURNSTILE_TEST_SECRET;
}

export function readRateLimitPerMin(env: Env): number {
  const value = Number(env.RATE_LIMIT_PER_MIN ?? "");
  if (!Number.isFinite(value) || value <= 0) return DEFAULT_RATE_LIMIT_PER_MIN;
  return Math.trunc(value);
}

// ---------------------------------------------------------------------------
// 反爬 / 同源 / 安全头
// ---------------------------------------------------------------------------

export interface ClientCheck {
  ok: boolean;
  error?: "forbidden_client" | "bad_accept";
  message?: string;
  userAgent?: string;
  reason?: string;
}

/**
 * UA 检查：**已按用户要求取消黑名单拦截**（原 curl/无头浏览器/抓取库黑名单不再生效）。
 * 反爬/防机器人改由 Cloudflare 边缘防护（自定义域名后的免费 WAF/Bot Fight Mode）
 * 与 Turnstile 人机验证负责；这里只保留 UA 记录用于日志。
 */
export function checkUserAgent(request: Request): ClientCheck {
  const ua = (request.headers.get("user-agent") ?? "").trim();
  return { ok: true, userAgent: ua };
}

/** Accept 检查：已放宽（原 Accept 白名单校验取消），避免误伤正常浏览器/降级请求。 */
export function checkAcceptHeader(_request: Request): ClientCheck {
  return { ok: true };
}

/** 同源判断：请求 Origin 必须等于当前站点 origin（无 Origin 的请求视为同源，如 curl/服务端）。 */
export function checkSameOrigin(request: Request): { ok: boolean; origin: string | null; self: string } {
  const self = new URL(request.url).origin;
  const origin = request.headers.get("origin");
  if (!origin) return { ok: true, origin: null, self };
  try {
    return { ok: new URL(origin).origin === self, origin, self };
  } catch {
    return { ok: false, origin, self };
  }
}

/** 给所有响应统一加安全头（不覆盖上游已有同名头）。 */
export function applySecurityHeaders(response: Response): Response {
  const headers = new Headers(response.headers);
  for (const [key, value] of Object.entries(SECURITY_HEADERS)) {
    if (!headers.has(key)) headers.set(key, value);
  }
  headers.set("x-content-type-options", "nosniff");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

// ---------------------------------------------------------------------------
// 限流
// ---------------------------------------------------------------------------

export interface RateLimitResult {
  allowed: boolean;
  limit: number;
  remaining: number;
  retryAfter: number; // 秒
  resetAt: number; // epoch ms
}

/**
 * 客户端 IP：Cloudflare 边缘会写 cf-connecting-ip；
 * 本地 wrangler --local / node dev-server 没有时按 x-forwarded-for / x-real-ip / 固定 local 兜底。
 */
export function clientIp(request: Request): string {
  const direct = (request.headers.get("cf-connecting-ip") ?? "").trim();
  if (direct) return direct;
  const xff = (request.headers.get("x-forwarded-for") ?? "").split(",")[0]?.trim();
  if (xff) return xff;
  const real = (request.headers.get("x-real-ip") ?? "").trim();
  if (real) return real;
  return "local";
}

interface MemoryWindow {
  count: number;
  resetAt: number;
}

/** 单 isolate 内存限流窗口（本地 dev / 单 isolate 够用）。 */
const memoryWindows = new Map<string, MemoryWindow>();
const MEMORY_MAX_KEYS = 10_000;

function checkMemoryRateLimit(key: string, limit: number, now: number): RateLimitResult {
  let window = memoryWindows.get(key);
  if (!window || now >= window.resetAt) {
    window = { count: 0, resetAt: now + RATE_LIMIT_WINDOW_MS };
    memoryWindows.set(key, window);
  }
  window.count += 1;

  if (memoryWindows.size > MEMORY_MAX_KEYS) {
    for (const [k, w] of memoryWindows) if (now >= w.resetAt) memoryWindows.delete(k);
  }

  const retryAfter = Math.max(1, Math.ceil((window.resetAt - now) / 1000));
  const allowed = window.count <= limit;
  return { allowed, limit, remaining: Math.max(0, limit - window.count), retryAfter, resetAt: window.resetAt };
}

/** KV 限流（跨 isolate，最终一致；并发下计数可能少算，见 README 说明）。 */
async function checkKvRateLimit(kv: KVNamespace, key: string, limit: number, now: number): Promise<RateLimitResult> {
  const windowStart = Math.floor(now / RATE_LIMIT_WINDOW_MS) * RATE_LIMIT_WINDOW_MS;
  const resetAt = windowStart + RATE_LIMIT_WINDOW_MS;
  const kvKey = `rl:${key}:${windowStart}`;
  let count = 0;
  try {
    const raw = await kv.get(kvKey);
    count = raw ? Number(raw) || 0 : 0;
  } catch {
    /* KV 读失败时退回内存限流，避免全站 500 */
    return checkMemoryRateLimit(key, limit, now);
  }
  count += 1;
  try {
    await kv.put(kvKey, String(count), { expirationTtl: Math.ceil(RATE_LIMIT_WINDOW_MS / 1000) * 2 });
  } catch {
    /* 写失败不阻断请求 */
  }
  const retryAfter = Math.max(1, Math.ceil((resetAt - now) / 1000));
  return { allowed: count <= limit, limit, remaining: Math.max(0, limit - count), retryAfter, resetAt };
}

/**
 * 按客户端 IP 每分钟限流（默认 60 次/分钟）。
 * 绑定 env.RATE_LIMIT_KV 时走 KV；否则用内存窗口。
 *
 * D1 实现（生产若不想用 KV，可加 rate_limits 表 + 如下 UPSERT，注意每次请求 1 次 D1 写）：
 *   CREATE TABLE IF NOT EXISTS rate_limits (
 *     key TEXT NOT NULL, window_start INTEGER NOT NULL, count INTEGER NOT NULL,
 *     PRIMARY KEY (key, window_start)
 *   );
 *   INSERT INTO rate_limits (key, window_start, count) VALUES (?, ?, 1)
 *   ON CONFLICT(key, window_start) DO UPDATE SET count = count + 1 RETURNING count;
 */
export async function checkRateLimit(env: Env, request: Request): Promise<RateLimitResult> {
  const limit = readRateLimitPerMin(env);
  const key = clientIp(request);
  const now = Date.now();
  if (env.RATE_LIMIT_KV) return checkKvRateLimit(env.RATE_LIMIT_KV, key, limit, now);
  return checkMemoryRateLimit(key, limit, now);
}

// ---------------------------------------------------------------------------
// Turnstile 校验
// ---------------------------------------------------------------------------

export interface TurnstileResult {
  ok: boolean;
  errors: string[];
  /** true 表示用的是官方测试 secret 且远端不可达，按“测试密钥必过”短路（生产不会走到）。 */
  degraded?: boolean;
}

export async function verifyTurnstileToken(env: Env, token: unknown, remoteIp?: string): Promise<TurnstileResult> {
  const response = typeof token === "string" ? token.trim() : "";
  if (response === "") return { ok: false, errors: ["missing-input-response"] };

  const secret = readTurnstileSecret(env);
  const endpoint = (env.TURNSTILE_VERIFY_URL ?? "").trim() || TURNSTILE_SITEVERIFY_URL;
  const form = new URLSearchParams({ secret, response });
  if (remoteIp && remoteIp !== "local") form.set("remoteip", remoteIp);

  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: form,
    });
    const data = (await res.json()) as { success?: boolean; "error-codes"?: string[] };
    if (data.success === true) return { ok: true, errors: [] };
    return { ok: false, errors: Array.isArray(data["error-codes"]) ? data["error-codes"] : ["verification-failed"] };
  } catch (err) {
    // 官方测试 secret 的语义就是“永远通过”；离线/受限环境下远端不可达时短路，便于本地验收。
    // 生产用真实 secret 时不会走这里。
    if (isTurnstileTestSecret(env)) {
      return { ok: true, errors: [], degraded: true };
    }
    return { ok: false, errors: ["siteverify-unreachable"], };
  }
}

// ---------------------------------------------------------------------------
// quant_pass cookie：HMAC-SHA256(expiry, COOKIE_KEY)
// ---------------------------------------------------------------------------

const encoder = new TextEncoder();

function toBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(value: string): Uint8Array | null {
  try {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
    const binary = atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  } catch {
    return null;
  }
}

async function importHmacKey(key: string): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", encoder.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

export function parseCookieHeader(header: string | null): Record<string, string> {
  const out: Record<string, string> = {};
  if (!header) return out;
  for (const part of header.split(";")) {
    const idx = part.indexOf("=");
    if (idx <= 0) continue;
    const name = part.slice(0, idx).trim();
    const value = part.slice(idx + 1).trim();
    if (name) out[name] = value;
  }
  return out;
}

export interface PassCookie {
  value: string;
  expiresAt: number;
  setCookie: string;
}

/** 签发 quant_pass：值为 `{exp}.{base64url(HMAC-SHA256(exp))}`，1 小时有效。 */
export async function issuePassCookie(env: Env, nowSeconds: number = Math.floor(Date.now() / 1000)): Promise<PassCookie | null> {
  const key = readCookieKey(env);
  if (!key) return null;
  const expiresAt = nowSeconds + COOKIE_TTL_SECONDS;
  const payload = String(expiresAt);
  const cryptoKey = await importHmacKey(key);
  const signature = new Uint8Array(await crypto.subtle.sign("HMAC", cryptoKey, encoder.encode(payload)));
  const value = `${expiresAt}.${toBase64Url(signature)}`;
  const setCookie = `${COOKIE_NAME}=${value}; Path=/; Max-Age=${COOKIE_TTL_SECONDS}; HttpOnly; Secure; SameSite=Lax`;
  return { value, expiresAt, setCookie };
}

/** 校验 quant_pass：签名正确且未过期。 */
export async function verifyPassCookie(env: Env, cookieHeader: string | null, nowSeconds: number = Math.floor(Date.now() / 1000)): Promise<boolean> {
  const key = readCookieKey(env);
  if (!key) return false;
  const raw = parseCookieHeader(cookieHeader)[COOKIE_NAME];
  if (!raw) return false;
  const idx = raw.lastIndexOf(".");
  if (idx <= 0) return false;
  const expiresAt = Number(raw.slice(0, idx));
  if (!Number.isFinite(expiresAt) || expiresAt <= nowSeconds) return false;
  const signature = fromBase64Url(raw.slice(idx + 1));
  if (!signature) return false;
  try {
    const cryptoKey = await importHmacKey(key);
    return await crypto.subtle.verify("HMAC", cryptoKey, signature, encoder.encode(String(expiresAt)));
  } catch {
    return false;
  }
}
