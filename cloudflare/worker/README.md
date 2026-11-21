# Cloudflare Worker · 个人量化工作台（cloud-dev / task-4）

Worker + D1 + 静态看板托管（Pages 产物由 frontend-dev 写，Worker 通过 Assets 挂载）。
本地开发**不需要 Cloudflare 账号**，全部跑在本地；公网部署时带反爬 / 限流 / Turnstile 人机验证。

> ⚠️ 仅供研究学习，非投资建议。本项目只做模拟盘，不接券商、不下真实订单。

## 端点

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /api/config` | 公开 | 前端启动时读：Turnstile sitekey、验证路径、`challenge:true`、限流参数 |
| `POST /api/verify` | 公开 | `{token}` → Turnstile 校验，通过后 `Set-Cookie: quant_pass=<HMAC>`（1 小时） |
| `GET /api/health` | 公开 | 健康检查：D1/模型/安全配置；监控请带浏览器 UA + Accept |
| `GET /api/signals` | `quant_pass` | 从 D1 读信号 → `{schema:1, items:[...], updated_at}`；`?symbols=600519,000001`、`?limit=200`(≤500)、`?latest=1`、`?status=ok` |
| `GET /api/stream` | `quant_pass` | SSE：`hello` → `signals`（5s 轮询 D1 推送新信号）→ 30s 心跳 `: heartbeat`；`id` = `updated_at`；30 分钟后发 `bye` 后 EventSource 自动重连 |
| `GET /api/model` | `quant_pass` | 附加端点：Worker 打包的 §5 权重 + 零特征推理 probe（核对双端同源） |
| `POST /api/ingest` | token | 本地 Python 推送信号；单个 §4 对象 / 数组 / `{items:[...]}`；UPSERT 幂等 |
| `GET /` 及静态路径 | 公开（反爬） | `cloudflare/pages/` 产物；目录为空时返回提示 JSON |

约定：
- `GET /api/signals` 的 `items` = 契约 §4 信号 JSON（`prob` 对象、`confidence`=max(prob) 4 位、`top_features` 按 |contrib| 降序 ≤5）。
- D1 `signals` 表 = 契约 §3.3，**不含 paper_\* 字段**；§4 的 `name`/`paper` 不落库（前端按 symbol 映射 name，paper 属本地模拟盘）。
- `direction` 必须与 `prob` argmax 一致，`prob` 和必须为 1（±1e-6）；`trade_date`/`expires_at`/`confidence`/`signal_id` 缺失时按契约推导并返回 `warnings`。
- `signal_id` 缺失时生成 `{symbol}-{trade_date}-{asof_compact}`，如 `600519-2026-11-21-20261121143500`。
- 新需求新增了 `/api/config`、`/api/verify`（Lead 会同步更新 `docs/CONTRACT.md` §9）。

## 安全能力（公网部署）

全部在 `src/security.ts` 实现，`src/index.ts` 统一接入（`securityGate` + `applySecurityHeaders`）。

### 1. 反爬 / 浏览器指纹
- **UA 黑名单**：`curl`、`wget`、`python-requests`、`python-urllib`、`aiohttp`、`httpx`、`scrapy`、`httpie`、`node-fetch`、`undici`、`axios`、`Go-http-client`、`okhttp`、`libwww-perl`、`HeadlessChrome`、`phantomjs`、`puppeteer`、`playwright`、`selenium`、`*bot*`/`spider`/`crawler`、Postman、Insomnia → **403** `{"error":"forbidden_client"}`；**无 UA 也 403**。
- **Accept 校验**：缺失或不是 `text/html|application/json|text/event-stream|text/plain|*/*` → **403** `{"error":"bad_accept"}`。
- **同源 CORS**：带跨域 `Origin` 的请求 → **403** `{"error":"cross_origin_forbidden"}`；预检 `OPTIONS` 只对同源回 204 + 回显 `Access-Control-Allow-Origin`，跨域 403。不再有 `*` 通配。
- **安全响应头**（所有响应，含 4xx/5xx）：
  - `Content-Security-Policy`: `default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; ...; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`
  - `X-Frame-Options: DENY`、`Referrer-Policy: strict-origin-when-cross-origin`、`X-Content-Type-Options: nosniff`、`Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()`
- 例外：`POST /api/ingest` 是 token 鉴权的机器接口，不受 UA/Accept 反爬限制（Python `httpx` 直接推送，见 `src/quant/export_cloud.py`）；但同样计入限流。

### 2. 限流（按 IP，默认 60 次/分钟）
- 客户端 IP：`cf-connecting-ip`（Cloudflare 边缘写入）→ 本地回退 `x-forwarded-for` / `x-real-ip` / `local`。
- 本地 `wrangler dev --local` / `npm run dev:node`：**内存 Map**（每 isolate 固定 60s 窗口）即可。
- 部署：建议绑定 KV（跨 isolate + 边缘各地生效），`env.RATE_LIMIT_KV` 存在时走 KV；也可改用 D1（`src/security.ts` 末尾有建表 + UPSERT 注释实现）。
- 超限：**429** + `Retry-After: <秒>` + `X-RateLimit-Limit/Remaining/Reset`，body `{"error":"rate_limited","retry_after":N}`。
- `RATE_LIMIT_PER_MIN` 可调（默认 60；压测/联调可临时调大）。

### 3. Turnstile 人机验证
流程：
1. 前端 `GET /api/config` 拿 sitekey（返回字段有多别名：`sitekey`/`turnstile_sitekey`/`turnstileSitekey`/`turnstile.sitekey`，另有 `verify_path`、`cookie_name`、`cookie_ttl_seconds`、`rate_limit`）。
2. 前端渲染 Turnstile widget，拿到 token 后 `POST /api/verify {"token":"..."}`（同源 `credentials: 'same-origin'`）。
3. Worker 用 `TURNSTILE_SECRET` 调 `https://challenges.cloudflare.com/turnstile/v0/siteverify`：
   - 成功 → **200** `{"schema":1,"ok":true,"expires_in":3600,"expires_at":"..."}` + `Set-Cookie: quant_pass=<exp>.<base64url(HMAC-SHA256(exp, COOKIE_KEY))>; Path=/; Max-Age=3600; HttpOnly; Secure; SameSite=Lax`；
   - 失败 → **403** `{"schema":1,"ok":false,"error":"challenge_failed","errors":[...]}`；
   - 缺 token → 400；`COOKIE_KEY` 未配置 → 503。
4. `/api/signals`、`/api/stream`、`/api/model` 校验 `quant_pass`：未通过 → **401** `{"schema":1,"ok":false,"error":"challenge_required","challenge":true}`（前端识别 `challenge:true` 弹验证卡）。
- cookie 1 小时有效、HttpOnly + Secure + SameSite=Lax；HMAC 用 `crypto.subtle`，密钥只从 `env.COOKIE_KEY` 读。本地 `http://127.0.0.1:8788` 被浏览器视为 secure context，cookie 可正常保存。
- 默认用 Cloudflare 官方**测试密钥**（不能上生产）：sitekey `1x00000000000000000000AA`、secret `1x0000000000000000000000000000000AA`（永远通过）。真实部署必须换成自己 Turnstile 站点的 key/secret。
- 离线/受限环境：测试 secret 下远端 siteverify 不可达时按“测试密钥必过”短路（`degraded:true`），生产真实 secret 不会短路。`TURNSTILE_VERIFY_URL` 仅供测试覆盖（如本地 mock），生产不要设置。

## 目录

```
cloudflare/worker/
├── package.json          # wrangler 4 / Node 24；npm run dev / dev:node / parity / db:* / security-test
├── wrangler.toml         # D1 binding + [assets] ../pages + dev port 8788；KV/vars 注释示例
├── schema.sql            # D1 建表（契约 §3.3）+ 索引（手工/CI 用）
├── migrations/0001_create_signals.sql
├── seed.sql              # 4 条演示信号（幂等），方便前端联调
├── models/model.json     # §5 模型权重（训练产物由 model-dev 提供，npm run sync-model 同步）
├── src/
│   ├── index.ts          # 路由 + 端点 + SSE + 静态资源 + 安全门
│   ├── security.ts       # 反爬 UA/Accept、同源 CORS、安全头、限流、Turnstile、HMAC cookie
│   ├── db.ts             # D1 建表/UPSERT/查询/健康
│   ├── signals.ts        # §4 校验/归一化 + token 常量时间比较
│   ├── model.ts          # §5 softmax 推理（TS）
│   ├── schema.ts         # 与 schema.sql 同源的 DDL（Worker 自动建表用）
│   └── types.ts
├── test/
│   ├── parity.mjs        # §10 双端推理 parity（node 直跑，无需 npm install）
│   ├── fixtures/parity_cases.json  # 自造 fixture（期望值由 Python 按 §5 公式算出）
│   └── d1-shim.mjs       # node:sqlite 实现 D1 子集（本地回退运行时用）
└── scripts/
    ├── dev-server.mjs    # 无 workerd 环境的本地回退运行时（同一份 src 代码 + 内置 Turnstile mock）
    ├── smoke.sh          # 端点 + 人机验证全流程烟测
    ├── security-test.sh  # 安全验收：无UA/curl UA→403、无cookie→401、verify→cookie→200、坏cookie→401、高频→429
    ├── check-bundle.mjs  # esbuild 打包自检（不启动 workerd）
    ├── check-schema.mjs  # schema.sql / migrations / schema.ts 三处一致性
    └── sync-model.mjs    # data/models/model.json -> models/model.json
```

## 本地验证（标准方式，需要能跑 workerd 的机器）

```bash
cd cloudflare/worker
npm install

# 密钥/测试配置：token、cookie HMAC key、Turnstile 测试密钥（.dev.vars 已 gitignore）
cp .dev.vars.example .dev.vars

# 起本地 Worker（--local 不需要 Cloudflare 账号）
npx wrangler dev --local --port 8788
# 或：npm run dev

# 建表 + 演示数据（本地 D1）
npx wrangler d1 execute personal_quant --local --file=schema.sql
npx wrangler d1 execute personal_quant --local --file=seed.sql   # 或 npm run db:seed:local
# 也支持 migrations：npx wrangler d1 migrations apply personal_quant --local
```

一键验证（服务已启动）：

```bash
cd cloudflare/worker
BASE=http://127.0.0.1:8788 QUANT_INGEST_TOKEN=<.dev.vars 里的 token> bash scripts/smoke.sh
BASE=http://127.0.0.1:8788 bash scripts/security-test.sh   # 安全验收，最后会做高频限流测试
```

手工验证人机验证流程（curl 必须带浏览器 UA，否则被反爬 403）：

```bash
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
B=http://127.0.0.1:8788
curl -s -A "$UA" -H 'Accept: application/json' $B/api/config | python3 -m json.tool
curl -s -A "$UA" -H 'Accept: application/json' -H 'content-type: application/json' \
  -c /tmp/quant.jar -X POST $B/api/verify -d '{"token":"XXXX.DUMMY.TOKEN.XXXX"}'   # 本地 mock/test key 通过
curl -s -A "$UA" -H 'Accept: application/json' -b /tmp/quant.jar $B/api/signals | python3 -m json.tool
curl -s -A "$UA" -H 'Accept: application/json' $B/api/health | python3 -m json.tool
```

## 本地验证（wrangler/workerd 跑不起来时：Android proot / 39-bit VA 等）

某些手机/受限容器里 `workerd` 一启动就 abort（TCMalloc 要求 48-bit 虚拟地址空间，日志
`MmapAligned() failed ... assumes a 48-bit virtual address space`）。这是运行时限制，不是项目代码问题。
此时用同一份 `src/` 代码的 Node 回退运行时（D1 用 `node:sqlite`，内置 Turnstile siteverify mock）：端点/安全行为与云端一致。

```bash
cd cloudflare/worker
npm run dev:node     # http://127.0.0.1:8788，自动建表 + 首次导入 seed.sql；任意 token 可完成 /api/verify
```

`wrangler.toml` 仍保持标准配置，正常机器上 `npx wrangler dev --local --port 8788` 可直接使用；
可用 `npx wrangler deploy --dry-run --outdir dist` 验证配置/打包（不需要账号）。

## 推送接口（本地 Python -> Cloudflare）

`src/quant/export_cloud.py` 会 POST 到 Worker（token 只从环境变量读）：

```bash
QUANT_INGEST_TOKEN=<token> python -m quant.cli export --target cloudflare
# 等价 curl：
curl -X POST http://127.0.0.1:8788/api/ingest \
  -H "content-type: application/json" \
  -H "x-ingest-token: $QUANT_INGEST_TOKEN" \
  -d '{
    "schema": 1, "symbol": "600519", "name": "贵州茅台",
    "asof": "2026-11-21T14:35:00+08:00", "trade_date": "2026-11-21", "price": 1680.5,
    "direction": "up", "prob": {"up": 0.42, "flat": 0.31, "down": 0.27},
    "confidence": 0.42, "horizon": "1d", "expires_at": "2026-11-24T09:30:00+08:00",
    "model_version": "v0.1.0", "status": "ok",
    "top_features": [{"name": "ret_5d", "value": 0.031, "contrib": 0.08}]
  }'
```

- 鉴权：`Authorization: Bearer <QUANT_INGEST_TOKEN>` 或 `X-Ingest-Token: <...>`；未配置 token → 503，缺失/错误 → 401。
- 反爬例外：ingest 是机器接口，不受 UA/Accept 限制（Python httpx 可直接推），但仍占用限流配额。
- 单次请求体上限 1 MB；批量部分失败返回 `accepted`/`rejected`（含每条原因）/`warnings`。

## 部署步骤（真实上线，Lead 执行）

本地 `.dev.vars` 只用于开发；生产全部用 `wrangler secret put`。命令清单：

```bash
cd cloudflare/worker

# 0) 账号
npx wrangler login                          # 或 export CLOUDFLARE_API_TOKEN=...
npx wrangler whoami

# 1) D1 数据库
npx wrangler d1 create personal_quant        # 记下返回的 database_id
# 把 wrangler.toml 里 database_id 的占位符替换成真实 id
npx wrangler d1 migrations apply personal_quant --remote

# 2) secrets（都不进 git）
npx wrangler secret put QUANT_INGEST_TOKEN   # 与本地 export_cloud.py 使用的一致
npx wrangler secret put COOKIE_KEY           # openssl rand -base64 32 生成
npx wrangler secret put TURNSTILE_SECRET     # Cloudflare Turnstile 站点 secret

# 3) Turnstile sitekey 是公开值：写 wrangler.toml [vars] 或 dashboard
#    [vars]
#    TURNSTILE_SITEKEY = "0x4AAAAAAA..."

# 4)（建议）跨 isolate 限流 KV：本地 --local 不需要
npx wrangler kv namespace create RATE_LIMIT_KV
# 把返回 id 填到 wrangler.toml 的 [[kv_namespaces]]（注释里有模板）

# 5) 发布（Worker + D1 binding + Assets/Pages 一起上线）
npx wrangler deploy

# 6) 线上验收（curl 必须带浏览器 UA/Accept；真实 secret 下 Turnstile 测试 token 不通过，需浏览器真 token）
curl -s -A 'Mozilla/5.0 (X11; Linux x86_64) Chrome/131.0.0.0' -H 'Accept: application/json' \
  https://<worker>.workers.dev/api/health | python3 -m json.tool
BASE=https://<worker>.workers.dev bash scripts/security-test.sh
```

- 看板就是 Worker 的 Assets（`[assets] directory = "../pages"`），`wrangler deploy` 一并发布，无需单独 Pages 项目。
- 真实 Turnstile key 获取：Cloudflare Dashboard → **Turnstile** → Add site → 得到 Sitekey（公开）与 Secret（保密）；本地开发继续用测试密钥即可。
- 只把公开信号数据放上云；不上传任何账户/券商凭据。仓库不提交 token（`.dev.vars` 已 gitignore）。
- 可选：自定义域名 / 只允许自己访问（Cloudflare Access）也可在 dashboard 配，不属于 Worker 代码。

## 环境变量

| 变量 | 来源 | 说明 |
|---|---|---|
| `QUANT_INGEST_TOKEN` | `.dev.vars` / `wrangler secret put` | **必需**才有 ingest 能力；只从 env 读，代码不硬编码、不回显 |
| `INGEST_TOKEN` | 同上 | 兼容旧名（优先级低于 `QUANT_INGEST_TOKEN`） |
| `COOKIE_KEY` | `.dev.vars` / `wrangler secret put` | **必需**才有 /api/verify；quant_pass HMAC-SHA256 密钥 |
| `TURNSTILE_SITEKEY` | `[vars]` / secret | 公开 sitekey；缺省用测试 key（仅本地） |
| `TURNSTILE_SECRET` | `wrangler secret put` | Turnstile 校验 secret；缺省用测试 secret（生产必须换） |
| `RATE_LIMIT_PER_MIN` | var | 每 IP 每分钟限流，默认 60 |
| `RATE_LIMIT_KV` | `[[kv_namespaces]]` binding | 可选；绑定后用 KV 跨 isolate 限流，否则内存 Map |
| `TURNSTILE_VERIFY_URL` | 仅测试 | 覆盖 siteverify 地址（本地 mock），生产不要设置 |
| `WORKER_VERSION` | 可选 var | 覆盖 `/api/health` 版本号 |

`.dev.vars` / `.dev.vars.*` 已加入 `cloudflare/worker/.gitignore`；示例见 `.dev.vars.example`。

## 模型双端推理 parity（契约 §5 / §10）

```bash
cd cloudflare/worker
node test/parity.mjs            # 无需 npm install；node 24 原生跑 TS
node scripts/check-bundle.mjs   # esbuild 打包自检（验证 Worker 可构建，不启动 workerd）
npm run sync-model              # data/models/model.json 就绪后同步到 worker/models/model.json
```

自动配对并**逐组跑所有可用配对**（任何一组超差都失败）：

| 优先级 | model | cases | 说明 |
|---|---|---|---|
| 1 | `tests/fixtures/model/model.json` | `tests/fixtures/model/parity_vectors.json` | model-dev 固定 fixture（契约 §10 官方对照） |
| 2 | `data/models/model.json` | `tests/fixtures/model/parity_cases.json` | 真实训练产物 + 同名用例（训练后可用） |
| 3 | `data/models/model.json` | 自动合成输入 | 真实模型无用例时，按 mean/scale 生成确定性输入，Python 算期望值 |
| 4 | `worker/models/model.json` | `worker/test/fixtures/parity_cases.json` | 自造 §5 占位模型（期望值由 Python 算出） |

- 用例没有 expected 时调用本地 Python：优先 `quant.model.predict.predict_proba`（PYTHONPATH=src），不可用则回退 Python 内联的 §5 公式实现；两者都是「Python vs TS」独立对照。
- 容差默认 1e-6（`PARITY_TOL` 可覆盖），同时断言 `prob` 和为 1；失败打印每组每个 case 的 Δ 与期望值，退出码 1。
- 支持 fixture 形态：数组 / `{cases|items|inputs|vectors:[]}`；特征可以是 dict，也可以是按 `model.features` 顺序的数组；expected 别名 `expected|expected_proba|proba|probabilities|output`。用例自带 `features` 时自动找同目录匹配的 `model.json`。
- 真实模型出现后：`npm run sync-model` 把 `data/models/model.json` 同步给 Worker（`GET /api/model` 可直接核对 Worker 打包的权重版本），再跑 `node test/parity.mjs`。
  同步真实模型后，自造占位 fixture 会因 `model_version` 不一致被自动跳过（不会拿旧 fixture 套新权重造成假失败）；真实模型改走 `trained model + synthesized cases` 与 Python `predict_proba` 对照。

## 故障排查

| 现象 | 处理 |
|---|---|
| `workerd` 启动即 abort，日志含 `TCMalloc assumes a 48-bit virtual address space` | 当前机器 VA 空间不足（Android/proot 常见）；用 `npm run dev:node` 本地验证，正常机器上用 wrangler |
| curl 直接请求返回 403 | 反爬生效：加浏览器 UA + Accept；`curl -A 'Mozilla/5.0 ...' -H 'Accept: application/json'` |
| `/api/signals` 返回 401 `challenge:true` | 先 `GET /api/config` → Turnstile → `POST /api/verify` 拿 `quant_pass` cookie（本地测试 key 任意 token 即通过） |
| `POST /api/verify` 返回 503 `server_misconfigured` | 没配 `COOKIE_KEY`：本地写 `.dev.vars`，部署 `wrangler secret put COOKIE_KEY` |
| 大量 429 | 触发限流；`Retry-After` 秒后重试 / 调大 `RATE_LIMIT_PER_MIN` / 部署绑定 `RATE_LIMIT_KV` |
| ingest 返回 503 `ingest_token_not_configured` | `.dev.vars` 写 `QUANT_INGEST_TOKEN=...` 后重启 dev；部署则 `wrangler secret put` |
| ingest 返回 401 | 检查 `Authorization: Bearer` / `X-Ingest-Token` 与 `.dev.vars` 一致（本地重启后生效） |
| `/api/signals` 空数组 | 先 `npm run db:seed:local` 或推一条真信号；`/api/health` 看 `d1` 状态 |
| 本地 D1 状态想重置 | 删 `cloudflare/worker/.wrangler/state`（wrangler）或 `.wrangler/local-dev/`（dev:node） |
| parity 用了占位模型 | `python -m quant.cli train` 产出 `data/models/model.json` 后 `npm run sync-model`，再跑 parity |

## 免责声明

仅供研究学习，输出不构成任何投资建议。市场有风险，决策自负。
