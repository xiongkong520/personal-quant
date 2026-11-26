# 全市场数据 + CPU 训练 · VPS 方案

> 目标：**一台境内小服务器**接管「全市场行情抓取 → 训练 → 模型导出 → 推回仓库」，
> 手机只当看板。不需要 GPU，不需要 Kaggle 验证，月度成本可以压到一杯咖啡。
>
> ⚠️ 仅供学习研究，非投资建议。

---

## 1. 为什么用 VPS（而不是手机 / Kaggle）

| 方案 | 抓全市场 5,000 只 | 训练 | 结论 |
|---|---|---|---|
| 手机（现在这台） | Baostock ~3 秒/只 → 4 小时+，且耗电、断网就断 | 8 核 ARM，能跑但不稳 | 只适合调试 |
| Kaggle Notebook | 需要手机验证 + Internet，全市场抓取容易被限流 | 免费但每次会话有时限 | 已被验证权限卡死 |
| **境内 VPS** | 直连 Baostock，**1 秒/只，4 分片并行 ≈ 20~30 分钟** | 2C4G 跑 LightGBM 完全够 | ✅ 社区常规做法 |
| 境外 VPS | 访问东财/baostock 反而慢、易被限流 | 同 CPU | ❌ 不推荐（延迟高） |

**社区标准分工**：境内轻量云 → 抓数据 + 训练（CPU）；Cloudflare Workers/Pages → 托管看板（你已经在用）；GPU 只在要做深度学习实验时按小时租（AutoDL 3090 约 ¥1.2/小时），日常训练用不上。

---

## 2. 机型与成本

| 配置 | 能干什么 | 参考价 |
|---|---|---|
| 2 核 4G / 60G 盘（**推荐**） | 全市场抓取 + LightGBM 训练（800 只池）；NN 可选跑小池 | 新用户 ¥60~120/年，续费 ¥30~60/月 |
| 4 核 8G / 80G 盘 | 抓取更快（8 分片），NN 训练池可到 500 只 | ¥300~600/年 |
| 2 核 2G | 只做抓取（训练换 GitHub Actions） | ¥40~100/年 |

- 系统选 **Ubuntu 22.04 / 24.04**，国内节点（阿里云/腾讯云轻量、华为云耀、UCloud 均可，别选境外节点）
- 磁盘：全市场 5,000 只 × 3 年 parquet ≈ **0.5~0.8 GB**，60G 盘绰绰有余
- 出站流量：每天增量只补当日数据，流量可忽略

---

## 3. 三步装好

```bash
# 1) 登录服务器（root 或 sudo 用户）
ssh root@你的服务器IP

# 2) 一键安装（clone + venv + 依赖 + 首次全市场抓取 + 定时任务）
curl -fsSL https://raw.githubusercontent.com/xiongkong520/personal-quant/main/deploy/vps/bootstrap.sh | bash

# 3) 让它能把训练结果推回仓库
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub          # 贴到 GitHub 仓库 Settings → Deploy keys，勾 Allow write access
cd /opt/personal-quant && git remote set-url origin git@github.com:xiongkong520/personal-quant.git
```

装完检查：

```bash
systemctl list-timers quant-train.timer   # 定时任务（每交易日 17:40 北京时间）
journalctl -u quant-train.service -f      # 实时日志
bash /opt/personal-quant/deploy/vps/daily_train.sh   # 手动跑一次
```

---

## 4. 每天它做什么（`deploy/vps/daily_train.sh`）

1. **全市场抓取**：`training/tpu/fetch_full_market.py`，Baostock 单源、4 分片并行，增量补当日；
2. **切训练子集**：`make_train_subset.py` 按近 60 日成交额取流动性前 N 只（默认 800）——冷门股流动性差、滑点大，本来就不该进交易模型；
3. **LightGBM Walk-Forward**：`train_lgbm.py` + `export_model.py` → `model/latest_model.json`（Worker 直接加载）；
4. **可选 NN**：`NN_LIMIT=200` 时跑 `training/tpu/nn_tf.py`（GRU，CPU）→ `model/nn_model.json`；
5. **提交**：`git add model/ && git commit && git push`，Cloudflare Pages 下一次构建自动带上新模型。

关键参数都在脚本头部，可用环境变量覆盖：

```bash
UNIVERSE=1500 WORKERS=6 LGBM_LIMIT=1200 NN_LIMIT=300 bash deploy/vps/daily_train.sh
```

---

## 5. 本机试跑（不买服务器也能验证）

```bash
# 只抓成交额前 600 只，验证整条链路
python training/tpu/fetch_full_market.py --workers 4 --limit 600 --start 2024-01-01
python training/tpu/make_train_subset.py --src training/data_full --limit 400 --out training/data
python training/train_lgbm.py --daily training/data/daily.parquet --index training/data/index.parquet
python training/export_model.py
```

---

## 6. 已知限制（照实写）

- **Baostock 不覆盖北交所**，股票池里 4/8/9 开头的会被过滤；
- **涨跌停池**（东财接口）只保留最近约 30 个交易日，历史情绪特征靠 `zt_pool.parquet` 逐日累积，新机器上会缺失一段时间（训练脚本会填 0 并标记 `sentiment_missing`）；
- **神经网络在这套数据上没有稳定方向性**：40 只池上 GRU 的方向子集 AUC ≈ 0.50、准确率低于「永远猜跌」；同折 LightGBM 约 0.45 vs 多数类 0.43。扩到全市场后是否改善需要重新验证，**不要把它当成靠谱的预测器**；
- 定时任务依赖服务器时区，timer 里已显式写 `Asia/Shanghai`；
- 若服务器内存 < 2G，LightGBM 训练池请压到 400 只以内，否则可能 OOM。
