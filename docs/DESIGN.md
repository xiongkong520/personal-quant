# 前端设计规范（DESIGN.md v1，硬性）

> 目标：**专业金融终端感 + 丝滑动效**。任何一眼能看出"AI 生成的模板"的实现都算不合格。

## 0. 反 AI 味清单（硬性禁止）

- ❌ 紫蓝渐变、玻璃拟态毛玻璃卡片堆叠、发光边框
- ❌ 居中大标题 + emoji 列表 + "✨ 智能赋能" 式营销文案
- ❌ 圆角 ≥20px 的"AI SaaS 模板"卡片、呼吸光晕
- ❌ emoji 当图标（🚀📊💡）；图标只用内联 SVG 线条或 CSS 形状
- ❌ 满屏粒子、光斑、噪点背景动画
- ❌ 弹跳过头的动效（禁用 cubic-bezier 过冲 > 1.1）、自动播放炫技动画
- ❌ 在线加载 Google Fonts / iconfont / CDN（断网必须能开）
- ⚠️ 克制：一屏最多一个强调色区块；动画只服务于"状态变化可感知"，不炫技

## 1. 视觉语言

**主题**：深色为主，浅色可选（同一套 CSS 变量切换）。
**A股配色**：红涨绿跌（中国习惯）—— 涨 `#FF4D4F`，跌 `#22C55E`，平 `#8A94A6`。

```css
:root {
  --bg-0: #0B0E13;      /* 页面底 */
  --bg-1: #11161F;      /* 卡片 */
  --bg-2: #1A2130;      /* 悬浮/表头 */
  --border: #232C3B;
  --border-strong: #344054;
  --text-0: #E6EDF7;    /* 主文字 */
  --text-1: #9AA7BA;    /* 次文字 */
  --text-2: #5C6B80;    /* 弱文字/占位 */
  --up: #FF4D4F;
  --down: #22C55E;
  --flat: #8A94A6;
  --accent: #4C8DFF;    /* 交互强调，克制使用 */
  --warn: #F5A623;
  --radius: 10px;       /* 卡片 10px，按钮 8px，标签 6px */
  --font-num: ui-monospace, "SF Mono", "JetBrains Mono", "Cascadia Mono", monospace;
}
```

**字体**：系统栈 `-apple-system, "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif`；**所有数字用 `font-variant-numeric: tabular-nums`**，价格/概率/净值用 `--font-num`。
**间距**：4 的倍数（4/8/12/16/24/32）；页面边距 16（移动）/24（桌面）。
**信息密度**：表格行高 36px（移动 32px）；卡片内边距 16；不要大留白空洞。
**布局**：桌面 12 列（主区 7 + 侧栏 5，或 4 列指标卡 + 主图）；移动端单列，指标 2 列。

## 2. 动效规范（丝滑的关键）

| 场景 | 时长 | 缓动 |
|---|---|---|
| hover / 按下 / focus | 120–160ms | `cubic-bezier(0.2, 0, 0, 1)` |
| 状态切换（方向灯、标签变色） | 200–240ms | `cubic-bezier(0.22, 1, 0.36, 1)` |
| 概率条宽度 | 240ms | 同上 |
| 数字补间（价格/净值/概率） | 400–600ms | 同上，用 rAF 补间，不用 CSS transition 数字 |
| 净值曲线描边生长 | 600–800ms | `ease-out` |
| 卡片进入（首次加载） | 300–400ms | stagger 40ms，translateY(8px)→0 + opacity |
| 列表更新（FLIP） | 240ms | 位置/尺寸变化用 transform |

规则：
- 只用 `transform` / `opacity` 做动画，避免触发布局；60fps 是验收线。
- **数据更新不整页刷新**：轮询/SSE 只 patch 变化的 DOM；数字用 rAF 从旧值补间到新值；旧值淡出、新值淡入，避免跳变。
- 加载态用骨架屏（skeleton shimmer 1.2s 循环，低对比），不要 spinner 转圈大图标。
- `@media (prefers-reduced-motion: reduce)` 时关闭所有动画（duration 0.01ms）。
- 不要用 animation 做持续循环装饰（除骨架屏 shimmer）。

## 3. 组件规格

- **方向灯**：12px 圆点 + 中文方向词（看涨/震荡/看跌），圆点颜色对应 `--up/--flat/--down`，切换时 crossfade + scale 0.98→1。
- **概率三分条**：单条水平堆叠（up/flat/down 三段），段宽 = 概率，标签在条下方（百分比 + 颜色）。
- **置信度**：细进度条 + 数字；< 0.4 显示"低置信"灰色标签。
- **特征贡献**：横条列表，贡献值正红负绿（A股色），数值右对齐等宽。
- **净值曲线**：SVG 手写 path；网格线极淡；`stroke-dasharray` 生长动画；hover 显示十字线 + tooltip（跟手，不抖）。
- **校准图**：散点 + 45° 参考线（虚线），点太小要能 hover。
- **回测指标卡**：label 小号 `--text-1`，数字 24–28px 等宽，单位小号；指标卡等宽网格。
- **信号表**：列 = 时间 / 代码 / 名称 / 方向 / 概率 / 置信度 / 价格 / 状态；行 hover 背景 `--bg-2`；新信号插入时 FLIP 滑入，不整表重排闪烁。
- **状态提示**：顶部细条（高 2px）显示连接状态：绿色已连接/黄色重连中/红色断开；自动重连（指数退避 1s→2s→4s→8s）。
- **空状态**：一行说明 + 可执行下一步（如"先运行 python -m quant.cli fetch"），不做插画。

## 4. 技术约束

- 纯 HTML + CSS + JS，**无构建、无 CDN、无第三方库**；文件：`index.html` / `styles.css` / `app.js`。
- **唯一例外**：Cloudflare Turnstile 官方脚本 `https://challenges.cloudflare.com/turnstile/v0/api.js`，仅在收到人机验证挑战（401 `{"challenge":true}`）时懒加载；除此之外无任何在线字体 / 图标 / CDN。
- 图表用 SVG 或 Canvas 手写；不引 Chart.js/ECharts（体积 + CDN 依赖）。
- 数据源：本地 `GET /api/signals`、`/api/paper`、`/api/equity`；Cloudflare `GET /api/signals` + `GET /api/stream`（SSE）。
- 可访问性：对比度 ≥ 4.5:1，focus-visible 样式，SSE 更新用 `aria-live="polite"`。

## 5. 验收（前端部分）

1. 1440px 与 390px 宽都可用，无横向滚动。
2. DevTools Performance 录制 10s：无长任务 > 50ms，动画期间稳定 60fps。
3. 断网打开页面：样式/字体/图标完整（无 CDN 请求 404）。
4. `prefers-reduced-motion` 模拟下无动画。
5. 数据刷新连拍 20 次：数字补间平滑、列表无闪烁/跳动。
6. 目测：像券商终端的专业感，而不是 AI 生成的落地页。
