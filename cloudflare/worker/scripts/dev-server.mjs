#!/usr/bin/env node
/**
 * 本地 Node 运行时（wrangler / workerd 不可用环境下的回退，例如 Android proot / 39-bit VA 手机）：
 * 直接 import src/index.ts 的 Worker fetch handler，用 node:sqlite 实现 D1 binding，
 * 用最小静态文件服务实现 ASSETS binding（cloudflare/pages/）。
 *
 * 端点行为与云端 Worker 完全一致（同一份代码），可用于：
 *   - 契约验收：curl /api/health、/api/signals、POST /api/ingest、GET /api/stream
 *   - 前端联调：前端直接请求 http://127.0.0.1:8788
 *
 * 用法：
 *   node scripts/dev-server.mjs [--port 8788] [--db .wrangler/local-dev/quant-cloud.sqlite] [--no-seed]
 * 环境变量（也可写在 .dev.vars）：
 *   QUANT_INGEST_TOKEN  推送 token；未配置时用内置 local-dev-token
 *   COOKIE_KEY          Turnstile 通过后签发 quant_pass 的 HMAC 密钥；未配置时用内置本地密钥
 *   TURNSTILE_SITEKEY / TURNSTILE_SECRET  默认用 Cloudflare 官方测试密钥
 *   TURNSTILE_VERIFY_URL  默认指向本脚本内置的 siteverify mock（任意 token 通过；token=fail 模拟失败）
 *   RATE_LIMIT_PER_MIN  每分钟限流次数（默认 60）
 *   QUANT_DEV_DB        本地 D1 SQLite 文件路径
 *
 * 标准开发入口仍是：npx wrangler dev --local --port 8788（有正常 48-bit VA 的机器）
 */
import { createServer } from "node:http";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import worker from "../src/index.ts"; // Node 24 原生 TypeScript type stripping
import { createD1 } from "../test/d1-shim.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const WORKER_DIR = resolve(HERE, "..");
const PAGES_DIR = resolve(WORKER_DIR, "../pages");

const args = process.argv.slice(2);
const argValue = (flag, fallback) => {
  const idx = args.indexOf(flag);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : fallback;
};

const PORT = Number(argValue("--port", process.env.PORT ?? "8788"));
const DB_PATH = resolve(WORKER_DIR, argValue("--db", process.env.QUANT_DEV_DB ?? ".wrangler/local-dev/quant-cloud.sqlite"));
const NO_SEED = args.includes("--no-seed");
const HOST = "127.0.0.1";

function readDevVars() {
  const file = join(WORKER_DIR, ".dev.vars");
  const vars = {};
  if (!existsSync(file)) return vars;
  for (const rawLine of readFileSync(file, "utf8").split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    vars[key] = value;
  }
  return vars;
}

const devVars = readDevVars();
const NO_TOKEN = args.includes("--no-token"); // 测试用：模拟未配置 token 的 Worker
const configuredToken = (
  process.env.QUANT_INGEST_TOKEN ??
  process.env.INGEST_TOKEN ??
  devVars.QUANT_INGEST_TOKEN ??
  ""
).trim();
const INGEST_TOKEN = NO_TOKEN ? "" : configuredToken || "local-dev-token";
const TOKEN_SOURCE = NO_TOKEN ? "--no-token" : configuredToken ? "环境变量 / .dev.vars" : "内置默认值（本地开发）";

