# 社区参考与方法论（每次动手前必读）

> **规矩：动手前先搜 GitHub / 社区（至少 2 次搜索），优先复用成熟方案，不自己发明。**

## 参考项目（2026-11 调研，均已验证存在）
| 项目 | 用途 | 可借鉴点 |
|---|---|---|
| [九章·ALGOR](https://github.com/Ksyniko/jiuzhang-algor) | 本地多智能体量化终端 | 后端 server.py 聚合 akshare 多源、暗色工业风 SPA、实时行情/板块成分/涨跌家数/情绪 |
| [dsh-limitup-widget](https://github.com/LusJourney/dsh-limitup-widget) | 涨停直播看板 | Python Flask 单文件 + 原生 JS 前端、akshare 免费接口、概念映射、60s 轮询 |
| [dsh-stock-terminal](https://github.com/linhut/dsh-stock-terminal) | 行情皮肤/插件 | 可靠性设计：轮询防竞态、请求去重、缓存、故障降级、本地轮询非服务端推送 |
| [a-stock-data](https://github.com/simonlin1212/a-stock-data) | A股数据工具包 | 零依赖直连 13 数据源 28 端点（akshare 替代） |
| [libra-design](https://goodie1972.github.io/libra-design/) | 金融前端组件 | 64 金融组件/设计令牌（K线/订单簿/行情表格/涨跌标签） |
| [tradingview-txkline](https://www.npmjs.com/package/tradingview-txkline) | K线组件 | lightweight-charts 封装、A股分时、多主题 |

## 数据源结论（实测）
- **腾讯** `qt.gtimg.cn`：**CORS 开放**（浏览器可直连、3 秒轮询），批量报价；`web.ifzq.gtimg.cn` minute 分时当日数据，Worker/本机均可用
- **新浪**：行情列表（`Market_Center.getHQNodeData` 分页，全市场 5579 只）；K 线/分时（有反爬，30s 级低频可用）
- **东财** push2 clist/trends2：**本机出口可用，Cloudflare 出口被限流/断连** → 只能离线管道跑
- **结论**：实时抓取放本地/离线；CF 只做静态托管 + 快照兜底

## 架构决策（2026-11 定）
1. **主路径：本地 Python 服务**（FastAPI/Flask 直连数据源，手机 proot 可跑）→ 前端只读；无 CF 限流/cron 限制，真正秒级
2. **CF 版：静态镜像 + 快照兜底**（分享/手机无本地环境时用）
3. 秒级上限：免费源 = 客户端 **3 秒轮询**（easyquotation 同款）；真 tick 级需付费 Level-2
4. 可靠性设计抄 dsh-stock-terminal：轮询防竞态、请求去重、短 TTL 缓存、故障降级
5. 数据正确性：`scripts/data_audit.py` 多源交叉校验（指数/成交额/涨跌家数/快照）

## 任务开工流程（写进每个任务）
1. 先搜社区（GitHub/博客）确认是否已有成熟方案
2. 有 → 借鉴结构/代码；没有 → 最小实现 + 单点验证
3. 改完跑 `scripts/e2e_check.py` + 截图验收
4. 不通过验收不提交
