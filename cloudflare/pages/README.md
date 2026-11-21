# Cloudflare Pages 产物（云端实时看板）

由 Worker 静态挂载（`wrangler.toml [assets] directory = "../pages"`）：`/api/*` 走 Worker，其余路径返回本目录静态文件。

## 文件

- `index.html` —— 与 `frontend/index.html` 同构，差异只有：`window.QT_MODE="cloud"`、`<title>`、brand-sub、页脚数据来源、`foot.mode` 文案。
- `styles.css`、`app.js` —— 与 `frontend/` 下同名文件**逐字节一致**（同一实现，按 `QT_MODE` 切换轮询/SSE）。

修改后同步：

```bash
cp frontend/styles.css frontend/app.js cloudflare/pages/
# index.html 的 6 处差异需手工保留（见 ../docs/DESIGN.md 与前端 README）
node --check cloudflare/pages/app.js
```

## 本地预览（本机 workerd 不可用时的 node 回退）

```bash
npm --prefix cloudflare/worker run dev:node   # → http://127.0.0.1:8788/
```

首次访问流程：`GET /api/config` → 页面收到 401 `{"challenge":true}` → 弹 Turnstile 验证 →
`POST /api/verify` 换 HttpOnly Cookie → 重试 `/api/signals` 并连 `/api/stream`（SSE）。
本地测试 sitekey 为 Cloudflare 官方测试 key，Worker 内置 siteverify mock（任意 token 通过，`token=fail` 模拟失败）。

## 云端数据说明

Worker 只提供 `/api/health`、`/api/config`、`/api/verify`、`/api/signals`、`/api/stream`、`/api/model`、`POST /api/ingest`。
`/api/paper`、`/api/equity`、`/api/backtest` 属于本地 FastAPI，云端页面会渲染"本地生成"空态（命令提示），不发起跨端请求、不报错。

## 安全与降级

- 人机验证覆盖层、429 倒计时、SSE 指数退避重连与"顶部 2px 状态细条"均由 `app.js` 统一实现。
- 唯一外部资源是 Turnstile 官方脚本（懒加载，仅挑战时请求）；其余样式/字体/图标全部内联或本地，断网仍可渲染空态。
- 详细动效、无障碍、自测清单见 [`../frontend/README.md`](../frontend/README.md)。
