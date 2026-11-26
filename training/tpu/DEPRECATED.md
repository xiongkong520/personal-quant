# ⚠️ 已弃用：Kaggle TPU 训练路径（保留仅作参考）

**状态：2026-11-26 起弃用，不再维护。**

## 为什么弃用

1. **Kaggle TPU 需要身份验证**：用户尝试创建 TPU Notebook 时身份验证未通过（截图已确认），
   免费 TPU 配额实际不可用，这条路径无法稳定跑通；
2. **没有必要**：正式模型是 **LightGBM 三分类**（`training/train_lgbm.py`），
   PyPI wheel 是 CPU 版；40 只票 × 3 年数据量很小，CPU 训练 1 分钟级、导出模型 < 2MB。
   TPU/GPU 对树模型没有收益（LightGBM GPU 也需源码编译，社区不推荐为这点数据折腾）；
3. **维护成本**：神经网络路径（GRU/TF 全市场）依赖 Kaggle 环境与推送流程，与每日 GitHub Actions
   CPU 训练主线重复，且没有进入线上推理链路（Worker 只加载 `model/latest_model.json` 树模型）。

## 替代方案（当前正式路径）

```bash
# 每日训练：GitHub Actions（CPU）或本地/Colab CPU 均可
python training/fetch_data.py        # AKShare 主 + Baostock 兜底
python training/train_lgbm.py        # LightGBM 三分类 Walk-Forward
python training/export_model.py      # → model/latest_model.json
```

详见 `training/README.md` 与 `docs/TRAINING_NOTES.md`。

## 目录说明（仅归档，不要在新流程里引用）

- `fetch_full_market.py` / `make_train_subset.py`：全市场样本准备（Baostock 分片抓取）
- `nn_core.py` / `nn_tf.py`：TensorFlow/Keras GRU 序列模型（TPU 训练用）
- `kernels.py` / `push.py`：Kaggle kernel 版本管理与推送
- `kernel_probe/`、`kernel_main/`、`kaggle_dataset/`、`kaggle_out/`、`universe_240.txt`：Kaggle 运行产物/数据集

> 如需恢复 TPU 路线：先解决 Kaggle 身份验证，再按 `training/tpu/` 内脚本自行评估；
> 建议在恢复前先确认线上推理链路（Worker `model_infer.ts`）是否愿意支持 TF 模型格式。
