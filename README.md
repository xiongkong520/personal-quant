# 个人量化工作台 · A股

![CI](https://github.com/xiongkong520/personal-quant/actions/workflows/ci.yml/badge.svg)

**在线看板（国内可访问）**：https://quant-board-9y2.pages.dev

**由 DeepSeek 从零构建**的个人量化研究台：A股数据采集 → 特征与方向判断 → 历史回测与胜率推算 → 模拟盘（T+1/涨跌停/费用/滑点）→ 本地与 Cloudflare 实时看板。

> ⚠️ **免责声明：仅供研究学习，不构成投资建议。** 本项目不做实盘、不连接券商/交易所下单接口、不保存任何交易密钥。市场有风险，决策自负。详见 [docs/DISCLAIMER.md](docs/DISCLAIMER.md)。

## 能力

- **数据**：AKShare 日线/分钟线，前复权 + 涨跌停价计算，东财接口限流时自动降级新浪源
- **方向判断**：20+ 技术特征 + softmax 回归，输出方向（看涨/震荡/看跌）、概率、置信度、Top 特征贡献；模型导出 JSON，Python / TypeScript 双端可复现
- **历史回测**：walk-forward 滚动切分（禁未来函数），输出总体与分方向 precision/recall/F1、logloss、Brier、3×3 混淆矩阵、置信度分桶校准、分年度稳定性
- **胜率推算**：交易口径 `win_rate`（与方向准确率严格分开）+ Wilson 95% 置信区间、盈亏比、最大回撤、Sharpe、净值曲线，并与 always-flat / 多数类 / 5日均线交叉 / 随机 四个基准对比
- **模拟盘**：虚拟资金、T+1、涨跌停、佣金（万2.5 最低5元）/ 印花税 / 过户费、滑点、整手、停牌顺延、订单拒因可追溯
- **本地看板**：FastAPI + 无构建前端，方向灯 / 概率条 / 净值曲线 / 回测指标，丝滑动效
- **Cloudflare**：Worker + D1 + Pages，SSE 实时更新；Turnstile 人机验证 + UA 反爬 + 限流 + 签名 Cookie（公网可部署，详见 [docs/SECURITY.md](docs/SECURITY.md)）

## 数据与隐私（一人一数据）

仓库只包含**代码**与 **4~6 行的脱敏测试样例**。真实行情库、模型文件、回测报告、模拟盘账户与交易记录全部在你本地 `data/` 生成，已在 `.gitignore` 中排除，**不会随仓库分发**。每个人克隆后自行采集自己的数据，数据互不共享。

## 由 DeepSeek 制作

本项目的需求拆解、架构设计、数据层、特征/模型/回测、模拟盘、前端与 Cloudflare 部署，由 **DeepSeek Agent 团队**（DeepSeek Harness 驱动）从零实现，人类仅提供方向与验收。代码、契约、测试均可在仓库中追溯。

## 快速开始

```bash
pip install -r requirements.txt        # 或 pip install -e .
python -m quant.cli fetch --symbols 600519,000001 --start 2024-01-01 --end 2026-11-20
python -m quant.cli train              # 训练并导出 data/models/model.json
python -m quant.cli backtest           # 历史回测 + 胜率，输出 data/reports/backtest.md/.json
python -m quant.cli signal             # 生成最新信号并落库
python -m quant.cli paper --init-cash 1000000 --advance 5   # 模拟盘推进
python -m quant.cli serve --port 8787  # 本地看板 http://127.0.0.1:8787/
```

Cloudflare 本地验证（无需账号）：

```bash
cd cloudflare/worker && npm i
npx wrangler dev --local --port 8788
curl 127.0.0.1:8788/api/health
```

公网部署（可选，需 Cloudflare 账号）：见 [cloudflare/worker/README.md](cloudflare/worker/README.md) 与 [docs/SECURITY.md](docs/SECURITY.md)。

## 存储

- **行情数据**：Parquet 文件（`data/parquet/`，按 symbol 分区），DuckDB 直接 `read_parquet` 查询，列式扫描快
- **信号 / 模拟盘状态**：DuckDB 单库（`data/db/quant.duckdb`）
- 旧 SQLite 数据迁移：`python scripts/migrate_storage.py`（逐表行数校验、幂等）

## 目录

```
src/quant/      数据/特征/模型/回测/模拟盘/本地API
cloudflare/     Worker + D1 + Pages
frontend/       本地看板（无构建 HTML/CSS/JS）
config/         配置
docs/           契约、架构、设计规范、安全、免责声明
scripts/        冒烟与部署脚本
tests/          离线单元测试（fixtures）
```

## 文档

- [模块契约（数据/信号/规则/胜率口径）](docs/CONTRACT.md)
- [架构与失败兜底](docs/ARCHITECTURE.md)
- [前端设计规范（反 AI 味 / 丝滑动效）](docs/DESIGN.md)
- [公网安全与部署](docs/SECURITY.md)
- [免责声明](docs/DISCLAIMER.md)

## License

MIT
