# 训练侧说明（TRAINING_NOTES）

> 面向维护者的训练链路备忘：数据源、无 TPU 决策、指标口径、每日产物、GitHub Actions 现状与遗留。
> 相关文件：`training/README.md`（操作手册）、`training/fetch_data.py` / `train_lgbm.py` / `export_model.py`、`training/tpu/DEPRECATED.md`。
>
> ⚠️ 仅供学习研究，非投资建议；不接券商、不做实盘。

---

## 1. 当前正式方案（TL;DR）

| 项 | 决定 |
|---|---|
| 计算 | **CPU 足够**。每日训练走 **GitHub Actions 标准 runner（2 vCPU）**；手工全量实验用 Kaggle Notebook **CPU** |
| TPU/GPU | **不需要，已弃用 TPU 路径**（原因见 §3） |
| 模型 | LightGBM 三分类（up/flat/down），Walk-Forward 训练，导出轻量树 JSON |
| 数据源 | **AKShare 首选 → Baostock 兜底**；不自行爬腾讯/新浪原始接口 |
| 产物 | `model/latest_model.json`（<2MB）+ `model/feature_config.json`；每日任务同时归档 `models/`（旧 softmax 模型）与 `reports/`（回测/市场摘要） |
| 线上消费 | Cloudflare Worker 拉取 `model/latest_model.json` 做树遍历推理（cloud-dev 的 `model_infer.ts`） |

---

## 2. 数据源

### 2.1 选型（社区主流）

| 优先级 | 数据源 | 接口 | 备注 |
|---|---|---|---|
| 1 主源 | **AKShare** | `stock_zh_a_hist_em(period="daily", adjust="qfq")`；涨跌停池 `stock_zt_pool_em` / `stock_zt_pool_dtgc_em` / `stock_zt_pool_zbgc_em`；交易日历 `calendar_trade_date_hist_sina` | A 股量化社区最流行免费源；**东财接口对共享 IP 高频限流**（GitHub Actions 上尤其明显），限流时会变慢/失败 |
| 2 兜底 | **Baostock** | `query_history_k_data_plus(..., adjustflag="2")`（前复权）；指数、交易日历同源 | 官方免费、匿名登录、长期稳定；AKShare 失败/熔断后自动切换 |
| 不采用 | 腾讯/新浪原始 HTTP | — | 社区不推荐：无公开 SLA、列口径易变、无复权保证；`fetch_data.py` 已删除这些路径（离线测试断言不存在） |
| 可选未接入 | Tushare / Qlib | — | Tushare 需积分/注册、Qlib 依赖重，零成本场景不作默认 |

### 2.2 口径

- **前复权**：AKShare `adjust="qfq"`；Baostock `adjustflag="2"`（价格列直接是前复权）。
- **剔除停牌/零成交行**：`volume<=0` 或价格为空的行直接丢弃（`_finalize_stock`）。
- **交易日历**：AKShare 官方封装 → Baostock 日历 → 工作日兜底。
- **幂等增量**：按主键合并去重（`daily: symbol+trade_date`，`zt_pool: trade_date`），每日重跑只补新数据；
  Parquet + snappy 压缩。
- **股票池**：默认 40 只（主板/创业板/科创板，消费/金融/新能源/科技/周期），`--symbols` 可覆盖。
- **基准/全市场成交额**：沪深300 用于超额收益；上证指数+深证成指成交额合计≈全市场成交额；
  指数缺失时自动降级为「股票池等权基准 + 股票池成交额合计」。

### 2.3 已知限制

1. **涨跌停池历史窗口**：AKShare 免费接口只覆盖最近约 10~30 个交易日
   （`stock_zt_pool_em` 更早日期返回空，`dtgc/zbgc` 明确报"只能获取最近 30 个交易日"）。
   3 年训练区间的真实情绪历史拿不到 → 缺失填 0 + `sentiment_missing` 标记，
   历史用「股票池内代理情绪」（池内涨停家数/触板未封率/连板高度）补齐。
   **每日 Actions 增量抓当日池并提交 `training/data/zt_pool.parquet`，逐日累积真实历史。**
2. **Baostock 匿名会话并发冲突**：Baostock 匿名登录是进程级会话，同一 IP 多进程并发
   （例如同时跑多个分片抓取、CI 与本机同时跑）会互相把对方踢下线，表现为 `用户未登录`。
   `fetch_data.py` 已加**自动重登录**：会话类错误时重置会话、重新登录并重试；大批量抓取建议单进程串行。
3. **东财限流**：GitHub Actions / 共享 IP 上 `Push2RateLimitError` 高发。训练侧已加**AKShare 连续失败熔断**：
   `--akshare-fail-fast N`（默认 2）：连续 N 次失败后本进程直接走 Baostock，避免 akshare 内部重试拖慢 CI；
   `--retries 1` 可进一步降低首次尝试成本（CI 推荐 `--retries 1 --akshare-fail-fast 1`）。