// 安全层（公网能力本地预览）：COOKIE_KEY / Turnstile / 限流
const COOKIE_KEY = (process.env.COOKIE_KEY ?? devVars.COOKIE_KEY ?? "local-dev-cookie-key-change-me").trim();
const COOKIE_KEY_SOURCE = (process.env.COOKIE_KEY ?? devVars.COOKIE_KEY) ? "环境变量 / .dev.vars" : "内置默认值（本地开发）";
const TURNSTILE_SITEKEY = (process.env.TURNSTILE_SITEKEY ?? devVars.TURNSTILE_SITEKEY ?? "").trim();
const TURNSTILE_SECRET = (process.env.TURNSTILE_SECRET ?? devVars.TURNSTILE_SECRET ?? "").trim();
/** 内置 siteverify mock：本地不需要外网也能完成 Turnstile 验证流程。 */
const TURNSTILE_VERIFY_URL = (
  process.env.TURNSTILE_VERIFY_URL ??
  devVars.TURNSTILE_VERIFY_URL ??
  `http://127.0.0.1:${PORT}/__dev/turnstile/siteverify`
).trim();
const VERIFY_URL_IS_MOCK = TURNSTILE_VERIFY_URL === `http://127.0.0.1:${PORT}/__dev/turnstile/siteverify`;
const RATE_LIMIT_PER_MIN = (process.env.RATE_LIMIT_PER_MIN ?? devVars.RATE_LIMIT_PER_MIN ?? "60").trim();

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".htm": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".ico": "image/x-icon",
  ".webmanifest": "application/manifest+json",
  ".txt": "text/plain; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".woff2": "font/woff2",
};

function servePage(request) {
  const url = new URL(request.url);
  let pathname;
  try {
    pathname = decodeURIComponent(url.pathname);
  } catch {
    pathname = url.pathname;
  }
  if (pathname === "/") pathname = "/index.html";
  const filePath = resolve(PAGES_DIR, `.${pathname}`);
  if (!filePath.startsWith(PAGES_DIR)) return new Response("Not Found", { status: 404 });
  if (!existsSync(filePath) || !statSync(filePath).isFile()) return new Response("Not Found", { status: 404 });
  return new Response(readFileSync(filePath), {
    status: 200,
    headers: { "content-type": MIME[extname(filePath).toLowerCase()] ?? "application/octet-stream", "cache-control": "no-store" },
  });
}

function readRequestBody(req) {
  return new Promise((resolveBody, rejectBody) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => resolveBody(Buffer.concat(chunks)));
    req.on("error", rejectBody);
  });
}

const db = createD1(DB_PATH);
await db.exec(readFileSync(join(WORKER_DIR, "schema.sql"), "utf8"));
let seeded = false;
if (!NO_SEED) {
  const countRow = await db.prepare("SELECT COUNT(*) AS n FROM signals").first();
  if (Number(countRow?.n ?? 0) === 0) {
    await db.exec(readFileSync(join(WORKER_DIR, "seed.sql"), "utf8"));
    seeded = true;
  }
}

const env = {
  DB: db,
  ASSETS: { fetch: async (request) => servePage(request) },
  QUANT_INGEST_TOKEN: INGEST_TOKEN,
  COOKIE_KEY,
  TURNSTILE_SITEKEY,
  TURNSTILE_SECRET,
  TURNSTILE_VERIFY_URL,
  RATE_LIMIT_PER_MIN,
  WORKER_VERSION: "0.1.0-node-dev",
};

