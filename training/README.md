# 零成本云端训练管道（training/）

> 目标：用 **Kaggle / Colab / GitHub Actions 免费算力**完成「数据采集 → LightGBM 训练 → 轻量模型 JSON 导出」，
> 训练产物提交回仓库，供 Cloudflare Worker 加载做 T+1 三分类推理。全流程**不需要 GPU**、不需要付费数据源。
>
> ⚠️ 仅供学习研究，非投资建议。训练/回测不接券商、不做实盘。

---

## 1. 数据源选型（社区主流做法）

| 优先级 | 数据源 | 用途 | 说明 |
|---|---|---|---|
| 1（首选） | **AKShare** | 前复权日线 `stock_zh_a_hist_em(adjust="qfq")`；涨跌停池 `stock_zt_pool_em` / `stock_zt_pool_dtgc_em`；交易日历 `calendar_trade_date_hist_sina` | A 股量化社区最流行的免费数据源，接口文档全、更新快；东财接口高频会限流，脚本内置退避重试 |
| 2（兜底） | **Baostock** | 前复权日线（`adjustflag="2"`）、指数日线、交易日历 | 官方免费、匿名登录、长期稳定，社区常用兜底源；AKShare 失败/限流时自动切换 |
| 3（可选，本次未接入） | Tushare / Qlib | 更大股票池、因子库 | Tushare 需积分/注册；Qlib 自带 A 股数据但依赖较重，零成本场景不作为默认 |

**不自行爬取腾讯/新浪原始 HTTP 接口**：社区不推荐（无公开 SLA、列口径易变、无复权保证），
因此 `fetch_data.py` 只有 AKShare / Baostock 两条路径，没有腾讯/新浪降级开关。

### 免费算力怎么选

| 平台 | 配置 | 免费额度 | 适用 |
|---|---|---|---|
| **Kaggle Notebook（首选）** | 2× T4 GPU / 4 vCPU / 16GB | 约 30 小时/周（GPU） | 跑 40 只池 × 3 年全流程（约 10–20 分钟，CPU 就够） |
| **Colab 免费** | 1× T4 / 2 vCPU | 动态（可能被回收） | 应急/临时实验；中断后重跑即可（脚本幂等增量） |
| **GitHub Actions（兜底）** | 2 vCPU / 7GB | 公开仓库免费 | 每天定时只产出/更新模型（小池 + 短训练，<10 分钟），无需 GPU |

> LightGBM 的 PyPI wheel 是 CPU 版，**不需要也不建议折腾 GPU**：本管道 40 只票 × 3 年数据量很小，
> T4 机器上 CPU 训练 1 分钟级别。

---

## 2. 三步流程

```bash
# 0) 安装依赖（Kaggle/Colab 里只需一次）
pip install -r training/requirements.txt

# 1) 数据采集：近 3 年前复权日线 + 最近 30 个交易日涨跌停池 → training/data/*.parquet
python training/fetch_data.py
# 小样本验证（更快）：
python training/fetch_data.py --symbols 600519,000001 --start 2024-01-01

# 2) 特征 + Walk-Forward 训练 → training/out/{model.txt,metrics.json,train_meta.json,oof.parquet}
python training/train_lgbm.py

# 3) 导出轻量模型 JSON → model/latest_model.json + model/feature_config.json
python training/export_model.py
```

导出的 `model/latest_model.json` 提交回仓库（约 300KB，上限 2MB）；Worker 从
`raw.githubusercontent.com` 拉取并做树遍历推理（见 `cloudflare/worker/src/model_infer.ts`）。

### Kaggle Notebook 一键

`training/kaggle_notebook.ipynb` 已封装上述三步：
1. Kaggle 新建 Notebook → **开启 Internet**（Settings → Internet on）；
2. 上传 `kaggle_notebook.ipynb`（或直接打开），把仓库 clone 下来；
3. 菜单 **Run All**，最后从右侧 Output 面板下载 `model/` 两个 JSON，提交回仓库。

### GitHub Actions 兜底

在 `daily-quant.yml`（或单独 workflow）里加一步（由 Lead 负责接入）：

```yaml
- name: 云端训练 + 导出模型
  run: |
    pip install -r training/requirements.txt
    python training/fetch_data.py --symbols 600519,000001,300750 --start 2024-01-01 --zt-days 1
    python training/train_lgbm.py
    python training/export_model.py
- name: 提交模型
  run: |
    git add model/
    git commit -m "chore(model): nightly lgbm $(date -u +%F)" || echo "无变更"
    git push || true
```

> Actions 里 `--zt-days 1` 只增量抓当日涨跌停池（接口历史窗口有限，历史靠每日累积）。