---

## 3. 为什么弃用 TPU（2026-11-26 决策）

**背景**：用户尝试 Kaggle TPU Notebook 时**身份验证未通过**（截图确认），免费 TPU 配额实际不可用；
TPU 路径（`training/tpu/` 下的 GRU/TF 全市场实验、Kaggle kernel 推送流程）随即砍掉。

**结论**

- **LightGBM 是 GBDT 树模型**：训练是直方图分裂，CPU 即最优路径；PyPI wheel 为 CPU 版，
  GPU 支持需源码编译，社区对 40 只票 × 3 年这种数据量也不推荐折腾。
- **性能足够**：12 只池 × 近 3 年 ≈ 1–3 分钟（Baostock 兜底时含网络时间），40 只池也仅 10–20 分钟。
- **产物不变**：线上推理只加载 `model/latest_model.json`（树模型），与 TPU/NN 路径无关。
- `training/tpu/` 目录**已弃用归档**（保留仅供追溯），说明见 `training/tpu/DEPRECATED.md`；
  新流程中不要引用其中脚本。

> 若未来要恢复神经网络路线：先解决 Kaggle 身份验证，并确认 Worker 推理链路愿意支持 TF/ONNX 模型格式，
> 否则会与当前"轻量树 JSON"契约冲突。

---

## 4. 指标口径（与 training/README.md 一致）

### 4.1 标签（T+1 可成交收益，含 0.2% 双边摩擦）

```
r = close[t+1] / open[t+1] - 1        # 次日开盘买、收盘卖（可成交口径）
up   : r >  +0.002   (0.2% 双边摩擦)
down : r <  -0.002
flat : |r| ≤ 0.2%（不构成可交易方向）
```

- 行 = 信号日 t（收盘后）；特征只使用 `≤ t` 数据；**市场/情绪类特征统一 `shift(1)`** 后对齐；
- 0.2% ≈ 双边交易成本（佣金+印花税+滑点近似），只有跑赢摩擦的行情才算方向样本。

### 4.2 Walk-Forward

- 滚动切分：**训练窗 250 交易日 → 验证窗 20 交易日 → 步长 20 交易日**（可调 `--train-days/--valid-days/--step`）；
- 每折独立重训 LightGBM（classes = down/flat/up），**验证窗严格位于训练窗之后**，不做随机切分；
- 汇总所有折的样本外（OOF）预测计算指标；最终部署模型用全部有标签样本重训，指标仍为样本外口径。

### 4.3 指标定义

| 指标 | 定义 |
|---|---|
| `accuracy` | 三分类总体准确率（OOF） |
| `logloss` / `brier` | 三分类交叉熵 / multiclass Brier（均匀分布基准 log(3)=1.0986 / 0.6667） |
| `win_rate` | **交易口径，与方向准确率分开**：对每个方向预测（up=做多、down=研究性做空）按 T+1 可成交收益扣 0.2% 摩擦，收益 > 0 记为胜；`win_rate = 胜笔/方向预测笔数` |
| `wilson95` | win_rate 的 Wilson 95% 置信区间 |
| `win_rate_by_direction` | long/short 分开的笔数、胜率、CI、单笔平均净收益 |
| `avg_net_return` / `payoff_ratio` | 单笔平均净收益 / 平均盈利÷平均亏损 |
| `feature_importance` | 最终 booster 的 gain/share 排序（写入 `feature_config.json`） |
| 混淆矩阵 / calibration / folds | 均为 OOF 口径，写入 `training/out/metrics.json` |

> **注意**：方向准确率基于 close[t]→close[t+1] 的标签语义，而交易口径基于 T+1 开盘→收盘的**可成交**收益；
> 两者不可混用。样本量 < 30 笔时胜率 CI 仅供参考。

---

## 5. 每日产物（谁写、是否入库）

| 文件 | 产出方 | 是否入库 | 说明 |
|---|---|---|---|
| `model/latest_model.json` | `training/export_model.py` | ✅ 提交 | 轻量树模型（约 325KB，上限 2MB），Worker 推理用 |
| `model/feature_config.json` | `training/export_model.py` | ✅ 提交 | 特征顺序/公式/归一化参考值/推理约定 |
| `training/data/zt_pool.parquet` | `training/fetch_data.py --zt-days 1` | ✅ 提交（增量） | 涨跌停池历史逐日累积；接口只给最近 30 日，靠每日累积补历史 |
| `training/out/metrics.json` | `training/train_lgbm.py` | ✅ 提交 | Walk-Forward 指标 + 特征重要性（每日变更） |
| `training/out/{model.txt,train_meta.json,oof.parquet}` | 训练 | ❌ 忽略 | 中间产物，`training/.gitignore` 忽略 |
| `training/data/{daily,index}.parquet` | 采集 | ❌ 忽略 | 可随时重抓；仅 zt_pool 需要入库累积 |
| `models/model.json`、`models/index_model.json` | 旧 softmax 管线（`quant.cli train` 后归档） | ✅ 提交 | **与 `model/` 不是一回事**：`models/` 是每日 softmax 归档，Worker 新链路用 `model/latest_model.json` |
| `reports/backtest.md`、`reports/market.json` 等 | 回测/市场/AI 报告 | ✅ 提交 | 每日研究归档，供前端/人工查看 |

