# 前端看板（本地 FastAPI + Cloudflare Pages）

个人量化工作台的两套前端产物，**同一套设计语言、同一份实现**：

| 目录 | 用途 | 数据来源 |
|---|---|---|
| `frontend/` | 本地看板，由 FastAPI 挂载在 `http://127.0.0.1:8787/` | `/api/signals`、`/api/paper`、`/api/equity`、`/api/backtest`，60s 轮询 |
| `cloudflare/pages/` | Cloudflare Pages 产物，由 Worker 静态挂载到 `/` | `/api/signals` + `/api/stream`（SSE），断线指数退避重连 |

设计规范见 `docs/DESIGN.md`（硬性），本目录只做实现，不改契约。

## 文件

```
frontend/
  index.html   结构（含内联 SVG 图标 sprite、Turnstile 验证覆盖层、免责声明 dialog）
  styles.css   全部视觉与动效（深色终端、A 股红涨绿跌、10px 圆角、响应式）
  app.js       数据适配 / 渲染 / rAF 补间 / FLIP / SVG 净值曲线 / 轮询 / SSE / Turnstile
cloudflare/pages/
  index.html   同构，仅 window.QT_MODE="cloud" 与少量文案不同
  styles.css  ┐ 与 frontend/ 下两个文件逐字节一致（diff 为空）
  app.js      ┘
```

同步命令（改完本地版后执行）：

```bash
cp frontend/styles.css frontend/app.js cloudflare/pages/
# index.html 需要保留两处差异：
#   <html data-mode>、<title>、window.QT_MODE、brand-sub、页脚数据来源、foot.mode 文案
```

## 预览

```bash
# 1) 本地看板（真实数据；worker 未启动也可用）
python -m quant.cli serve --port 8787
#    → http://127.0.0.1:8787/

# 2) 纯静态预览（无 API，用于看版式/断网降级）
cd frontend && python -m http.server 8080

# 3) Cloudflare 云端看板（本机 workerd 不可用时的 node 回退，端口 8788）
npm --prefix cloudflare/worker run dev:node
#    → http://127.0.0.1:8788/   （静态文件来自 cloudflare/pages/）
```

## 数据契约与容错

- 信号：契约 §4 JSON。`/api/signals` 兼容 数组 / `{items}` / `{signals}` / 单对象；`prob` 兼容嵌套或 `prob_up/flat/down`；`top_features` 兼容 JSON 字符串。
- 模拟盘：`{cash, equity, market_value, today_pnl, total_return, win_rate, positions, orders, account.initial_cash, as_of}`；`win_rate` 为 null 时显示"—/无平仓记录"。
- 净值：`[{ts,cash,market_value,equity}]`，按时间升序去重。
- 回测：契约 §12 的 `meta / direction / win_rate / benchmarks`；缺失（404）时渲染"未回测 + 命令提示"空态。
- Cloudflare 只有信号端点，模拟盘/回测/净值在云端渲染为"本地生成"空态，不报错。
- 请求失败：保留上次成功数据，状态条转为"部分接口异常/连接断开"；401 触发人机验证；429 显示限流倒计时。

## 动效实现（DESIGN.md §2 逐条落地）

| 场景 | 实现 | 时长 / 曲线 |
|---|---|---|
| 数字补间（价格/概率/净值/收益） | rAF + 三次贝塞尔求值（与 CSS 同曲线），逐帧只写 textContent | 480ms / `cubic-bezier(.22,1,.36,1)` |
| 列表更新 FLIP | 先量旧 `getBoundingClientRect`，改序后反位移再 WAAPI 归零；新行 -6px 滑入 | 240ms |
| 净值 SVG 描边生长 | `getTotalLength()` + `stroke-dasharray/dashoffset` WAAPI（px 单位）；面积淡入 | 700ms ease-out |
| 概率条 / 置信度 / 特征条 | `transform: translateX+scaleX` / `scaleX`，不触发布局 | 240ms |
| 卡片首次进入 | CSS `translateY(8px)+opacity`，stagger 40ms，只播一次 | 340ms |
| 骨架屏 | 低对比 shimmer（transform 位移，1.2s 循环），数据到达即替换 | 1.2s |
| 状态色切换 | 方向灯/标签 `background-color/color` | 220ms |
| hover / 按下 | 背景/边框/文字色 | 140ms |

- 只动 `transform` / `opacity`（净值描边按规范例外用 dashoffset）；无 width/height 过渡。
- `prefers-reduced-motion: reduce`：CSS 全部动画/过渡降到 0.01ms，JS 中补间直接落值、FLIP/描边/抖动/打勾动画全部跳过。
- 页面隐藏（`visibilitychange`）暂停轮询、关闭 SSE；回到前台立即刷新并重连。
- 轮询/SSE 只 patch 变化 DOM：表格按 `signal_id` 复用 `<tr>`，卡片按字段比较，回测/净值按签名比较，避免整块重绘闪烁。

## 人机验证（Cloudflare Turnstile，公网部署必需）

流程（两个前端同一实现）：