---

## 3. 数据文件（`training/data/*.parquet`，snappy 压缩）

### `daily.parquet`（个股日线，前复权）

| 列 | 说明 |
|---|---|
| `symbol` | 6 位代码 |
| `trade_date` | `YYYY-MM-DD` |
| `open/high/low/close` | **前复权**价（元） |
| `pre_close` | 前复权前收 |
| `volume` | 成交量（股，AKShare 手×100） |
| `amount` | 成交额（元） |
| `pct_chg` | 涨跌幅（%） |
| `turnover_rate` | 换手率（%） |
| `source` | `akshare` / `baostock` / `synthetic` |

- **剔除停牌零成交行**（volume<=0 或价格为空）；
- 增量合并：重复 `(symbol, trade_date)` 以最新为准。

### `zt_pool.parquet`（每日涨跌停池统计）

| 列 | 说明 |
|---|---|
| `trade_date` | 交易日 |
| `limit_up_count` / `limit_down_count` | 涨停/跌停家数 |
| `broken_count` / `broken_rate` | 炸板家数与炸板率 = 炸板/(涨停+炸板) |
| `max_consecutive_boards` | 最高连板高度 |
| `limit_up_seal_amount` | 涨停封板资金合计（元） |
| `top_industry` / `top_industry_limit_up` | 涨停最集中的行业及家数 |
| `has_down_data` / `has_broken_data` | 当日跌停池/炸板池是否可取（1/0） |

> **重要限制（实测 AKShare 1.20.65）**：涨跌停池接口只覆盖**最近约 10~30 个交易日**
> （`stock_zt_pool_dtgc_em` / `stock_zt_pool_zbgc_em` 超过 30 日直接报错）。
> 因此 3 年训练区间里，真实情绪温度计只在最近窗口可用；更早部分由训练脚本用
> 「股票池内代理情绪」特征补齐，并用 `sentiment_missing` 标记缺失。建议每日增量运行累积真实历史。

### `index.parquet`（指数基准）

`symbol`（`000300` 沪深300 / `000001` 上证 / `399001` 深证成指）、`trade_date`、OHLC、`volume`、`amount`、`source`。
沪深300 用于个股超额收益；上证+深证成交额合计≈全市场成交额（用于「成交额全市场占比」）。
指数源不可用时训练自动降级为「股票池等权基准 + 股票池成交额合计」。

---

## 4. 特征与标签口径（严禁未来函数）

行 = **信号日 t（收盘后）**；标签 = **T+1 可成交收益**。所有特征只使用 `≤ t` 的数据；
**市场/情绪类特征统一 `shift(1)`** 后对齐（比"收盘后即可得"更保守）。

### 4.1 标签

```
r = close[t+1] / open[t+1] - 1        # 次日开盘买、收盘卖（可成交口径）
up   : r >  +0.002  (+0.2% 双边摩擦)
down : r <  -0.002
flat : 其余（|r| ≤ 0.2%，不构成可交易方向）
```

- 只保留有 T+1 数据的行；停牌日已在数据层剔除，跨停牌时用下一可交易日开盘价；
- 0.2% 阈值 = 双边摩擦成本（佣金+印花税+滑点近似），只有跑赢摩擦的行情才计为方向样本。

### 4.2 特征（30 个，顺序即 `model.json` 的 `features[]`）

| 组 | 特征 | 公式（t = 信号日） |
|---|---|---|
| 个股动量 | `ret_1d/5d/20d` | `close[t]/close[t-w]-1` |
| 超额收益 | `excess_ret_5d/10d/20d` | `ret_w[t] - bench_ret_w[t]`（bench=沪深300，缺失退回股票池等权） |
| 量能 | `amount_share` | `amount[t] / 全市场成交额[t]`（上证+深证；缺失用股票池合计） |
| | `amount_share_ma5` | 5 日均值 |
| | `turnover_rate` | 换手率(%) |
| K线形态 | `upper_shadow` | `(high-max(open,close))/pre_close` |
| | `lower_shadow` | `(min(open,close)-low)/pre_close` |
| | `body_ratio` | `(close-open)/pre_close` |
| | `amplitude` | `(high-low)/pre_close` |
| | `gap` | `open/pre_close-1` |
| 均线乖离 | `ma_bias_5/20/60` | `close/MA_w-1` |
| 波动 | `volatility_5/20` | `std(ret_1d, w)` |
| 涨跌停情绪（lag1） | `limit_up_count_lag1` | 全市场涨停家数[t-1] |
| | `limit_down_count_lag1` | 全市场跌停家数[t-1] |
| | `broken_rate_lag1` | 炸板率[t-1] |
| | `max_consecutive_boards_lag1` | 最高连板[t-1] |
| | `limit_up_ma5_lag1` / `broken_rate_ma5_lag1` | 5 日均值[t-1] |
| | `sentiment_missing` | 当日涨跌停池数据缺失=1（缺失填 0） |
| 股票池代理情绪（lag1） | `pool_limit_up_count_lag1` | 池内收盘涨停家数[t-1]（按板块涨跌停价判断） |
| | `pool_broken_rate_lag1` | 池内触板未封比例[t-1] |
| | `pool_max_boards_lag1` | 池内最高连板[t-1] |
| 市场基准 | `bench_ret_5d_lag1` | 沪深300 5 日收益[t-1] |