---

## 6. GitHub Actions 状态检查（2026-11-26）

### 6.1 最近两次 Daily Quant run

| run | 时间(UTC) | 结论 | 训练步骤(#9) | 提交步骤(#10) |
|---|---|---|---|---|
| [45806366244](https://github.com/xiongkong520/personal-quant/actions/runs/45806366244) | 2026-11-26 10:59 | success | success，但耗时 **2026s ≈ 34 分钟** | success，耗时 **0s** |
| [45801292186](https://github.com/xiongkong520/personal-quant/actions/runs/45801292186) | 2026-11-26 10:26 | success | success，耗时 **2160s = 36 分钟** | success，耗时 **0s** |

两 run 的 event 均为 `push`（今日定时 run 未在 API 列表观察到，可能被 push 覆盖或调度延迟）；
两 run 的所有 step 结论均为 `success`，但存在两个问题（步骤级 success 有误导性）：

### 6.2 问题 A：训练步骤 34–36 分钟，逼近 job 40 分钟超时

- 证据：run 元数据中 step #9 耗时 2026s / 2160s，job 总时长约 35 分钟；`timeout-minutes: 40`；
- 定位：GitHub 共享 runner IP 大概率被东财限流，`fetch_data.py` 先试 AKShare；akshare 内部重试 + 我们的退避
  在 12 只票上累积成 30+ 分钟（本地单票 AKShare 失败只需 ~7s，说明 Actions 网络环境下 akshare 内部等待更久）；
- 训练侧已处理（2026-11-26 晚）：
  1. `fetch_data.py` 新增 **AKShare 连续失败熔断**（默认 2 次，`--akshare-fail-fast 1` 可更快切换），
     熔断后本进程直接 Baostock → 预计 CI 训练步骤回到 2–5 分钟；
  2. 新增 **Baostock 自动重登录**（会话被并发进程踢下线时自愈），大批量分片抓取时更稳；
  3. workflow 建议同步加 `--retries 1 --akshare-fail-fast 1`。

### 6.3 问题 B：训练步骤显示成功，但模型没有提交回仓库

- 证据：
  1. 仓库 `model/latest_model.json` 的提交历史只有 **1 个** commit（`9aad07b2` feat: 训练管道初始版本 10:25），
     之后两次 run 都未产生更新；
  2. `training/data/zt_pool.parquet` 的提交历史为 **0**（从未入库）；
  3. 最近 remote commits 中 quant-bot 的最后一笔是 **2026-11-25 19:31**（旧 softmax 归档步骤），
     训练接入（10:25 提交）之后的两次 run 都没有任何 quant-bot commit；
  4. run 里 step #10 耗时 **0s**：git commit 快速失败且被 `|| echo "无变更"` 吞掉。
- 定位（run 日志需 admin 权限，API 返回 403，以下基于元数据与仓库历史）：
  **`训练模型提交` 步骤没有设置 `git config user.name/email`**。GitHub 托管 runner 默认无 git 身份，
  `git commit` 会报 "Author identity unknown" 并失败；该步骤写了 `|| echo "无变更"`，于是静默通过。
  同 workflow 后面的"归档模型"步骤有 `git config`，所以历史上 quant-bot 能提交，恰好掩盖了这个问题。
  次要问题：`training/data/zt_pool.parquet`、`training/out/metrics.json` 此前被 `training/.gitignore` 忽略，
  `git add` 会报 ignored 错误（虽不阻止其它 pathspec staging，但需要 `-f` 才能纳入）。
- 训练侧已处理：`training/.gitignore` 已对这两个文件用 `!` 放行（保留其它忽略）。
- **workflow 需 Lead/维护者修**（`.github/workflows/daily-quant.yml`，不在训练侧写权限内）：

```yaml
- name: 提交训练模型与涨跌停池增量
  continue-on-error: true
  run: |
    git config user.name "quant-bot"
    git config user.email "quant-bot@users.noreply.github.com"
    git add model/
    [ -f training/data/zt_pool.parquet ] && git add training/data/zt_pool.parquet || true
    [ -f training/out/metrics.json ] && git add training/out/metrics.json || true
    git commit -m "chore(model): LightGBM $(date -u +%F)" || echo "无变更"
    git push || echo "push 跳过"
```

> 修复后正确行为：每日 run 产生一笔 `quant-bot: chore(model): LightGBM YYYY-MM-DD` 提交，
> `model/latest_model.json` 的 `trained_at`/指标每日更新；若模型无变化则"无变更"。

---

## 7. 本地验证记录（2026-11-26）

### 7.1 三步验收（本次 task-18 命令）

```bash
python training/fetch_data.py --symbols 600519,000001 --start 2024-01-01 --skip-zt
python training/train_lgbm.py
python training/export_model.py
```

- 本机东财限流：AKShare 失败 → 自动 Baostock 兜底（`source=baostock`），采集成功；
- 训练池：**62 只票** × 2024-01-02~2026-11-26（37,210 行，来自增量缓存/全市场子集）；
  指数 2106 行（000300/000001/399001）；
- 训练：22 折 Walk-Forward、OOF n=26,562；`training/out/metrics.json` 产出；
- 导出：`model/latest_model.json` **重新生成**（332,284 bytes ≈ 324.5KB，360 棵树 × 30 特征，
  class 顺序 up/flat/down）；`validate_model_json` 通过；Python 参考推理 vs booster **max|Δ|≈1.1e-16**；
- 指标（小样本/子集，仅验证链路，不代表模型有效）：

| 指标 | 值 |
|---|---|
| accuracy | 45.94% |
| logloss | 0.9622 |
| brier | 0.5994 |
| win_rate（交易口径） | 45.95%，Wilson 95% CI [45.35%, 46.55%]，26,555 笔 |
| long / short win_rate | 46.43%（13,344）/ 45.46%（13,211） |
| avg_net_return / payoff | -0.191% / 0.944 |

> 注意：胜率 < 50%、单笔净收益为负 → 小样本上**没有证明可交易 edge**；模型几乎不预测 flat。
> 不要在子集结果上调参（过拟合风险）；正式结论以 40 只池 × 3 年全量 Walk-Forward 为准。

### 7.2 并发抓取注意（本次遇到）

本机同时有多个 Baostock 分片抓取进程（`training/data_full/shard_*`）时，匿名会话会互相踢下线，
报 `用户未登录`。训练侧已加自动重登录自愈；**大批量抓取请单进程串行/分片逐个跑**，避免并发。

### 7.3 正式训练建议

- 40 只池 × 近 3 年：在 **Kaggle Notebook（CPU）** 按 `training/README.md` 三步跑（10–20 分钟），
  下载 `model/` 两个 JSON 提交；或等 Actions 修复后由每日任务自动更新；
- 每次训练后务必跑 `python training/export_model.py --validate-only model/latest_model.json` 与
  `python -m pytest tests/test_training_features.py -q`。

## 8. 遗留与待办

| # | 事项 | 负责 | 状态 |
|---|---|---|---|
| 1 | Actions 提交步骤补 `git config user.name/email`，让每日模型真正入库 | Lead/维护者（workflow 权限） | 待修（训练侧已出方案，见 §6.3） |
| 2 | Actions 训练步骤加 `--retries 1 --akshare-fail-fast 1`，避免 30+ 分钟 | Lead/维护者 | 待改（训练侧熔断已就绪） |
| 3 | 涨跌停池历史靠每日累积：确认 workflow 每日提交 `training/data/zt_pool.parquet` | Lead/维护者 | 待确认 |
| 4 | 40 只池全量训练（Kaggle CPU）产出正式模型并替换当前 8 票小样本模型 | Lead/model-dev | 待安排 |
| 5 | Worker `model_infer.ts` 按 §3.3 树编码实现推理 + Python/TS parity 测试 | cloud-dev | 待接入 |
| 6 | `training/tpu/` 目录后续可删除（当前保留归档 + DEPRECATED.md） | Lead | 待决策 |
| 7 | `cloudflare/worker/README.md`、`deploy/vps/README.md` 中仍有 TPU 表述，不属训练侧文档，需各自维护者同步 | cloud-dev / Lead | 待同步 |

---

## 9. 复现命令速查

```bash
# 数据（AKShare 主 + Baostock 兜底；CI 建议加 --retries 1 --akshare-fail-fast 1）
python training/fetch_data.py                                   # 40 只池 × 近 3 年
python training/fetch_data.py --symbols 600519,000001 --start 2024-01-01 --skip-zt   # 小样本
python training/fetch_data.py --synthetic                       # 离线合成（无网演示）

# 训练（Walk-Forward 250/20/20）
python training/train_lgbm.py
python training/train_lgbm.py --synthetic                       # 离线跑通

# 导出模型 JSON（< 2MB，自动裁剪）
python training/export_model.py
python training/export_model.py --validate-only model/latest_model.json

# 离线测试
python -m pytest tests/test_training_features.py -q
```
