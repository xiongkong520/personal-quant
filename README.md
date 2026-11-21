# 个人量化工作台（A股 · 模拟盘 · 本地 + Cloudflare）

一个人用的量化研究台：**数据采集 → 特征/方向判断 → 回测验证 → 模拟盘 → 实时看板**。
不做实盘、不碰券商密钥，只做研究与模拟。

> ⚠️ 免责声明：本项目仅供学习研究，输出不构成任何投资建议。市场有风险，决策自负。

## 能力

- A股日线/分钟线采集（AKShare，前复权 + 涨跌停价）
- 特征工程 + 方向判断模型（softmax 回归，导出 JSON 权重，Python / TypeScript 双端推理）
- walk-forward 回测：命中率、logloss、校准曲线，与"一直看涨"等基准对比
- 模拟盘：虚拟资金、T+1、涨跌停、佣金/印花税/过户费、滑点
- 本地 FastAPI + 静态看板
- Cloudflare Worker + D1 + Pages 实时更新（与本地同一份模型权重与信号 schema）

## 快速开始

```bash
# 1. 依赖
pip install -r requirements.txt
npm --prefix cloudflare/worker install

# 2. 拉数据（首次约 1 分钟）
python -m quant.cli fetch --symbols 600519,000001 --start 2024-01-01 --end 2026-11-20

# 3. 训练 + 回测
python -m quant.cli train  --horizon 1d
python -m quant.cli backtest --start 2025-01-01 --end 2026-11-20

# 4. 模拟盘 + 本地服务
python -m quant.cli paper --init-cash 1000000 --advance 1
python -m quant.cli serve --port 8787   # 打开 http://127.0.0.1:8787/

# 5. Cloudflare 本地验证（不需要账号）
npm --prefix cloudflare/worker run dev   # wrangler dev --local，http://127.0.0.1:8788
```

## 目录

```
src/quant/     Python：数据/特征/模型/回测/模拟盘/本地API
cloudflare/    Worker + D1 + Pages
frontend/      本地看板静态页
config/        配置
docs/          架构、契约、免责声明
scripts/       一键脚本
tests/         单测（离线 fixture）
```

## 文档

- [契约（数据/信号/规则）](docs/CONTRACT.md)
- [架构](docs/ARCHITECTURE.md)

## License

MIT
