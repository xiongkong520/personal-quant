# 公网部署与安全（SECURITY.md）

把看板发布到公网时，它不是"内部工具"：任何人扫到 URL 都可能来抓数据、刷接口、跑爬虫。
本文说明本项目的防护设计、部署步骤与验证方法。

> 重要事实：**`*.workers.dev` 子域不经过 Cloudflare 的 WAF / Bot Fight Mode / Security Level**（那些只对已接入的 zone 生效）。
> 所以本项目的"防火墙"实现在 **Worker 代码内**：UA 过滤、Accept 校验、同源 CORS、限流、Turnstile、签名 cookie、安全响应头。
> 如果以后把 Worker 绑定到自己的域名（zone），再叠加 Cloudflare Dashboard 侧的 WAF 规则（本文 §5 给出规则模板）。

## 1. 威胁模型

| 威胁 | 防护 |
|---|---|
| 脚本爬虫抓 `/api/signals` | UA 黑名单 + Accept 校验 + Turnstile 通过后的签名 cookie |
| 高频刷接口 / 小规模 DoS | 按 `cf-connecting-ip` 每分钟限流，429 + Retry-After |
| 伪造 "正常浏览器" UA | 必须持 `quant_pass` 签名 cookie（HMAC，HttpOnly），否则 401 + `{"challenge":true}` |
| 绕过人机验证直接调 API | cookie 只能由 `/api/verify` 在 Turnstile siteverify 成功后签发 |
| 冒用本地推送接口 | `/api/ingest` 需要 `x-ingest-token`，仅 POST |
| 数据被第三方页面嵌入/劫持 | CSP、`X-Frame-Options: DENY`、`Referrer-Policy`、`Permissions-Policy` |

**不做**：真实交易、账户密钥、付费数据；`/api/ingest` 不暴露任何写入用户数据的能力（只写信号历史）。

## 2. 防护层（实现位置）

1. `cloudflare/worker/src/security.ts`（或等价文件）
   - `guardRequest()`：UA 黑名单（curl/wget/python-requests/scrapy/httpie/node-fetch/Go-http-client/HeadlessChrome…）、Accept 校验、CORS 同源
   - `rateLimit()`：按 IP 60 次/分钟（本地 dev 用内存 Map；多实例部署时可换 KV/D1，见代码注释）
   - `requirePass()`：校验 `quant_pass` cookie（HMAC-SHA256，`COOKIE_KEY`，1 小时）
   - `securityHeaders()`：CSP、X-Frame-Options、Referrer-Policy、X-Content-Type-Options、Permissions-Policy
2. `POST /api/verify`：Turnstile siteverify（`TURNSTILE_SECRET`），成功签发 cookie
3. `GET /api/config`：返回 `sitekey`（`TURNSTILE_SITEKEY`，缺省用官方测试 key）
4. 前端（`cloudflare/pages/`）：401 `{"challenge":true}` 时弹出 Turnstile 卡片，验证通过后重试

## 3. 部署步骤（已登录账号；Lead 执行）

```bash
cd cloudflare/worker

# 1) D1 已创建：personal_quant（database_id 见 wrangler.toml）
npx wrangler d1 migrations apply personal_quant --remote
npx wrangler d1 execute personal_quant --remote --file=schema.sql   # 或用 migrations

# 2) 密钥（不要写进仓库；.dev.vars 已在 .gitignore）
openssl rand -hex 32 | npx wrangler secret put COOKIE_KEY
openssl rand -hex 24 | npx wrangler secret put QUANT_INGEST_TOKEN
npx wrangler secret put TURNSTILE_SECRET   # 真实 Turnstile secret（本地用测试 key）

# 3) 部署（Worker Assets 指向 ../pages）
npx wrangler deploy

# 4) 拿到公网 URL：https://personal-quant-worker.<subdomain>.workers.dev
```

真实 Turnstile widget：创建时把 `personal-quant-worker.<subdomain>.workers.dev` 填进 domains，
拿到 sitekey 后用 `npx wrangler secret put TURNSTILE_SITEKEY` 写入（或 dashboard 配置），再 `wrangler deploy` 一次。
本地开发用官方测试 key（永远通过）：sitekey `1x00000000000000000000AA`，secret `1x0000000000000000000000000000000AA`。

## 4. 公网验证清单

```bash
BASE=https://personal-quant-worker.<subdomain>.workers.dev

# 1) 无 UA / curl UA → 403
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/api/signals"
curl -s -o /dev/null -w "%{http_code}\n" -A "curl/8.5.0" "$BASE/api/signals"

# 2) 伪装浏览器 UA 但无 cookie → 401 + challenge
curl -s -A "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)" "$BASE/api/signals"

# 3) 浏览器打开 → Turnstile 通过 → 看板数据出现；DevTools Network 里信号请求带 quant_pass cookie
# 4) 高频 → 429
for i in $(seq 1 80); do curl -s -o /dev/null -w "%{http_code} " -A "Mozilla/5.0" "$BASE/api/signals"; done; echo

# 5) 安全头
curl -sI -A "Mozilla/5.0" "$BASE/" | grep -iE "content-security|x-frame|referrer"
```

预期：1) 403、2) 401、4) 出现 429、5) 有 CSP/X-Frame-Options。

## 5. 绑定自定义域名后（可选，增强）

1. Cloudflare Dashboard → Security → Settings：
   - Security Level: **High**
   - Bot Fight Mode: **On**
2. WAF → Custom rules（免费计划可用）：
   - `(http.user_agent contains "curl") or (http.user_agent contains "python") or (http.user_agent contains "scrapy")` → Block
   - `(cf.threat_score gt 10)` → Managed Challenge
   - `http.request.uri.path contains "/api/ingest" and ip.src ne <你的固定出口IP>` → Block
3. Rate limiting：`/api/*` 每 IP 60 次/分钟 → Block 1 分钟（与 Worker 内限流叠加）
4. Turnstile：用 **Managed Challenge** 模式，结合自定义域名更稳定。

## 6. 已知局限

- Worker 内存态限流在多实例/多地区下不共享，精度有限；要严格限流需 KV/D1/Durable Object（代码已留注释）。
- `workers.dev` 无法使用 WAF/Bot Fight Mode/Under Attack 模式，本文 §3 的代码内防护是唯一手段。
- Turnstile 测试 key 永远通过，只能用于本地联调；公网必须换真实 key。
- Worker 免费额度：10 万请求/天、D1 5GB/500 万读/天；超限会被限流，属于可接受边界。
- 本项目不防"高强度定向 DDoS"（需 Cloudflare 商业防护），只防脚本爬虫与低强度滥用。

## 7. 应急

- 疑似被爬：临时把 `RATE_LIMIT_PER_MIN` 调低（如 10）→ `wrangler deploy`；或直接改看板为需 Turnstile 强制。
- 信号被刷：`/api/ingest` 轮换 `QUANT_INGEST_TOKEN`，本地推送同步更新。
- 彻底关停：`npx wrangler delete`（或 dashboard 删除 Worker），域名随即不可达。