1. 启动时 `GET /api/config` 读 `turnstile_sitekey`（兼容 `sitekey` / `turnstileSitekey` / `turnstile.sitekey`）；读不到 → **开发模式**：不加载验证组件，直接请求数据，顶栏显示"开发模式"虚线徽标。
2. 数据/SSE 请求返回 `401 + {"challenge":true}` → 打开验证覆盖层（淡入 200ms，`role=dialog aria-modal`）。
3. 懒加载 `https://challenges.cloudflare.com/turnstile/v0/api.js`（**唯一外部资源，安全白名单例外**；不阻塞首屏、失败有重试）。
4. 成功回调拿 token → `POST /api/verify {"token":...}`（`credentials:"same-origin"`）→ Worker 下发 `quant_pass` HttpOnly Cookie → 关闭覆盖层（打勾描边 240ms）→ 重试原请求 / 重连 SSE。
5. 校验失败：只抖动一次（±3px，240ms），重置组件可重试；429 显示"稍后重试"。
6. 数据请求 429：顶部细提示条 + 秒级倒计时，倒计时内暂停轮询，结束自动恢复。

> 白名单建议（`docs/DESIGN.md` §4 由 Lead 维护）：
> "**唯一例外**：Cloudflare Turnstile 官方脚本 `https://challenges.cloudflare.com/turnstile/v0/api.js`，仅在收到验证挑战时懒加载；除此外无任何在线字体/图标/CDN。"

## 无障碍与降级

- 语义化 `<header>/<main>/<section>/<table>/<dialog>`；跳转链接；`aria-live="polite"` 播报连接状态与新信号；方向灯 `role=img` 概率条带文字说明。
- 键盘可操作：标的切换 `role=tablist` + ←/→、校准点可 Tab 聚焦看 tooltip、`focus-visible` 2px accent 描边。
- 断网/纯静态：样式、字体栈、图标全部内置，除 Turnstile 外零外部请求；无 API 时显示命令提示空态与"开发模式"徽标。
- 对比度：正文 `#E6EDF7`/`#9AA7BA` 对 `#11161F` ≥ 4.5:1；方向色仅用于数据语义，不单独承载信息（同时有文字）。

## 自测记录（frontend-dev，2026-11-21）

```text
$ node --check frontend/app.js && node --check cloudflare/pages/app.js
OK（两文件语法通过）

$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/
200（FastAPI 挂载看板；/api/signals /api/paper /api/equity /api/backtest 均 200/404 契约正常）

$ python3 -m http.server（frontend/ 纯静态）  →  GET / = 200

$ grep -oE 'https?://[^"'\'' )]+' frontend/app.js frontend/index.html ...
唯一外链：https://challenges.cloudflare.com/turnstile/v0/api.js（安全白名单）；
其余 http://127.0.0.1 仅为空态提示中的本地预览命令，无 CDN/字体/图标库。

$ 外部资源扫描：无 <link href="http...">、无 @import、无 iconfont、无 Google Fonts。
$ emoji 扫描（Unicode Emoji 区段）：0 处；图标全部为内联 SVG line icon。
$ 紫渐变/玻璃拟态扫描：无 purple/violet 色值、无 backdrop-filter、无发光边框；
  唯一渐变是净值面积填充（红/绿/灰，随涨跌变化，透明度 ≤0.14）。
$ prefers-reduced-motion：styles.css 媒体查询 + app.js matchMedia 分支齐全；
  headless --force-prefers-reduced-motion 截图：内容完整、净值线立即完整显示。

$ 宽度适配（Chromium headless 实测 document.scrollingElement）：
390  → scrollWidth=390, clientWidth=390  （SW=CW，无横向滚动）
1440 → scrollWidth=1440, clientWidth=1440
390 截图逐项检查：单列堆叠、指标 2 列、信号表（名称并入代码行）、订单/持仓表列宽不裁切、
                  顶栏隐藏次要信息、说明文字可读。

$ Turnstile 联调（mock 401 → 验证 → cookie → 重试）：
- 真实 Worker（node 回退，:8788）：GET / → 200，GET /api/signals → 401 challenge，
  Turnstile 测试 sitekey 通过后 POST /api/verify → 200 + Set-Cookie，GET /api/signals → 200；
  覆盖层在验证通过后隐藏。
- 验证失败（403）：覆盖层保持打开、错误行提示"验证未通过，请重新完成验证"、组件可重置重试。
- 429：顶部黄色细条 + 倒计时（Retry-After=12 → 显示 12s 递减），倒计时结束自动恢复轮询。
- 断线重连：SSE 503 场景实测请求间隔 1s→2s→4s→8s（指数退避），状态条"重连中"→"连接断开"。

$ 数据刷新稳定性：60s 轮询 / SSE 推送只 patch 变化节点；
  相同数据重复渲染为 no-op（签名比较），不会重排闪烁。
```

## 截图占位

<!-- 本地生成截图后放这里（建议不提交大图，或压缩后提交）：
![桌面 1440](screenshots/desktop-1440.png)
![移动 390](screenshots/mobile-390.png)
![人机验证](screenshots/turnstile-gate.png)

生成方式（本机 Chromium）：
chromium --headless --window-size=1440,1600 --screenshot=desktop-1440.png http://127.0.0.1:8787/
chromium --headless --window-size=390,3200  --screenshot=mobile-390.png   http://127.0.0.1:8787/
-->

## 已知限制

- Turnstile 是唯一在线依赖：完全断网且 Worker 已开启验证时无法通过验证（这是安全网关的设计，前端会给出网络/重试提示）；本地 FastAPI 无验证时不受影响。
- 云端看板不回传模拟盘/回测数据（Worker 只存信号），对应区块显示"本地生成"空态。
- `/api/backtest` 报告较大（含 trades_detail）时首屏解析约几十毫秒；只读取展示所需字段。