> 代理情绪用「训练股票池」近似全市场涨跌停温度，只用于补历史缺失；口径与全市场真实池不同，README 已注明。
> 特征缺失统一填 0（训练与 Worker 推理同口径）。所有特征在 `training/out/train_meta.json` 与
> `model/feature_config.json` 中带公式说明。

### 4.3 Walk-Forward

- 滚动切分：训练窗 250 交易日 → 验证窗 20 交易日 → 步长 20 交易日（可 `--train-days/--valid-days/--step` 调）；
- 每折独立重训 LightGBM（三分类，`down/flat/up`），**验证窗严格在训练窗之后**，不做随机切分；
- 汇总所有折的样本外（OOF）预测计算：`accuracy`、`logloss`、`multiclass brier`、
  分方向 P/R/F1、混淆矩阵、置信度校准、交易口径 `win_rate`（做多/研究性做空，扣 0.2% 摩擦）+ Wilson 95% CI；
- 最终部署模型用全部有标签样本重训，指标仍为 Walk-Forward 样本外口径。

---

## 5. 模型 JSON（`model/latest_model.json`）

遵循 `docs/UPGRADE_PLAN.md` §3.3：

```json
{
  "schema": 1,
  "model": "lightgbm",
  "version": "lgbm-2026-11-26",
  "trained_at": "2026-11-26T18:30:00+08:00",
  "label": { "horizon": 1, "definition": "T+1 收益 > 0.2%（双边摩擦）→ up ...", "classes": ["up", "flat", "down"] },
  "features": [{ "name": "ret_1d", "index": 0, "dtype": "float" }, "..."],
  "feature_config": { "winsorize": { "lower": 0.01, "upper": 0.99 }, "normalize": { "mean": [], "std": [] } },
  "trees": [
    { "split_feature": [0, 1], "threshold": [0.5, -0.2], "left_child": [1, -1], "right_child": [-1, -2],
      "leaf_value": [-0.1, 0.2, 0.3], "decision_type": ["<=", "<="], "class_index": 0 }
  ],
  "objective": "multiclass",
  "num_class": 3,
  "metrics": { "walk_forward": { "accuracy": 0.0, "win_rate": 0.0, "wilson95": [], "logloss": 0.0, "n": 0 }, "cost": 0.002 }
}
```

### 树编码约定（Worker `model_infer.ts` 按此实现）

- 数组下标 = 树的内部节点编号：`split_feature` / `threshold` / `left_child` / `right_child` / `decision_type` 等长；
- `left_child` / `right_child`：`>= 0` 指向内部节点下标；`< 0` 指向叶子，`leaf_idx = -child - 1`，取 `leaf_value[leaf_idx]`；
- `leaf_value` 为叶子输出值；`decision_type` 数值特征均为 `"<="`；
- `class_index`：该树贡献的类别（`0=up, 1=flat, 2=down`，与 `label.classes` 顺序一致）；
- 推理：`raw[class_index] += tree(x)` → `p = softmax(raw)`；特征按 `features[].index` 顺序输入，缺失填 0。

`model/feature_config.json` 额外给出：特征顺序/分组/公式、缩尾与标准化参考值（`applied=false`，树模型不需要）、
标签口径、基准来源、特征重要性 Top、Worker 推理约定。**体积目标 < 2MB**：默认配置约 300KB；
超过上限时 `export_model.py` 会自动按类别裁剪树数并用 OOF 重算指标（`truncated=true`）。

---

## 6. 常用参数

### `fetch_data.py`

| 参数 | 默认 | 说明 |
|---|---|---|
| `--symbols` | 内置 40 只池 | 逗号分隔代码 |
| `--start/--end` | 近 3 年 ~ 今天 | 数据区间 |
| `--zt-days` | 30 | 抓最近 N 个交易日涨跌停池（0=全部；接口只支持近 30 日） |
| `--skip-zt/--skip-index` | - | 跳过情绪池/指数（更快） |
| `--synthetic` | - | 离线合成数据（无网络演示/CI） |

