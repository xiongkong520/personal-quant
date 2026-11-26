# 本地数据服务 + 公网隧道（LOCAL SERVER）

> 定位：**实时行情走本地 Python 服务，Cloudflare 只做静态托管/快照兜底**。
> 本地服务只读公开行情源（腾讯 / 新浪），无写接口、无账号、无付费数据源；仅供研究学习，不构成投资建议。

- 代码：`src/quant/local_server.py`（FastAPI，默认 `127.0.0.1:8899`）
- 隧道：`scripts/tunnel.sh`（cloudflared quick tunnel，免费 `*.trycloudflare.com`）
- 前端数据源切换：规划见下文「§7 前端数据源切换」（`localStorage.quant_api_base`）

---

## 1. 为什么是「本地服务 + 前端」

`docs/REFERENCE.md`（2026-11 调研结论）：

- **腾讯** `qt.gtimg.cn`：CORS 开放、批量报价；`web.ifzq.gtimg.cn` 当日分时本机实测可用；
- **新浪**：行情列表分页（全市场约 5580 只）、行业板块接口可用；
- **东财** push2：本机出口可用，**Cloudflare 出口被限流/断连** → 实时抓取只能放本地/离线；
- 免费源的「秒级」上限就是客户端 **3 秒轮询**（easyquotation 同款思路），真 tick 需付费 Level-2。

因此：**本地 FastAPI 直连数据源（本机出口）→ 前端轮询本地/隧道地址**，
云端 Worker 保留静态页 + 快照/历史/信号兜底。手机 proot 环境同样可跑（纯 Python + 一个静态二进制隧道）。

社区参考（动手前已搜，参考实现）：

- Cloudflare Quick Tunnels 官方文档：<https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/>
- 九章·ALGOR（本地多智能体量化终端，`server.py` 聚合多源 + 腾讯批量行情解析同款思路）：<https://github.com/Ksyniko/jiuzhang-algor>
- dsh-stock-terminal（可靠性设计：轮询防竞态/请求去重/短 TTL 缓存/故障降级）：<https://github.com/linhut/dsh-stock-terminal>
- a-stock-data（零依赖直连多数据源）：<https://github.com/simonlin1212/a-stock-data>

---

## 2. 快速开始（本机 / 手机 proot）

```bash
cd /root/workspace/personal-quant
python -m quant.local_server              # → http://127.0.0.1:8899  (等价于 python -m quant.local_server --port 8899)
# 或指定监听地址（0.0.0.0 供同局域网设备访问；公网隧道不需要）
python -m quant.local_server --host 0.0.0.0 --port 8899
```

- 依赖：`fastapi` / `uvicorn` / `httpx`（项目 `requirements.txt` 已包含）；
- 交互文档：<http://127.0.0.1:8899/docs>；根路径 `/` 返回服务信息 JSON；
- 健康检查：`curl -s http://127.0.0.1:8899/api/health`。

> 注意：本服务**不读写** `data/db/quant.duckdb`、不参与模拟盘，只做行情转发 + 缓存；
> 历史 K 线/信号/回测/模拟盘仍由 `python -m quant.cli serve --port 8787`（或云端）提供。

### 实测样例（2026-11-26 盘后）

```console
$ curl -s 'http://127.0.0.1:8899/api/health'
{"schema":1,"ok":true,"service":"personal-quant-local","version":"1.0.0",
 "time":"2026-11-26T21:21:37+08:00","pid":29384,"uptime_sec":905.3,
 "cache":{"entries":7,"alive":0}, ...}

$ curl -s 'http://127.0.0.1:8899/api/quote?codes=sh000001,sz399001,600519'
{"schema":1,"ok":true,"source":"qt.gtimg.cn","codes":["sh000001","sz399001","sh600519"],
 "count":3,"items":[
   {"code":"sh000001","name":"上证指数","price":3476.88,"prev_close":3476.47,
    "open":3490.14,"high":3497.81,"low":3455.04,"change":0.41,"pct_chg":0.01,
    "amount":613332810452.0,"volume":454062959,"time":"2026-11-26 16:14:27"},
   ...],"missing":[],"updated_at":"2026-11-26T21:21:37+08:00","degraded":false}

$ curl -s 'http://127.0.0.1:8899/api/intraday?code=sh000001'
{"schema":1,"ok":true,"source":"web.ifzq.gtimg.cn","code":"sh000001",
 "trade_date":"2026-11-26","updated_at":"...","stale":false,"count":242,
 "data":{"code":"sh000001","name":"上证指数","trade_date":"2026-11-26","pre_close":3476.47,
   "points":[{"time":"09:30","price":3490.14,"avg_price":null,"volume":4730979}, ... 242 点 ...]}}

$ curl -s 'http://127.0.0.1:8899/api/market-all?page=1&num=20'
{"schema":1,"ok":true,"source":"sina_market_center","node":"hs_a","page":1,"num":20,
 "count":20,"total":5580,"items":[{"symbol":"bj920000","code":"920000","name":"安徽凤凰",
 "price":8.98,"pct_chg":-1.319,"amount":3798844.0,"turnover":0.72521, ...}], ...}

$ curl -s 'http://127.0.0.1:8899/api/board?limit=3'
{"schema":1,"ok":true,"source":"sina_industry","type":"industry","count":3,
 "items":[{"code":"new_mthy","name":"煤炭行业","count":41,"pct_chg":2.4744,
   "amount":11753800206.0,"leader":{"code":"sh600397","name":"江钨装备","pct_chg":7.765,"price":11.38}}, ...]}
```

