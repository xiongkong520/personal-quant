# 个人量化研究平台 · UI/UX 改版方案（v1）

> 状态：方案待确认 · 未开始实施
> 原则：**不伪造数据**、**不删除已有功能**、**保留技术栈与部署方式**、**渐进式改造**

## 一、设计定位

- **风格**：专业金融终端（参考 iFinD/Bloomberg 终端）+ 现代数据分析平台；深色蓝灰主题
- **不做**：普通后台管理系统、大面积渐变/发光、装饰性卡片
- **统一规范**：
  - 颜色：现有 CSS 变量体系（`--bg-0/1/2`、`--border`、`--text-0/1/2`、涨红跌绿/中性灰）扩展语义色（warn/danger/ok）
  - 间距：8px 网格（4/8/12/16/24/32）
  - 字体：正文系统字体；数字统一等宽（`ui-monospace`），金额/概率固定小数位
  - 图表：K 线/分时/净值统一配色与轴样式；信息密度优先，卡片间距一致
  - 移动端：390px 优先，底部 Tab 导航，图表横向滚动，触摸友好（≥44px 点击区）

## 二、导航结构（8 个独立页面）

桌面：左侧栏（图标+文字，可收起）；手机：底部 Tab（总览 / 行情 / 回测 / 模拟 / 更多），其余页面在「更多」抽屉。

| # | 页面 | 路由 | 桌面 | 手机 |
|---|---|---|---|---|
| 1 | 总览 Dashboard | `#/dashboard` | 侧栏第 1 项 | Tab 1 |
| 2 | 行情中心 Market | `#/market` | 侧栏 | Tab 2 |
| 3 | 因子实验室 Factor Lab | `#/factor` | 侧栏 | 更多 |
| 4 | 模型训练 Model Lab | `#/model` | 侧栏 | 更多 |
| 5 | 策略回测 Backtest | `#/backtest` | 侧栏 | Tab 3 |
| 6 | 模拟交易 Paper Trading | `#/paper` | 侧栏 | Tab 4 |
| 7 | 数据管理 Data Center | `#/data` | 侧栏 | 更多 |
| 8 | 系统设置 Settings | `#/settings` | 侧栏 | 更多 |

## 三、页面内容与数据映射（基于真实接口）

| 页面 | 核心内容 | 数据来源（现有） | 可用性 |
|---|---|---|---|
| **总览** | ① 市场状态卡（情绪分/分化/仓位建议/更新时间）② 4 大指数行情 ③ 自选股（信号股）行情与概率 ④ 最新模型信号 + 方向概率 ⑤ 午评/收评摘要 ⑥ 系统状态（数据源健康、更新状态） | `/api/market` `/api/regime` `/api/signals` `/api/summary/latest` `/api/health` | ✅ 全部可用 |
| **行情中心** | ① 指数 K 线（4 指数切换）② 今日分时 + 情绪/涨跌家数曲线 ③ 涨跌分布/涨停结构 ④ 板块涨幅榜 + 资金流 ⑤ 个股 K 线（搜索/代码） | `/api/kline` `/api/intraday` `/api/market/history` `/api/market` `/api/search` | ✅ 全部可用 |
| **因子实验室** | 因子 IC/RankIC/ICIR 排名、分层回测、Top-Bottom、因子相关性 | `data/reports/factor_eval.json`（本地） | ⚠️ 云端缺接口 → 需新增 `/api/factors` + Actions 推送；未接入前该页显示「开发中 · 待接入云端数据」 |
| **模型训练** | 模型版本/特征数/训练指标、walk-forward 评估、特征贡献 Top5、下次更新 | 本地 `model.json` + `/api/backtest` | ⚠️ 部分：walk-forward/回测指标可经 `/api/backtest` 展示；训练指标（准确率等）需新增 `/api/model-meta`；未接入部分标注开发中 |
| **策略回测** | 净值曲线（策略 vs 基准）、指标卡（年化/夏普/最大回撤/胜率/样本量）、分方向命中、基准对比、月度收益 | `/api/backtest` | ✅ 可用（前端补完整） |
| **模拟交易** | 账户现金/市值/今日盈亏/累计收益/胜率、持仓表、订单表、净值曲线、交易规则说明 | `/api/paper` `/api/equity` | ✅ 可用（补完整+说明） |
| **数据管理** | 数据源健康（东财/腾讯/新浪/iFinD/Baostock）、K 线覆盖（40 只×日期范围）、最近更新、免费额度提示、本地数据规模 | `/api/health` `/api/kline` + 新增 `/api/data-status`（建议） | ⚠️ 部分：基础可用；数据源状态明细需新增端点 |
| **系统设置** | 主题（深/浅）、刷新频率、数据源偏好、免责声明、版本信息、清空本地缓存 | `/api/config` + localStorage + 静态 | ✅ 可用 |

## 四、组件结构（复用现有实现）

### 布局组件
- `AppShell`：顶栏（品牌/搜索/实时状态）+ 侧栏（桌面）/底部 Tab（手机）+ 内容区
- `PageHeader`：标题 + 副标题 + 页面级操作（刷新/切换）
- `Card`：统一卡片（头部/内容/底部、角标、折叠）
- `StatGrid`：指标卡网格（数字 + 单位 + 变化色）
- `ChartCard`：图表容器（标题/提示/空态/骨架）

