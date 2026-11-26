/**
 * 境外信息代理通道（本机 → Cloudflare Worker → 境外站点）。
 *
 * 用途：本机环境在需要访问境外信息（Google/Gemini/Kaggle/GitHub raw 等）时，
 * 通过 Worker 出口（海外）转发请求，不依赖用户手机 VPN，也不影响用户网络。
 *
 * 安全：
 * - x-ingest-token 鉴权（与推送 token 相同）；
 * - 域名白名单（仅限查资料/API 用途）；
 * - 仅 https、GET/POST，超时 30s，响应体限制 4MB。
 */
import type { Env } from "./types.ts";

const ALLOWED_HOSTS = new Set([
  "www.google.com",
  "google.com",
  "generativelanguage.googleapis.com",
  "aistudio.google.com",
  "kaggle.com",
  "www.kaggle.com",
  "raw.githubusercontent.com",
  "api.github.com",
  "github.com",
  "pypi.org",
  "files.pythonhosted.org",
  "huggingface.co",
  "cdn.jsdelivr.net",
  "api.openai.com",
  "api.anthropic.com",
  "query1.finance.yahoo.com",
  "query2.finance.yahoo.com",
  "finance.yahoo.com",
]);

const MAX_BODY_BYTES = 4 * 1024 * 1024;
const TIMEOUT_MS = 30000;

export interface ProxyRequest {
  url: string;
  method?: "GET" | "POST";
  headers?: Record<string, string>;
  body?: string;
}

/** 境外信息转发（鉴权由路由层负责）。 */
export async function handleForeignProxy(request: Request): Promise<Response> {
  let payload: ProxyRequest;
  try {
    payload = (await request.json()) as ProxyRequest;
  } catch {
    return new Response(JSON.stringify({ ok: false, error: "invalid_json" }), {
      status: 400,
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  }
  const target = String(payload.url ?? "").trim();
  let parsed: URL;
  try {
    parsed = new URL(target);
  } catch {
    return new Response(JSON.stringify({ ok: false, error: "invalid_url" }), {
      status: 400,
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  }
  if (parsed.protocol !== "https:" || !ALLOWED_HOSTS.has(parsed.hostname)) {
    return new Response(JSON.stringify({ ok: false, error: "host_not_allowed", host: parsed.hostname }), {
      status: 403,
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  }
  const method = payload.method === "POST" ? "POST" : "GET";
  const headers: Record<string, string> = { "user-agent": "quant-foreign-proxy/1.0" };
  for (const [key, value] of Object.entries(payload.headers ?? {})) {
    if (typeof value === "string" && key.toLowerCase() !== "host" && key.toLowerCase() !== "cookie") {
      headers[key] = value;
    }
  }
  try {
    const resp = await fetch(parsed.toString(), {
      method,
      headers,
      body: method === "POST" ? payload.body : undefined,
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    const buffer = await resp.arrayBuffer();
    if (buffer.byteLength > MAX_BODY_BYTES) {
      return new Response(JSON.stringify({ ok: false, error: "response_too_large", bytes: buffer.byteLength }), {
        status: 413,
        headers: { "content-type": "application/json; charset=utf-8" },
      });
    }
    let bodyText: string;
    const decoder = new TextDecoder("utf-8");
    try {
      bodyText = decoder.decode(buffer);
    } catch {
      return new Response(
        JSON.stringify({
          ok: true,
          status: resp.status,
          content_type: resp.headers.get("content-type") ?? "",
          base64: btoa(String.fromCharCode(...new Uint8Array(buffer))),
        }),
        { headers: { "content-type": "application/json; charset=utf-8" } },
      );
    }
    return new Response(
      JSON.stringify({
        ok: true,
        status: resp.status,
        content_type: resp.headers.get("content-type") ?? "",
        body: bodyText,
      }),
      { headers: { "content-type": "application/json; charset=utf-8" } },
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ ok: false, error: "fetch_failed", message: err instanceof Error ? err.message : String(err) }),
      { status: 502, headers: { "content-type": "application/json; charset=utf-8" } },
    );
  }
}