---

## 3. 接口契约

所有响应带 `schema` / `ok`；上游失败返回 **明确错误**（不编造数据）：

```json
{"schema":1,"ok":false,"error":"data_unavailable","source":"qt.gtimg.cn",
 "message":"qt.gtimg.cn 请求失败: ...","updated_at":"..."}   // HTTP 502/503
```
参数非法返回 `{"ok":false,"error":"invalid_params","message":"..."}`（HTTP 400）。

| 端点 | 参数 | 上游 | 缓存 | 说明 |
|---|---|---|---|---|
| `GET /api/health` | — | 无（不访问上游） | — | `{ok,time,service,version,pid,uptime_sec,cache,endpoints}` |
| `GET /api/quote` | `codes`（逗号分隔，支持 `600519` 自动转 `sh600519`；≤60 个） | 腾讯 `qt.gtimg.cn`（GBK，50 个/批） | **2s** | 实时价/涨跌幅/成交额；停牌/无报价进 `missing`，不编造 |
| `GET /api/intraday` | `code`（指数：`sh000001`/`sz399001`/`sz399006`/`sh000300` 等） | 腾讯 `web.ifzq.gtimg.cn/appstock/app/minute/query` | **5s** | 当日 242 点；`data.points[{time,price,avg_price,volume}]`，对齐云端 `/api/intraday` 形状 |
| `GET /api/market-all` | `page`(≥1)、`num`(20–100)、`node`(hs_a/sh_a/sz_a/bj_a)、`sort`、`asc` | 新浪 `Market_Center.getHQNodeData` + `getHQNodeStockCount` | **60s** | 全市场分页（`total`≈5580）；字段：symbol/code/name/price/pct_chg/amount/turnover/… |
| `GET /api/board` | `limit`(1–100)、`sort`(pct_chg/amount) | 新浪 `newSinaHy.php`（GBK） | **60s** | 行业板块涨幅榜 + 领涨股（leader） |

字段口径：

- `amount` = 元；`volume` = 手（腾讯报价接口口径）；
- 分时 `volume` 为**单分钟增量**（服务端由累计量差分），另给 `cum_volume`/`cum_amount` 便于扩展；
- `pct_chg` = 涨跌幅百分比（`2.35` 表示 +2.35%）；指数 `avg_price` 为 `null`（指数均价无意义），个股按 VWAP 计算；
- `stale=true` 表示分时交易日不是今天（收盘后仍可查看当日）。

---

## 4. 数据源与缓存

| 数据 | 数据源 | TTL | 说明 |
|---|---|---|---|
| 指数/个股实时 | `qt.gtimg.cn`（GBK） | 2s | 批量，50 个/请求；指数、个股、ETF 均可 |
| 分时 | `web.ifzq.gtimg.cn` | 5s | 当日 242 点（09:30–15:00），收盘后仍可取当日 |
| 全市场列表 | 新浪 `Market_Center` | 60s | 分页 20–100/页；总数接口单独缓存 300s |
| 行业板块 | 新浪 `newSinaHy.php` | 60s | 行业（概念板块后续可加） |

缓存实现：进程内 `TTLCache`（线程锁保护），返回体带 `"cached": true/false` 便于排查。
免费接口失败重试一次（0.25s 退避）后抛错，避免放大限流。

---

## 5. 公网隧道（cloudflared quick tunnel，免费）

```bash
# 1) 先起本地服务
python -m quant.local_server --port 8899

# 2) 另开终端起隧道（首次会自动下载 cloudflared 二进制到 ~/.cache/quant-tunnel/）
bash scripts/tunnel.sh
```

脚本行为：

