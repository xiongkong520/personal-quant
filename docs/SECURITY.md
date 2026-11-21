# 公网部署与安全（SECURITY.md）

> 2026-11-21 更新：按用户要求**取消 UA/Accept 白名单拦截**，人机验证（Turnstile）+ Cloudflare 边缘防护（自定义域名后的免费 WAF）为主；新增「国内访问」说明。

## 0. 国内访问：用 Cloudflare Pages（已验证可达）

实测（国内网络）：
- ❌ `https://personal-quant-worker.xiongkong520.workers.dev` → 空白/超时（该子域被阻断）
- ✅ `https://quant-board-9y2.pages.dev` → 正常（Pages 静态页 + Pages Functions 同域 API）
- ✅ `www.cloudflare.com`、`challenges.cloudflare.com`（Turnstile 组件）可达

**当前生产方案**：整站部署在 Pages（`quant-board-9y2.pages.dev`），
`cloudflare/pages/functions/api/[[path]].ts` 复用 Worker 实现，D1 / 密钥 / Turnstile 与 Worker 版一致。
部署命令：`cd cloudflare/pages && ../worker/node_modules/.bin/wrangler pages deploy . --project-name quant-board --branch main`

**关于免费套餐防火墙（WAF）**：`*.pages.dev` 与 `*.workers.dev` 都不在 Cloudflare Zone 下，
**无法配置 WAF 自定义规则 / Bot Fight Mode / Security Level**——这些能力需要把自定义域名接入 Cloudflare（Add a site，改 NS）。
当前无域名时的安全层级 = **Turnstile 人机验证 + Worker 内限流（120/min）+ 安全响应头 + Cloudflare 网络层 DDoS 防护**。

如果以后有域名（推荐，可解锁免费 WAF）：
1. Cloudflare Dashboard → Add a site → 填域名 → Free 套餐 → 复制两个 NS。
2. 到注册商把 NS 改成 Cloudflare 的，等待生效。
3. Workers & Pages → `quant-board` → Custom domains 绑定 `quant.你的域名`。
4. Security：Security Level = High、Bot Fight Mode = On；WAF 自定义规则与 Rate Limiting 见 §5。
5. 前端不变（相对路径 `/api/...` 自动跟随域名）。

## 1. 威胁模型

| 威胁 | 防护 |
|---|---|
| 未授权抓 `/api/signals` | **Turnstile 人机验证**：通过后签发 1h HMAC 签名 cookie（HttpOnly/Secure/SameSite=Lax），否则 401 + `{"challenge":true}` |
| 脚本爬虫 | 人机验证 + 限流；(自定义域名后) CF WAF/Bot Fight Mode 边缘拦截 |
| 高频刷接口 / 小规模 DoS | 按 `cf-connecting-ip` **120 次/分钟**限流，429 + Retry-After；(自定义域名后) + CF Rate Limiting |
| 冒用推送接口 | `/api/ingest` 需要 `x-ingest-token`，仅 POST |
| 第三方页面嵌入/劫持 | CSP、`X-Frame-Options: DENY`、`Referrer-Policy`、`Permissions-Policy` |

**已取消**（用户 2026-11-21 要求）：UA 黑名单（curl/抓取库/无头浏览器）、Accept 白名单校验 —— 避免误伤正常浏览器；反爬交给 Turnstile 与边缘 WAF。

**不做**：真实交易、账户密钥、付费数据。

## 2. 防护层（实现位置）

1. `cloudflare/worker/src/security.ts`
   - `checkUserAgent()` / `checkAcceptHeader()`：**已取消拦截**，仅保留 UA 记录
   - `checkSameOrigin()`：CORS 同源校验
   - `checkRateLimit()`：按 IP 120 次/分钟（内存 Map；绑定 KV 时走 KV，生产建议 KV）
   - `applySecurityHeaders()`：CSP / X-Frame-Options / Referrer-Policy / X-Content-Type-Options / Permissions-Policy
2. `POST /api/verify`：Turnstile siteverify（`TURNSTILE_SECRET`）成功 → 签发 `quant_pass` cookie（HMAC-SHA256，`COOKIE_KEY`，1h）
3. `GET /api/signals`、`/api/stream`、`/api/model`：无 cookie → 401 + `{"challenge":true}`
4. `POST /api/ingest`：`x-ingest-token` 鉴权（不受 UA/人机验证限制）
5. 前端（`cloudflare/pages/`）：401 挑战时弹 Turnstile 卡片；429 倒计时；SSE 断线退避

## 3. 部署步骤（Lead 已执行过一次）

```bash
cd cloudflare/worker
cp wrangler.local.toml wrangler.toml      # 真实 D1 id / sitekey（本地文件，不入库）
npx wrangler d1 migrations apply personal_quant --remote
npx wrangler secret put QUANT_INGEST_TOKEN
npx wrangler secret put COOKIE_KEY
npx wrangler secret put TURNSTILE_SECRET
npx wrangler deploy                        # Worker + Assets(pages) 一起上线
git checkout -- wrangler.toml              # 还原仓库版占位符
```

Turnstile 站点：Cloudflare Dashboard → Turnstile → Add site（域名填自定义域名或 workers.dev），
sitekey 写 `[vars] TURNSTILE_SITEKEY`，secret 用 `wrangler secret put`。
本地开发用官方测试 key（永远通过）：sitekey `1x00000000000000000000AA` / secret `1x0000000000000000000000000000000AA`。

## 4. 公网验证清单（当前行为）

```bash
BASE=https://quant.你的域名.com   # 或 workers.dev（国内打不开）

# 1) 健康检查 → 200，security.ua_filter=false
curl -s "$BASE/api/health" | head -c 300

# 2) 未人机验证 → 401 challenge（不再是 403）
curl -s -A "Mozilla/5.0" -H "Accept: application/json" "$BASE/api/signals"

# 3) 浏览器打开 → Turnstile 验证 → cookie → 数据出现
# 4) 高频 → 429
for i in $(seq 1 130); do curl -s -o /dev/null -w "%{http_code} " -A "Mozilla/5.0" "$BASE/api/signals"; done; echo

# 5) 安全头
curl -sI "$BASE/" | grep -iE "content-security|x-frame|referrer"
```

## 5. 已知局限

- `*.workers.dev` 国内被阻断（网络层），与代码/安全策略无关；自定义域名是唯一稳定方案。
- Worker 内存态限流在多 isolate 下不共享，精度有限；严格限流请绑 KV 或用 CF Rate Limiting。
- Turnstile 依赖 `challenges.cloudflare.com`（国内实测可达，但网络差时可能慢；前端有提示与重试）。
- Worker 免费额度：10 万请求/天；D1 5GB/500 万读/天。
- 本项目不防高强度 DDoS（需 CF 商业防护），只防脚本爬虫与低强度滥用。

## 6. 应急

- 疑似被爬：自定义域名下开 WAF 规则 / 调低 `RATE_LIMIT_PER_MIN`；或暂时要求更严格的 Turnstile 模式。
- 信号被刷：轮换 `QUANT_INGEST_TOKEN`（本地推送同步更新）。
- 彻底关停：`npx wrangler delete`（或 dashboard 删除 Worker），域名随即不可达。