const server = createServer(async (req, res) => {
  const startedAt = Date.now();
  const controller = new AbortController();
  try {
    const hostHeader = req.headers.host ?? `${HOST}:${PORT}`;
    const url = new URL(req.url ?? "/", `http://${hostHeader}`);

    // 内置 Turnstile siteverify mock（不经过 Worker）：任意 token 通过；token=fail 模拟校验失败。
    if (url.pathname === "/__dev/turnstile/siteverify" && req.method === "POST") {
      const body = await readRequestBody(req);
      const params = new URLSearchParams(body.toString());
      const response = params.get("response") ?? "";
      const ok = response !== "fail";
      const payload = ok ? { success: true } : { success: false, "error-codes": ["invalid-input-response"] };
      res.writeHead(200, { "content-type": "application/json; charset=utf-8" });
      res.end(JSON.stringify(payload));
      return;
    }

    const headers = new Headers();
    for (const [key, value] of Object.entries(req.headers)) {
      if (Array.isArray(value)) headers.set(key, value.join(", "));
      else if (typeof value === "string") headers.set(key, value);
    }
    let body;
    if (!["GET", "HEAD"].includes(req.method ?? "GET")) body = await readRequestBody(req);

    res.on("close", () => {
      if (!res.writableEnded) controller.abort();
    });

    const request = new Request(url, { method: req.method, headers, body, signal: controller.signal });
    const response = await worker.fetch(request, env, { waitUntil() {}, passThroughOnException() {} });
    res.statusCode = response.status;
    for (const [key, value] of response.headers) {
      if (key.toLowerCase() === "set-cookie") continue;
      res.setHeader(key, value);
    }
    const setCookies = typeof response.headers.getSetCookie === "function" ? response.headers.getSetCookie() : [];
    if (setCookies.length > 0) res.setHeader("set-cookie", setCookies);
    res.flushHeaders?.();
    console.log(`${req.method} ${url.pathname}${url.search} -> ${response.status} (${Date.now() - startedAt}ms)`);

    if (!response.body) {
      res.end();
      return;
    }
    const reader = response.body.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!res.write(Buffer.from(value))) await new Promise((resolveDrain) => res.once("drain", resolveDrain));
      }
      res.end();
    } catch (err) {
      if (controller.signal.aborted || res.writableEnded) {
        try {
          res.end();
        } catch {
          /* ignore */
        }
      } else {
        console.error(`[dev-server] 响应流异常：${err instanceof Error ? err.message : String(err)}`);
        try {
          res.destroy();
        } catch {
          /* ignore */
        }
      }
    }
  } catch (err) {
    console.error(`[dev-server] 请求处理失败：`, err);
    if (!res.headersSent) res.writeHead(500, { "content-type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ schema: 1, ok: false, error: "dev_server_error", message: err instanceof Error ? err.message : String(err) }));
  }
});

server.on("error", (err) => {
  console.error(`[dev-server] 启动失败（端口 ${PORT}）：${err.message}`);
  process.exit(1);
});

server.listen(PORT, HOST, () => {
  const pagesReady = existsSync(join(PAGES_DIR, "index.html"));
  console.log("────────────────────────────────────────────────────────────");
  console.log(`  personal-quant Worker (node dev runtime)`);
  console.log(`  http://${HOST}:${PORT}/            静态看板 ${pagesReady ? "cloudflare/pages/index.html" : "(cloudflare/pages/ 还没有产物，返回提示 JSON)"}`);
  console.log(`  http://${HOST}:${PORT}/api/health`);
  console.log(`  http://${HOST}:${PORT}/api/signals`);
  console.log(`  http://${HOST}:${PORT}/api/stream   (SSE, 30s 心跳)`);
  console.log(`  POST /api/ingest 鉴权：Authorization: Bearer <QUANT_INGEST_TOKEN>`);
  console.log(`  D1 本地文件：${DB_PATH}`);
  console.log(`  push token：${INGEST_TOKEN ? "已配置" : "未配置（ingest 将返回 503，可用 QUANT_INGEST_TOKEN 配置）"}（来源：${TOKEN_SOURCE}）`);
  if (seeded) console.log("  已导入 seed.sql 演示信号（4 条）");
  console.log(`  安全：UA 黑名单 / Accept / 同源 CORS / 安全响应头；限流 ${RATE_LIMIT_PER_MIN}/min/IP`);
  console.log(`  Turnstile：sitekey=${TURNSTILE_SITEKEY || "测试 1x00000000000000000000AA"}，siteverify=${VERIFY_URL_IS_MOCK ? "内置 mock（任意 token 通过）" : TURNSTILE_VERIFY_URL}`);
  console.log(`  cookie：quant_pass（HMAC，1 小时）COOKIE_KEY 来源=${COOKIE_KEY_SOURCE}`);
  console.log('  验证流程：GET /api/config -> POST /api/verify {"token":"任意(本地mock)"} -> 带 cookie 访问 /api/signals');
  console.log("  注意：这是 workerd 不可用环境下的本地回退；标准入口仍为 npx wrangler dev --local --port 8788");
  console.log("────────────────────────────────────────────────────────────");
});

const shutdown = () => {
  console.log("\n[dev-server] 关闭中…");
  server.close(() => {
    db.close();
    process.exit(0);
  });
  setTimeout(() => process.exit(0), 1500).unref();
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