1. 检查 `http://127.0.0.1:8899/api/health`（未启动直接提示先启动服务）；
2. 找/下载 cloudflared：`QUANT_CLOUDFLARED` 指定 → PATH → 缓存 → 下载；
   下载策略：`aria2c` 多线程 GitHub（快）→ **Cloudflare 官方 apt 仓库 `.deb` 解包**（`pkg.cloudflare.com`，本机实测约 1MB/s，不安装系统包）→ curl 单线程 GitHub 兜底；
3. `cloudflared tunnel --url http://127.0.0.1:8899 --no-autoupdate`；
4. 从日志提取 `https://<随机词>.trycloudflare.com` 打印并写入 `/tmp/quant-tunnel-url.txt`；
5. 公网自检 `/api/health`（trycloudflare 首次 DNS/边缘生效约 10–60s，属正常）。

实测输出（2026-11-26）：

```console
[tunnel] 本地服务健康检查通过: http://127.0.0.1:8899/api/health
[tunnel] 使用缓存二进制: /root/.cache/quant-tunnel/cloudflared
==================================================================
 本地数据服务公网隧道已建立（trycloudflare 免费临时域名）
   本地服务 : http://127.0.0.1:8899
   公网地址 : https://generations-armstrong-women-graphics.trycloudflare.com
   健康检查 : https://generations-armstrong-women-graphics.trycloudflare.com/api/health
   行情示例 : https://generations-armstrong-women-graphics.trycloudflare.com/api/quote?codes=sh000001,sz399001
==================================================================
[tunnel] 公网自检通过
```

公网实测：

```console
$ curl -s 'https://generations-armstrong-women-graphics.trycloudflare.com/api/quote?codes=sh000001,sz399001,sz399006'
HTTP 200
{"schema":1,"ok":true,"source":"qt.gtimg.cn","count":3,
 "items":[{"code":"sh000001","name":"上证指数","price":3476.88,"pct_chg":0.01,
           "amount":613332810452.0}, {"code":"sz399001", ...}, {"code":"sz399006", ...}]}
```

### 后台常驻 / 开机自启（手机 proot 无 systemd）

```bash
# 后台跑隧道（URL 会写进 /tmp/quant-tunnel-url.txt）
nohup bash scripts/tunnel.sh > /tmp/quant-tunnel.log 2>&1 &
grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/quant-tunnel.log | head -1

# 后台跑服务
nohup python -m quant.local_server --port 8899 > /tmp/quant-local-server.log 2>&1 &

# 停止
pkill -f 'quant.local_server'        # 服务
pkill -f 'cloudflared tunnel --url'  # 隧道
```

开机自启（proot / Android 上无 systemd，任选其一）：

- 放到 `~/.bashrc` / 登录脚本末尾（本机每次打开 Ubuntu 会话时拉起的场景）；
- 用 `tmux`/`screen` 常驻会话；
- Android 用户可用 Termux:Boot + 一个启动脚本调用上面的 nohup 命令（需 App 支持自启）。

注意事项：

- **免费临时域名每次启动都会变**；要固定域名需 Cloudflare 账号 + Named Tunnel（不在本任务范围）；
- 公网 URL 是「谁拿到都能读行情」的只读地址：本项目无鉴权需求，但**不要**把带隐私/密钥的服务通过 quick tunnel 暴露；
- 不用时 `Ctrl-C` 或 kill 掉，避免长期占用连接与免费额度。

---

## 6. 与 Cloudflare 版的分工

| 能力 | 本地服务（本文件，:8899） | `quant.cli serve`（:8787，本地历史/模拟盘） | Cloudflare Worker/Pages |
|---|---|---|---|
| 指数/个股实时报价 | ✅ 腾讯秒级（缓存 2s） | ❌ | ✅ 快照/缓存（分钟级） |
| 当日分时 | ✅ 腾讯 242 点 | ❌ | ✅ 东财 trends2（边缘缓存） |
| 全市场列表 | ✅ 新浪分页 | ❌ | ✅ 静态快照 `market_all.json` |
| 板块 | ✅ 新浪行业 | ❌ | ✅ market-overview sectors |
| K 线/信号/回测/模拟盘 | ❌ | ✅（DuckDB/Parquet） | ✅（D1 快照/SSE） |
| 历史/离线研究 | ❌ | ✅ | 快照兜底 |

结论：**前端默认用 CF（可用性好、无需本地环境）；配置了本地隧道地址后，指数条/分时/全市场/板块切到本地秒级源，失败自动回退 CF。**

---

## 7. 前端数据源切换（已实现，2026-11-26）

`frontend/` 与 `cloudflare/pages/` 两份 `app.js` / `styles.css` 字节一致，实现（最小侵入）：

