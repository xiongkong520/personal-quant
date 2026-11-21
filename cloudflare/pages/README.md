# Cloudflare Pages 看板（生产：https://quant-board-9y2.pages.dev）

同域部署：静态页 + Pages Functions（`functions/api/[[path]].ts` 复用 `../worker/src/index.ts`），
`/api/*` 由 Functions 处理，其余路径由 Pages 静态托管。

> 背景：`*.workers.dev` 在国内被阻断（空白/超时），`*.pages.dev` 实测可达（Via/浏览器已验证），
> 所以国内访问入口用 Pages，Worker 版保留作备用/自定义域名方案。

## 部署

```bash
cd cloudflare/pages
../worker/node_modules/.bin/wrangler pages deploy . --project-name quant-board --branch main --commit-dirty=true
```

首次需创建项目与配置（已完成，记录备查）：
- `wrangler pages project create quant-board --production-branch main`
- D1 绑定 `DB → personal_quant`（Pages 项目 Settings → Functions → D1）
- 环境变量 `TURNSTILE_SITEKEY`
- Secrets：`COOKIE_KEY`、`TURNSTILE_SECRET`、`QUANT_INGEST_TOKEN`
  （`wrangler pages secret put <NAME> --project-name quant-board`）
- Turnstile 站点域名需包含 `quant-board-9y2.pages.dev`

## 文件

- `index.html` —— 与 `frontend/index.html` 同构，差异只有：`window.QT_MODE="cloud"`、`<title>`、brand-sub、页脚数据来源、`foot.mode` 文案。
- `styles.css`、`app.js` —— 与 `frontend/` 下同名文件**逐字节一致**（按 `QT_MODE` 切换轮询/SSE）。
- `functions/api/[[path]].ts` —— Pages Functions 适配层，把 `/api/*` 交给 Worker 的 `fetch` 实现。

修改后同步：

```bash
cp frontend/styles.css frontend/app.js cloudflare/pages/
# index.html 的 6 处差异需手工保留（见 ../docs/DESIGN.md 与前端 README）
node --check cloudflare/pages/app.js
```

## 安全（当前无域名时的层级）

- Turnstile 人机验证：未通过 → `/api/signals`、`/api/stream`、`/api/model` 返回 401 `{"challenge":true}`
- Worker 内限流：按 IP 120 次/分钟（429 + Retry-After）
- 安全响应头：CSP、X-Frame-Options、Referrer-Policy 等
- `*.pages.dev` 不在 Cloudflare Zone 下，**无法配置 WAF 自定义规则**；需要 WAF 时见 `../docs/SECURITY.md`（绑定自定义域名）

## 数据流

本地 Python（`python -m quant.cli export`，`cloudflare.worker_url` 指向本页地址）
→ `POST /api/ingest`（x-ingest-token）→ D1 → 页面 SSE 实时更新。