### `train_lgbm.py`

| 参数 | 默认 | 说明 |
|---|---|---|
| `--train-days / --valid-days / --step` | 250 / 20 / 20 | Walk-Forward 窗口 |
| `--num-iterations / --num-leaves` | 120 / 15 | 树数/叶子数（决定模型 JSON 体积） |
| `--label-cost` | 0.002 | 双边摩擦阈值 |
| `--seed` | 42 | 随机种子（同数据+种子 → 同模型） |
| `--synthetic` | - | 离线合成数据跑通全流程 |

### `export_model.py`

| 参数 | 默认 | 说明 |
|---|---|---|
| `--booster/--meta/--oof` | `training/out/*` | 训练产物路径 |
| `--out-model/--out-config` | `model/latest_model.json` / `model/feature_config.json` | 输出 |
| `--max-size-mb` | 2.0 | 体积上限（超出自动裁剪树数） |
| `--validate-only` | - | 只校验已有模型 JSON |

---

## 7. 故障排查

| 现象 | 处理 |
|---|---|
| `Push2RateLimitError` / 东财限流 | 脚本自动退避重试并切 Baostock；建议降低频率（`--sleep 1.0`）或隔几分钟重跑 |
| 涨跌停池大量为空 | 正常：免费接口只覆盖最近约 10~30 个交易日；老数据用代理情绪，历史靠每日增量累积 |
| `libgomp.so.1` 缺失（Linux 精简环境） | `apt-get install -y libgomp1`（Kaggle/Colab 已内置） |
| 特征/标签全空 | 数据区间不足 60 个交易日（ma_bias_60 需要预热）；扩大 `--start` |
| 训练报「类别不足」 | 训练窗内缺少 flat/up/down；扩大区间或调大 `--train-days` |
| 模型 JSON 超 2MB | 降低 `--num-iterations/--num-leaves`，或让导出脚本自动裁剪（会重算指标） |

---

## 8. 产出清单

| 文件 | 是否提交 | 说明 |
|---|---|---|
| `model/latest_model.json` | ✅ | 轻量 LightGBM 树模型（Worker 加载） |
| `model/feature_config.json` | ✅ | 特征顺序/公式/归一化参数/推理约定 |
| `training/out/metrics.json` | ❌ | Walk-Forward 指标 + 特征重要性（本地/Notebook 留档） |
| `training/out/train_meta.json` / `model.txt` / `oof.parquet` | ❌ | 中间产物（`training/.gitignore` 忽略） |
| `training/data/*.parquet` | ❌ | 行情数据（可随时重抓，不占仓库） |

---

## 9. 本次小样本真实运行（验收记录 2026-11-26）

```bash
# 1) 采集（小样本；当时东财接口临时限流，脚本自动切 Baostock，属预期兜底路径）
python training/fetch_data.py --symbols 600519,000001,300750,601318,600036,000858,002594,600030 --start 2024-01-01 --skip-zt
#    → daily.parquet 5616 行 / 8 只票 / 2024-01-02~2026-11-26；index.parquet 2106 行（000300/000001/399001）
#    → 数据源：akshare 限流 → baostock 兜底成功（source 列标注；代码首选 AKShare，恢复后自动用回）

# 2) 训练（250/20/20 滚动、LightGBM 120 轮 × 15 叶）
python training/train_lgbm.py
#    → training/out/{model.txt,metrics.json,train_meta.json,oof.parquet}

# 3) 导出
python training/export_model.py
#    → model/latest_model.json（332,573 bytes ≈ 325KB < 2MB）+ model/feature_config.json
```

本次样本外（Walk-Forward，22 折，OOF n=3520）指标：

| 指标 | 值 |
|---|---|
| accuracy | 41.70% |
| logloss | 1.0805（均匀 1.0986） |
| multiclass Brier | 0.6625（均匀 0.6667） |
| 交易口径 win_rate | 42.17%（Wilson 95% CI 40.53%~43.83%，3455 笔） |
| 分方向 win_rate | long 39.35%（1596 笔）/ short 44.59%（1859 笔，研究性做空） |
| avg_net_return / payoff | -0.198% / 0.937 |

> 说明：本次仅 8 只票、且涨跌停池历史数据不可用（免费接口只覆盖最近 30 日），情绪特征以股票池代理为主。
> 结果不构成"模型有效"的结论，仅用于验证训练/导出链路与指标口径；正式训练请在 Kaggle 用 40 只池 × 3 年数据重跑。
> 导出模型已通过 `validate_model_json` schema 校验，Python 参考推理与 LightGBM booster 预测 max|Δ|=1.1e-16。
