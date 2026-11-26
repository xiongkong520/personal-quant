#!/usr/bin/env python3
"""TPU 序列模型 · 与框架无关的核心（数据 → 窗口 → 折 → 导出 → 参考前向）

设计原则
--------
1. **只复用已验证的特征口径**：直接调用 ``training/train_lgbm.py`` 的
   ``build_training_dataset``，保证 LightGBM 与神经网络看到的特征、标签完全一致
   （30 个特征、T+1 可成交三分类、全部 ``<=t`` 数据 + 情绪特征 lag1）。
2. **与框架无关**：本文件只依赖 numpy / pandas，不 import tensorflow。
   数据切窗、标准化、Walk-Forward 折、模型导出、numpy 参考前向都在这里，
   TPU 训练器（``nn_tf.py``）只负责"把同样的数学放到 TPU 上跑"。
3. **导出可被 JS/TS 复算**：``export_model`` 产出的 JSON 只有矩阵 + 归一化统计，
   前端/Worker 用三十行矩阵乘法就能复现；``predict_numpy`` 就是这份 JSON 的参考实现，
   TPU 跑完会用它和 TensorFlow 输出做逐样本对齐（parity check）。

模型结构（GRU 变体，单隐层 + 两层 MLP 头）
------------------------------------------
    x (T=20, F=30)            ← 标准化后的日频特征窗口
      └─ GRU(H=48)：h_t = (1-z)*h_{t-1} + z*tanh(x_t·W_h + (r*h_{t-1})·U_h + b_h)
      └─ Dense(H→64, relu) → Dense(64→3, logits) → softmax

用法（自检，无需 tensorflow）
------------------------------
    python training/tpu/nn_core.py --self-test
    python training/tpu/nn_core.py --self-test --data-dir training/data --seq-len 20
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

TPU_DIR = Path(__file__).resolve().parent
TRAINING_DIR = TPU_DIR.parent
REPO_DIR = TRAINING_DIR.parent

SCHEMA = "nn_gru_v1"
DEFAULT_SEQ_LEN = 20


# ---------------------------------------------------------------------------
# 复用 train_lgbm.py（不复制特征公式，避免两套口径漂移）
# ---------------------------------------------------------------------------
_TL_CACHE: Any = None


def load_train_lgbm():
    """按文件路径导入 training/train_lgbm.py（仓库脚本，非包）。"""
    global _TL_CACHE
    if _TL_CACHE is not None:
        return _TL_CACHE
    path = TRAINING_DIR / "train_lgbm.py"
    if not path.is_file():
        raise FileNotFoundError(f"找不到 {path}")
    spec = importlib.util.spec_from_file_location("train_lgbm", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules.setdefault("train_lgbm", module)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    _TL_CACHE = module
    return module


def feature_columns() -> list[str]:
    return list(load_train_lgbm().FEATURE_COLUMNS)


def schema_classes() -> list[str]:
    return list(load_train_lgbm().SCHEMA_CLASSES)


# ---------------------------------------------------------------------------
# 1. 数据装载
# ---------------------------------------------------------------------------
def load_frames(data_dir: str | Path) -> tuple[pd.DataFrame, pd.DataFrame | None, pd.DataFrame | None]:
    """读取 fetch_data.py 产出的 parquet（缺失的辅助表返回 None）。"""
    from pathlib import Path as _Path

    base = _Path(data_dir)
    daily_path = base / "daily.parquet"
    if not daily_path.is_file():
        raise FileNotFoundError(f"缺少 {daily_path}，先跑 training/fetch_data.py")
    daily = pd.read_parquet(daily_path)
    zt = pd.read_parquet(base / "zt_pool.parquet") if (base / "zt_pool.parquet").is_file() else None
    index = pd.read_parquet(base / "index.parquet") if (base / "index.parquet").is_file() else None
    return daily, zt, index


def build_rows(
    daily: pd.DataFrame,
    zt_pool: pd.DataFrame | None = None,
    index: pd.DataFrame | None = None,
    label_cost: float = 0.002,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """特征 + 标签（直接复用 train_lgbm.build_training_dataset）。"""
    tl = load_train_lgbm()
    return tl.build_training_dataset(daily, zt_pool, index, label_cost=label_cost)


# ---------------------------------------------------------------------------
# 2. 切窗（每只股票自己的时间轴，窗口不跨股票）
# ---------------------------------------------------------------------------
def make_sequences(
    df: pd.DataFrame,
    seq_len: int = DEFAULT_SEQ_LEN,
    feature_cols: list[str] | None = None,
) -> tuple[np.ndarray, pd.DataFrame]:
    """把 (symbol, date) 行级样本扩成 (symbol, date, 过去 seq_len 天) 窗口。

    - 窗口内每天的特征都是"当天收盘后可得"的（train_lgbm 已保证），因此无未来函数；
    - 只保留标签有效（label 非空）且窗口完全落在该股票历史内的样本；
    - 返回 ``X: (n, seq_len, F) float32`` 与对齐的 ``meta_df``（symbol/date/label/label_ret）。
    """
    feature_cols = list(feature_cols or feature_columns())
    if seq_len < 2:
        raise ValueError("seq_len 至少为 2")
    need = ["symbol", "trade_date", "label", "label_ret", *feature_cols]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"缺少列：{missing}")

    work = df[need].sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    n_feat = len(feature_cols)
    windows: list[np.ndarray] = []
    meta_rows: list[dict[str, Any]] = []

    for symbol, group in work.groupby("symbol", sort=True):
        arr = group[feature_cols].to_numpy(dtype=np.float32)
        dates = group["trade_date"].to_numpy()
        labels = group["label"].to_numpy()
        rets = group["label_ret"].to_numpy(dtype=np.float64)
        n = len(group)
        if n < seq_len:
            continue
        # 用滑动窗口视图 (n-seq+1, seq, F) —— 只挑标签有效的行，避免逐行 copy
        view = np.lib.stride_tricks.sliding_window_view(arr, (seq_len, n_feat))[:, 0]
        for i in range(seq_len - 1, n):
            lab = labels[i]
            if lab is None or (isinstance(lab, float) and math.isnan(lab)):
                continue
            if not np.isfinite(rets[i]):
                continue
            windows.append(view[i - seq_len + 1])
            meta_rows.append(
                {
                    "symbol": symbol,
                    "trade_date": str(dates[i]),
                    "label": str(lab),
                    "label_ret": float(rets[i]),
                }
            )

    if not windows:
        raise RuntimeError("窗口为空：检查 seq_len 与数据长度")

    X = np.ascontiguousarray(np.stack(windows), dtype=np.float32)
    meta = pd.DataFrame(meta_rows)
    meta["label_idx"] = [schema_classes().index(x) for x in meta["label"]]
    return X, meta


# ---------------------------------------------------------------------------
# 3. Walk-Forward 折（与 train_lgbm.walk_forward 同一套日期切法）
# ---------------------------------------------------------------------------
def fold_splits(
    dates: list[str] | np.ndarray,
    train_days: int = 250,
    valid_days: int = 40,
    step: int | None = None,
    max_folds: int = 6,
) -> list[dict[str, Any]]:
    """按交易日切滚动折：训练窗在验证窗之前，逐折重训，收集样本外预测。"""
    uniq = sorted({str(d) for d in dates})
    step = int(step or valid_days)
    if len(uniq) < train_days + valid_days:
        raise RuntimeError(f"只有 {len(uniq)} 个交易日，不足 train_days({train_days})+valid_days({valid_days})")
    splits: list[dict[str, Any]] = []
    start = train_days
    while start + valid_days <= len(uniq) and len(splits) < max_folds:
        splits.append(
            {
                "fold": len(splits),
                "train_dates": uniq[start - train_days:start],
                "valid_dates": uniq[start:start + valid_days],
            }
        )
        start += step
    if not splits:
        raise RuntimeError("没有产生任何有效折")
    return splits


# ---------------------------------------------------------------------------
# 4. 标准化（统计量只来自训练窗）
# ---------------------------------------------------------------------------
def fit_norm(X: np.ndarray) -> dict[str, list[float]]:
    flat = X.reshape(-1, X.shape[-1]).astype(np.float64)
    mean = flat.mean(axis=0)
    std = flat.std(axis=0)
    std = np.where((std < 1e-9) | ~np.isfinite(std), 1.0, std)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    return {"mean": mean.tolist(), "std": std.tolist()}


def apply_norm(X: np.ndarray, norm: dict[str, list[float]], clip: float = 8.0) -> np.ndarray:
    mean = np.asarray(norm["mean"], dtype=np.float32)
    std = np.asarray(norm["std"], dtype=np.float32)
    out = (X - mean) / std
    if clip:
        out = np.clip(out, -clip, clip)
    return out.astype(np.float32)


# ---------------------------------------------------------------------------
# 5. 前向：numpy 参考实现（与 nn_tf.py 的 TF 图逐算子对齐）
# ---------------------------------------------------------------------------
def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0)))


def gru_forward(p: dict[str, Any], X: np.ndarray) -> np.ndarray:
    """X 已标准化，形状 (n, T, F)。返回 (n, H) 末步隐状态。

    与 nn_tf.py 中 ``gru_forward_tf`` 同一套公式：
        z  = sigmoid(x_t·Wz + h·Uz + bz)
        r  = sigmoid(x_t·Wr + h·Ur + br)
        h' = tanh(x_t·Wh + (r*h)·Uh + bh)
        h  = (1-z)*h + z*h'
    """
    W = np.asarray(p["gru_kernel"], dtype=np.float32)        # (F, 3H)
    U = np.asarray(p["gru_recurrent"], dtype=np.float32)     # (H, 3H)
    b = np.asarray(p["gru_bias"], dtype=np.float32)          # (3H,)
    n, T, F = X.shape
    H = U.shape[0]
    Wz, Wr, Wh = W[:, :H], W[:, H:2 * H], W[:, 2 * H:]
    Uz, Ur, Uh = U[:, :H], U[:, H:2 * H], U[:, 2 * H:]
    bz, br, bh = b[:H], b[H:2 * H], b[2 * H:]

    xz_all = X.reshape(n * T, F) @ W + b                     # (n*T, 3H)
    xz_all = xz_all.reshape(n, T, 3 * H)
    h = np.zeros((n, H), dtype=np.float32)
    for t in range(T):
        xt = xz_all[:, t, :]
        z = sigmoid(xt[:, :H] + h @ Uz)
        r = sigmoid(xt[:, H:2 * H] + h @ Ur)
        hh = np.tanh(xt[:, 2 * H:] + (r * h) @ Uh)
        h = (1.0 - z) * h + z * hh
    return h


def mlp_forward(p: dict[str, Any], h: np.ndarray) -> np.ndarray:
    W1 = np.asarray(p["dense1_kernel"], dtype=np.float32)
    b1 = np.asarray(p["dense1_bias"], dtype=np.float32)
    W2 = np.asarray(p["dense2_kernel"], dtype=np.float32)
    b2 = np.asarray(p["dense2_bias"], dtype=np.float32)
    d = np.maximum(h @ W1 + b1, 0.0)
    return d @ W2 + b2


def softmax(logits: np.ndarray) -> np.ndarray:
    z = logits - logits.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def predict_logits(export: dict[str, Any], X_raw: np.ndarray) -> np.ndarray:
    X = apply_norm(np.asarray(X_raw, dtype=np.float32), export["norm"])
    return mlp_forward(export["params"], gru_forward(export["params"], X))


def predict_numpy(export: dict[str, Any], X_raw: np.ndarray) -> np.ndarray:
    """导出 JSON 的参考推理（Worker/TS 端应能复现同一结果）。"""
    return softmax(predict_logits(export, X_raw))


# ---------------------------------------------------------------------------
# 6. 导出 JSON（矩阵四舍五入，控制在 2MB 以内）
# ---------------------------------------------------------------------------
def _round_matrix(arr: np.ndarray, digits: int = 6) -> list:
    return np.round(np.asarray(arr, dtype=np.float64), digits).tolist()


def export_model(
    params: dict[str, np.ndarray],
    norm: dict[str, list[float]],
    metrics: dict[str, Any],
    seq_len: int,
    units: int,
    dense: int,
    version: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feature_cols = feature_columns()
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "version": version,
        "task": "A股 T+1 三分类（次日开盘买/收盘卖，扣双边摩擦）",
        "classes": schema_classes(),
        "features": feature_cols,
        "seq_len": int(seq_len),
        "arch": {
            "type": "gru_mlp",
            "units": int(units),
            "dense": int(dense),
            "gate_order": ["z", "r", "h"],
            "gru_formula": "z=sig(x@Wz+h@Uz+bz); r=sig(x@Wr+h@Ur+br); "
                           "h'=tanh(x@Wh+(r*h)@Uh+bh); h=(1-z)*h+z*h'",
            "activation": "relu",
            "logits_then_softmax": True,
        },
        "norm": {"mean": _round_matrix(norm["mean"]), "std": _round_matrix(norm["std"]), "clip": 8.0},
        "params": {
            "gru_kernel": _round_matrix(params["gru_kernel"]),
            "gru_recurrent": _round_matrix(params["gru_recurrent"]),
            "gru_bias": _round_matrix(params["gru_bias"]),
            "dense1_kernel": _round_matrix(params["dense1_kernel"]),
            "dense1_bias": _round_matrix(params["dense1_bias"]),
            "dense2_kernel": _round_matrix(params["dense2_kernel"]),
            "dense2_bias": _round_matrix(params["dense2_bias"]),
        },
        "metrics": metrics,
    }
    if extra:
        payload.update(extra)
    return payload


def export_size_mb(payload: dict[str, Any]) -> float:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) / 1024 / 1024


# ---------------------------------------------------------------------------
# 7. 评估 / 融合
# ---------------------------------------------------------------------------
def evaluate_oof(
    meta: pd.DataFrame,
    proba: np.ndarray,
    label_cost: float = 0.002,
) -> dict[str, Any]:
    tl = load_train_lgbm()
    out = tl.evaluate_predictions(
        meta["label"].to_numpy(), proba, meta["label_ret"].to_numpy(), cost=label_cost
    )
    out["n"] = int(len(meta))
    return out


def class_weights(labels: np.ndarray, n_classes: int = 3, cap: float = 4.0) -> np.ndarray:
    counts = np.bincount(labels, minlength=n_classes).astype(np.float64)
    counts = np.maximum(counts, 1.0)
    w = counts.sum() / (n_classes * counts)
    w = np.minimum(w, cap)
    return (w / w.mean()).astype(np.float32)


def blend_probs(p_a: np.ndarray, p_b: np.ndarray, weight_a: float = 0.5) -> np.ndarray:
    return weight_a * p_a + (1.0 - weight_a) * p_b


def oof_frame(meta: pd.DataFrame, proba: np.ndarray, fold_ids: np.ndarray) -> pd.DataFrame:
    classes = schema_classes()
    part = meta[["symbol", "trade_date", "label", "label_ret"]].copy()
    for i, c in enumerate(classes):
        part[f"prob_{c}"] = proba[:, i]
    part["pred"] = [classes[i] for i in np.argmax(proba, axis=1)]
    part["fold"] = fold_ids.astype(int)
    return part


# ---------------------------------------------------------------------------
# 8. 自检（无 tensorflow）
# ---------------------------------------------------------------------------
def _self_test(data_dir: str, seq_len: int) -> int:
    print("[1] 读取 parquet:", data_dir)
    daily, zt, index = load_frames(data_dir)
    print(f"    daily={daily.shape} symbols={daily['symbol'].nunique()} zt={None if zt is None else zt.shape}")
    df, meta = build_rows(daily, zt, index)
    print(f"[2] 特征样本 {len(df)} 行，{df['symbol'].nunique()} 只，{df['trade_date'].nunique()} 天")
    X, seq_meta = make_sequences(df, seq_len=seq_len)
    print(f"[3] 窗口 X={X.shape} 标签分布={seq_meta['label'].value_counts().to_dict()}")
    splits = fold_splits(seq_meta["trade_date"].tolist(), 250, 40, step=60, max_folds=3)
    for s in splits:
        print(f"    fold {s['fold']}: train {s['train_dates'][0]}~{s['train_dates'][-1]} "
              f"valid {s['valid_dates'][0]}~{s['valid_dates'][-1]}")
    train_idx = seq_meta["trade_date"].isin(splits[0]["train_dates"]).to_numpy()
    norm = fit_norm(X[train_idx])
    print(f"[4] 归一化 mean[0]={norm['mean'][0]:.4f} std[0]={norm['std'][0]:.4f}")

    rng = np.random.default_rng(0)
    F, H, D = X.shape[-1], 16, 32
    params = {
        "gru_kernel": (rng.normal(0, 0.1, (F, 3 * H))).astype(np.float32),
        "gru_recurrent": (rng.normal(0, 0.1, (H, 3 * H))).astype(np.float32),
        "gru_bias": np.zeros(3 * H, dtype=np.float32),
        "dense1_kernel": (rng.normal(0, 0.1, (H, D))).astype(np.float32),
        "dense1_bias": np.zeros(D, dtype=np.float32),
        "dense2_kernel": (rng.normal(0, 0.1, (D, 3))).astype(np.float32),
        "dense2_bias": np.zeros(3, dtype=np.float32),
    }
    export = export_model(params, norm, {"note": "self-test 随机权重，仅验证形状/导出"}, seq_len, H, D, "self-test")
    probs = predict_numpy(export, X[:64])
    print(f"[5] 参考前向 probs={probs.shape} 行和="
          f"{np.round(probs.sum(axis=1)[:3], 6).tolist()} 最大={probs.max():.4f}")
    payload_mb = export_size_mb(export)
    print(f"[6] 导出 JSON 结构 OK，随机权重大小 {payload_mb:.2f} MB（正式模型 H=48 时约 "
          f"{payload_mb * (48 / H) ** 0.5:.2f} MB 量级）")
    assert probs.shape == (64, 3) and np.allclose(probs.sum(axis=1), 1.0, atol=1e-5)
    print("[self-test] 全部通过 ✅")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TPU 序列模型核心自检（不依赖 tensorflow）")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--data-dir", default=str(TRAINING_DIR / "data"))
    ap.add_argument("--seq-len", type=int, default=DEFAULT_SEQ_LEN)
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test(args.data_dir, args.seq_len)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