1. `localStorage.quant_api_base` 保存本地服务地址（默认空 = 全部走 CF）；
   `getJSON(path, options)` 支持 `options.base` 覆盖；`marketGet(path)` 只把「行情类请求」拼到本地 base；
2. 生效范围（本地 base 非空时）：
   - 指数条：`/api/quote` 3 秒轮询（云端快照情绪/涨跌家数仍从 `/api/market` 补齐，缺失不影响指数条）；
   - 今日分时：`/api/intraday`（本地 payload 与云端同形状，`normIntradayPayload` 直接复用）；
   - 全市场表格：**仍用云端快照** `/api/market-all`（见下「决策」），CF 失败时才用本地分页首屏 100 只兜底；
   - 板块榜：市场全景优先用云端 regime 快照；缺失时用本地 `/api/board` 的涨跌幅兜底（无资金流字段显示「—」）；
   - signals / paper / kline / backtest / regime / stream 等：始终走云端，不受本地 base 影响；
3. 设置页（`#/settings`）：本地服务地址输入框 + 保存并启用 / 测试连接 / 恢复云端 + 当前生效地址与连接状态；地址只存本机 localStorage；
4. 本地不可用（服务/隧道挂了/超时）时自动回退云端，不阻塞首屏；
5. 顶部指数条下方「3 秒直连」逻辑同时支持两种来源：本地 base（走 `/api/quote`）或浏览器直连腾讯（CORS）。

**决策：全市场表格不走本地分页。** 原因：本地 `/api/market-all` 是新浪分页接口（每页 ≤100 只、全市场约 5580 只需 56 次串行请求），而表格/搜索需要一次性全量数组；若逐页拉取会明显加重新浪限流风险且首屏变慢。因此表格继续用云端静态快照（60s 缓存、含全量数据），本地分页仅作为 CF 快照失败时的首屏兜底（100 只）。

验收截图（CDP，本地页面 + `quant_api_base=http://127.0.0.1:8899`）：

- `/tmp/pq19/desktop/ds-dashboard-local.png`（指数条：上证 3476.88 +0.01% 6133亿 等本地实时值）
- `/tmp/pq19/desktop/ds-settings.png`（设置页：输入框/测试连接成功 v1.0.0 22ms/保存/恢复云端）
- `/tmp/pq19/desktop/ds-market-intraday.png`（今日分时：242 点、2026-11-26、昨收 3476）
- 手机 390 宽：`/tmp/pq19/mobile/ds-*.png`；控制台 0 error；切换「恢复云端」后 localStorage 清空。

---

## 8. 故障排查

| 现象 | 原因/处理 |
|---|---|
| 启动报 `Address already in use` | 端口被占用：换 `--port`，或 `pkill -f quant.local_server` |
| `api/quote` 502 | 腾讯接口不可达/限流：稍后重试；检查 `curl -s https://qt.gtimg.cn/q=sh000001` |
| `api/market-all` 502 | 新浪限流：降低请求频率（服务端已 60s 缓存），换 `node=sh_a` 缩小范围 |
| `api/intraday` 400 | 只支持指数代码（`sh000001`/`sz399001`/`sz399006`/`sh000300`…）；个股分时暂未开放 |
| 隧道公网 502/000 | trycloudflare 首次生效慢（10–60s）或临时抖动：等几秒重试；日志 `tail -f /tmp/quant-cloudflared.log` |
| 下载 cloudflared 很慢 | 安装 `aria2c`（`apt-get install -y aria2`）走多线程；脚本会自动改用官方 `.deb` 兜底 |
| `Cannot determine default configuration path` | cloudflared 的无害提示（quick tunnel 不需要配置文件） |

---

## 9. 验收记录（2026-11-26）

```bash
# 本地 4 接口
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8899/api/quote?codes=sh000001,sz399001'   # 200
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8899/api/intraday?code=sh000001'           # 200（242 点）
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8899/api/market-all?page=1&num=100'        # 200（total 5580）
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8899/api/board?limit=20'                    # 200
# 隧道
bash scripts/tunnel.sh      # 打印 trycloudflare URL，自检 /api/health 通过
curl -s -o /dev/null -w '%{http_code}\n' "$URL/api/quote?codes=sh000001,sz399001"                      # 200
```

> 前端「设置本地地址 → 指数条/分时走本地」已实现并截图验收（见 §7）：
> 本地页面 `http://127.0.0.1:8787/` + `localStorage.quant_api_base=http://127.0.0.1:8899`，
> 指数条数值与本地 `/api/quote` 一致、分时为本地 242 点、设置页「测试连接」成功；
> 云端 Pages 页面只需部署本次前端改动即可获得同一能力（Pages 静态 HTML 无 CSP 限制）。