### 基础组件
- `StatusPill`（冰点/分化/偏热/偏空等状态标签，配色语义化）
- `SegmentedControl`（周期/范围切换：60/120/250 日；指数切换）
- `Tabs`（事件利好/利空、分时/日线）
- `DataTable`（信号表/持仓/订单/板块榜，统一列宽与对齐）
- `EmptyState`（未实现/无数据：中性文案 + 操作提示）
- `Skeleton`（加载骨架）
- `Callout`（免责声明/风险提示/数据缺失）

### 复用现有代码（不重写）
- 已有函数直接复用：`createKlinePanel/renderKline`、`renderMarket/renderRealtimeStrip`、`renderPanorama`、`renderBacktest/renderBacktestExtras`、`renderPaper/renderOrders/renderEquity`、`renderRegime`、`renderSummary`、`renderEvents`、`loadIntraday/renderTodayTabs`、搜索联想、`renderSignalTable` 等
- 重组方式：把现有 panel 挂到对应页面容器；保留原有 id/事件绑定；页面切换时显示/隐藏 + 按需触发加载

## 五、技术方案（保持现有技术栈）

1. **无构建原生 JS**（保持 Cloudflare Pages 部署不变）
2. **Hash 路由**：`#/dashboard` 等；路由表 + 页面注册；浏览器前进/后退可用
3. **按页懒加载**：进入页面才请求该页数据；总览保留实时轮询/SSE；离开页面暂停其轮询
4. **状态管理**：复用现有 `state` 对象 + 页面级 `pageState`；统一处理 5 种状态（loading/error/empty/challenge/ok）
5. **两套前端同步**：`frontend/` 与 `cloudflare/pages/` 的 `app.js`+`styles.css` 必须 `cmp` 字节一致；`index.html` 保留 6 处模式差异；缓存版本号升级
6. **不破坏原有逻辑**：现有面板函数/接口/事件不删除，只重排与增强；「全部功能」入口保留
7. **新增后端（可选，按阶段）**：
   - `/api/factors`（因子评估 JSON）
   - `/api/model-meta`（模型版本/训练指标）
   - `/api/data-status`（数据源健康/覆盖统计）
   - 以上均由 GitHub Actions 每日生成并推送（现有推送链路扩展）

## 六、分阶段实施计划

### 阶段 P1：应用框架与导航（先做）
- AppShell（侧栏/Tab/顶栏）、hash 路由、页面注册、设计 token 扩展
- 把现有全部面板迁入对应页面容器（总览/行情先接）
- 交付：页面可切换、无 JS 错误、移动端底部 Tab
- 验证：node --check、cmp、CDP 截图（1440/390）、线上部署

### 阶段 P2：总览 Dashboard 完整
- 市场状态卡（情绪/分化/仓位建议）、指数卡、自选/信号卡（含方向概率）、午评摘要、系统状态条
- 交付：首屏信息完整、真实数据、自动刷新

### 阶段 P3：行情中心完整
- 指数 K 线/分时/情绪曲线、涨跌分布、板块榜/资金流、个股 K 线搜索联动

### 阶段 P4：策略回测 + 模拟交易完整
- 回测：净值曲线、指标卡、分方向、基准对比、月度收益
- 模拟：账户/持仓/订单/净值、规则说明
- 空态：无数据中性提示

### 阶段 P5：因子/模型/数据/设置
- 先做设置/数据管理（现有数据）；因子/模型页先出页面骨架 + 「开发中」标注
- 后端接口（/api/factors、/api/model-meta、/api/data-status）就绪后再接入真实数据

### 阶段 P6：打磨与验收
- 移动端细节、加载/错误/空态全覆盖、性能（切页不重复请求）、全量测试、部署验证、截图归档

## 七、验收标准（每阶段）

1. `node --check frontend/app.js` 通过；两套前端 `cmp` 一致
2. 本地 `python -m quant.cli serve` + 线上 CDP 截图（1440/390）：页面渲染、无 console error
3. 数据全部来自接口（无伪造数字）；未实现功能显示「开发中」而非假数据
4. 移动端无横向溢出；Tab 可达；图表可滚动
5. `python -m pytest tests/ -q --ignore=tests/test_cloud_contract.py` 不回归（前端改动不影响后端测试）
6. Cloudflare Pages 部署命令不变，线上验证通过

## 八、风险与对策

| 风险 | 对策 |
|---|---|
| app.js 已 5300+ 行，整体重构风险高 | 渐进式：先框架后页面，每阶段可回滚；不重写已有函数 |
| 两套前端不一致导致线上旧版 | 每阶段 `cmp` + 部署后 curl 版本号验证 |
| 云端缺因子/模型接口 | 先出页面骨架 + 「开发中」，接口就绪再接入，绝不伪造 |
| 情绪/状态口径变化 | 情绪已拆「赚钱效应/投机热度/分化」；页面统一展示新口径 |
| 手机性能（canvas 多） | 页面切换时暂停非当前页定时器/重绘 |
