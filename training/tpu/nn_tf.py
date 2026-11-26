#!/usr/bin/env python3
"""TPU 训练器：GRU 序列模型 + 原生 TensorFlow 手写训练环（TPUStrategy / GPU / CPU 通用）

为什么不用 Keras
----------------
TF 2.16+ 自带 Keras 3，而 Keras 3 对 ``TPUStrategy`` 支持很差（官方建议 TPU 走 JAX 或回退
``tf_keras``）。这里直接绕开 Keras：变量、前向图、梯度、优化器全部原生 TF 写，
在 Kaggle 免费 TPU v5e-8 和 CPU 上是同一份代码，每一步算的是什么都看得见。

流程
----
1. 复用 ``nn_core`` 的切窗/折/标准化（与 LightGBM 完全同口径的 30 特征、T+1 三分类标签）；
2. 逐折在 TPU 上训练（8 核数据并行），收集样本外概率；
3. 权重导成 numpy → 写 ``nn_model.json``（前端/Worker 可用矩阵乘法复算）；
4. numpy 参考前向复算 → parity check（导出 JSON 与训练图必须一致，阈值 1e-4）；
5. 可选：同折 LightGBM 基线 + 概率融合，量化 TPU 模型有没有增量。

用法
----
    python training/tpu/nn_tf.py --out-dir /kaggle/working/out --epochs 12 --max-folds 6
    python training/tpu/nn_tf.py --smoke            # 1 折 1 epoch，验证环境
    python training/tpu/nn_tf.py --baseline-lgbm    # 附带同折 LightGBM 对比与融合

产物（--out-dir）
-----------------
``nn_model.json`` / ``nn_metrics.json`` / ``nn_oof.parquet`` / ``env.json``
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nn_core as core  # noqa: E402

PARAM_ORDER = (
    "gru_kernel",
    "gru_recurrent",
    "gru_bias",
    "dense1_kernel",
    "dense1_bias",
    "dense2_kernel",
    "dense2_bias",
)


def log(msg: str) -> None:
    print(f"[nn_tf] {msg}", flush=True)


def fmt(value: Any, digits: int = 4) -> str:
    return "None" if value is None else f"{value:.{digits}f}"


# ---------------------------------------------------------------------------
# 设备探测
# ---------------------------------------------------------------------------
def detect_device(tf) -> tuple[Any, dict[str, Any]]:
    """优先 TPU，其次 GPU，最后 CPU。返回 (strategy, info)。"""
    info: dict[str, Any] = {
        "tensorflow": tf.__version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "tried": [],
    }
    for kwargs in ({"tpu": "local"}, {}):
        try:
            resolver = tf.distribute.cluster_resolver.TPUClusterResolver(**kwargs)
            tf.config.experimental_connect_to_cluster(resolver)
            tf.tpu.experimental.initialize_tpu_system(resolver)
            strategy = tf.distribute.TPUStrategy(resolver)
            info.update(
                {
                    "accelerator": "tpu",
                    "resolver": kwargs.get("tpu", "auto"),
                    "num_replicas": int(strategy.num_replicas_in_sync),
                    "devices": [d.name for d in tf.config.list_logical_devices("TPU")],
                }
            )
            log(f"TPU 就绪：{strategy.num_replicas_in_sync} 核")
            return strategy, info
        except Exception as exc:  # noqa: BLE001
            info["tried"].append({"kwargs": kwargs or "auto", "error": f"{type(exc).__name__}: {exc}"})

    gpus = tf.config.list_physical_devices("GPU")
    if gpus:
        strategy = tf.distribute.MirroredStrategy()
        info.update(
            {
                "accelerator": "gpu",
                "num_replicas": int(strategy.num_replicas_in_sync),
                "devices": [d.name for d in gpus],
            }
        )
        log(f"GPU 就绪：{len(gpus)} 张")
        return strategy, info

    info.update({"accelerator": "cpu", "num_replicas": 1, "devices": []})
    log("未发现 TPU/GPU，回退 CPU（结果有效，只是慢）")
    return tf.distribute.get_strategy(), info


# ---------------------------------------------------------------------------
# 参数 / 前向 / 训练环
# ---------------------------------------------------------------------------
def init_params(tf, scope, n_feat: int, units: int, dense: int, seed: int = 42) -> dict[str, Any]:
    """在 strategy.scope() 内建变量。门顺序固定 [z, r, h]。"""
    rng = np.random.default_rng(seed)

    def glorot(fan_in: int, fan_out: int) -> np.ndarray:
        limit = np.sqrt(2.0 / (fan_in + fan_out))
        return rng.normal(0.0, limit, (fan_in, fan_out)).astype(np.float32)

    with scope:
        params = {
            "gru_kernel": tf.Variable(glorot(n_feat, 3 * units), name="gru_kernel"),
            "gru_recurrent": tf.Variable(
                rng.normal(0.0, 0.08, (units, 3 * units)).astype(np.float32), name="gru_recurrent"
            ),
            # z 门偏置初始为正：初始更偏向"记住历史"，序列模型收敛更稳
            "gru_bias": tf.Variable(
                np.concatenate([np.full(units, 0.5, np.float32), np.zeros(2 * units, np.float32)]),
                name="gru_bias",
            ),
            "dense1_kernel": tf.Variable(glorot(units, dense), name="dense1_kernel"),
            "dense1_bias": tf.Variable(np.zeros(dense, np.float32), name="dense1_bias"),
            "dense2_kernel": tf.Variable(glorot(dense, 3), name="dense2_kernel"),
            "dense2_bias": tf.Variable(np.zeros(3, np.float32), name="dense2_bias"),
        }
    return params


def make_forward(tf, params: dict, seq_len: int, units: int, dropout: float):
    """返回 forward(x, training) -> logits (B, 3)；x 为已完成标准化的 (B, T, F)。

    与 ``nn_core.gru_forward`` 逐算子一致：
        z = sigmoid(x·Wz + h·Uz + bz)
        r = sigmoid(x·Wr + h·Ur + br)
        h'= tanh(x·Wh + (r*h)·Uh + bh)
        h = (1-z)*h + z*h'
    """
    W = params["gru_kernel"]
    U = params["gru_recurrent"]
    Uz, Ur, Uh = U[:, :units], U[:, units:2 * units], U[:, 2 * units:]
    b = params["gru_bias"]

    def gru_forward(x):
        batch = tf.shape(x)[0]
        h = tf.zeros([batch, units], dtype=tf.float32)
        # 先算全部时间步的输入投影（一次大矩阵乘，比每步算更省）
        flat = tf.reshape(x, [-1, tf.shape(x)[-1]])
        x_proj = tf.reshape(tf.matmul(flat, W) + b, [batch, seq_len, 3 * units])
        for t in range(seq_len):
            xt = x_proj[:, t, :]
            z = tf.sigmoid(xt[:, :units] + tf.matmul(h, Uz))
            r = tf.sigmoid(xt[:, units:2 * units] + tf.matmul(h, Ur))
            hh = tf.tanh(xt[:, 2 * units:] + tf.matmul(r * h, Uh))
            h = (1.0 - z) * h + z * hh
        return h

    def forward(x, training: bool = False):
        if dropout and training:
            x = tf.nn.dropout(x, rate=dropout)
        h = gru_forward(x)
        d = tf.nn.relu(tf.matmul(h, params["dense1_kernel"]) + params["dense1_bias"])
        if dropout and training:
            d = tf.nn.dropout(d, rate=dropout)
        return tf.matmul(d, params["dense2_kernel"]) + params["dense2_bias"]

    return forward


def make_adam(tf, variables: list, lr: float = 2e-3, beta1: float = 0.9, beta2: float = 0.999, eps: float = 1e-7):
    """手写 Adam（不依赖 Keras 优化器；TPU 上每个 replica 同步更新同一批变量）。"""
    m = [tf.Variable(tf.zeros_like(v), trainable=False, name=f"m_{i}") for i, v in enumerate(variables)]
    v2 = [tf.Variable(tf.zeros_like(x), trainable=False, name=f"v_{i}") for i, x in enumerate(variables)]
    step = tf.Variable(0, dtype=tf.int64, trainable=False, name="adam_step")

    def update(grads):
        step.assign_add(1)
        t = tf.cast(step, tf.float32)
        bc1 = 1.0 - tf.pow(beta1, t)
        bc2 = 1.0 - tf.pow(beta2, t)
        deltas = []
        for g, mm, vv in zip(grads, m, v2):
            mm.assign(beta1 * mm + (1.0 - beta1) * g)
            vv.assign(beta2 * vv + (1.0 - beta2) * tf.square(g))
            deltas.append(lr * (mm / bc1) / (tf.sqrt(vv / bc2) + eps))
        for delta, var in zip(deltas, variables):
            var.assign_sub(delta)

    return update


def train_fold(
    tf,
    strategy,
    forward,
    params: dict[str, Any],
    Xtr: np.ndarray,
    ytr: np.ndarray,
    wtr: np.ndarray,
    epochs: int,
    global_batch: int,
    lr: float,
    seed: int,
) -> tuple[float, list[float]]:
    """在 strategy 上数据并行训练，返回 (耗时秒, 每 epoch 平均 loss)。"""
    n = len(Xtr)
    steps_per_epoch = max(1, n // global_batch)

    ds = tf.data.Dataset.from_tensor_slices((Xtr, ytr.astype(np.int32), wtr.astype(np.float32)))
    ds = (
        ds.shuffle(min(n, 200_000), seed=seed, reshuffle_each_iteration=True)
        .repeat()
        .batch(global_batch, drop_remainder=True)
        .prefetch(tf.data.AUTOTUNE)
    )
    dist_ds = strategy.experimental_distribute_dataset(ds)

    variables = [params[k] for k in PARAM_ORDER]
    apply_adam = make_adam(tf, variables, lr=lr)
    num_replicas = int(strategy.num_replicas_in_sync)

    @tf.function
    def step_fn(batch):
        x, y, w = batch
        with tf.GradientTape() as tape:
            logits = forward(x, training=True)
            loss_vec = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=y, logits=logits)
            loss = tf.reduce_sum(loss_vec * w) * (1.0 / float(global_batch))
        grads = tape.gradient(loss, variables)
        # 数据并行：replica 内先把梯度 all-reduce 成全局平均，再各自更新（各卡变量一致）
        ctx = tf.distribute.get_replica_context()
        if ctx is not None and num_replicas > 1:
            grads = [ctx.all_reduce(tf.distribute.ReduceOp.SUM, g) / num_replicas for g in grads]
        apply_adam(grads)
        return loss

    started = time.time()
    losses: list[float] = []
    for epoch in range(epochs):
        it = iter(dist_ds)
        total = 0.0
        for _ in range(steps_per_epoch):
            per_replica = strategy.run(step_fn, args=(next(it),))
            total += float(strategy.reduce(tf.distribute.ReduceOp.SUM, per_replica, axis=None))
        losses.append(total / steps_per_epoch)
        log(f"    epoch {epoch + 1}/{epochs} loss={losses[-1]:.4f}")
    return time.time() - started, losses


def params_to_numpy(params: dict[str, Any]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for k in PARAM_ORDER:
        v = params[k]
        out[k] = np.asarray(v.numpy() if hasattr(v, "numpy") else v, dtype=np.float32)
    return out


def predict_cpu(tf, params_np: dict[str, np.ndarray], X: np.ndarray, seq_len: int, units: int) -> np.ndarray:
    """单设备（CPU）推理：TPU 只负责训练，推理走 CPU 避免跨 replica gather。"""
    const_params = {k: tf.constant(v) for k, v in params_np.items()}
    forward = make_forward(tf, const_params, seq_len, units, 0.0)
    with tf.device("/CPU:0"):
        logits = forward(tf.constant(np.asarray(X, dtype=np.float32)), training=False)
        return tf.nn.softmax(logits, axis=1).numpy().astype(np.float32)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    default_out = Path("/kaggle/working/out") if Path("/kaggle").is_dir() else core.TPU_DIR / "out"
    ap = argparse.ArgumentParser(description="TPU GRU 训练（Walk-Forward）")
    ap.add_argument("--data-dir", default=str(core.TRAINING_DIR / "data"))
    ap.add_argument("--out-dir", default=str(default_out))
    ap.add_argument("--seq-len", type=int, default=core.DEFAULT_SEQ_LEN)
    ap.add_argument("--units", type=int, default=48)
    ap.add_argument("--dense", type=int, default=64)
    ap.add_argument("--dropout", type=float, default=0.10)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch", type=int, default=1024, help="全局 batch（TPU 上须能被 8 核整除）")
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--train-days", type=int, default=250)
    ap.add_argument("--valid-days", type=int, default=40)
    ap.add_argument("--step", type=int, default=60)
    ap.add_argument("--max-folds", type=int, default=6)
    ap.add_argument("--label-cost", type=float, default=0.002)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--baseline-lgbm", action="store_true", help="同折跑 LightGBM 基线并做概率融合")
    ap.add_argument("--smoke", action="store_true", help="1 折 1 epoch，快速验证环境")
    args = ap.parse_args(argv)

    if args.smoke:
        args.max_folds, args.epochs, args.valid_days, args.step = 1, 1, 20, 20

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)

    import tensorflow as tf  # noqa: PLC0415 —— 延迟导入，便于静态检查/--help

    tf.random.set_seed(args.seed)
    np.random.seed(args.seed)
    log(f"TensorFlow {tf.__version__} | 输出目录 {out_dir}")

    strategy, dev_info = detect_device(tf)
    global_batch = args.batch
    if dev_info["accelerator"] != "cpu":
        # 全局 batch 必须能被副本数整除；不足时把 batch 抬到副本数
        global_batch = max(dev_info["num_replicas"], (global_batch // dev_info["num_replicas"]) * dev_info["num_replicas"])
    log(f"global_batch={global_batch} | 副本数={dev_info['num_replicas']}")

    # --- 数据 ---
    t0 = time.time()
    daily, zt, index = core.load_frames(args.data_dir)
    log(f"数据：daily={daily.shape} symbols={daily['symbol'].nunique()}")
    df, data_meta = core.build_rows(daily, zt, index, label_cost=args.label_cost)
    X_raw, seq_meta = core.make_sequences(df, seq_len=args.seq_len)
    log(
        f"窗口：X={X_raw.shape} 标签={seq_meta['label'].value_counts().to_dict()} "
        f"（{time.time() - t0:.1f}s）"
    )
    dates = sorted({str(d) for d in seq_meta["trade_date"]})
    splits = core.fold_splits(dates, args.train_days, args.valid_days, args.step, args.max_folds)
    log(f"Walk-Forward：{len(splits)} 折")

    n_feat = int(X_raw.shape[-1])
    oof_probs = np.full((len(seq_meta), 3), np.nan, dtype=np.float32)
    fold_ids = np.full(len(seq_meta), -1, dtype=np.int32)
    fold_logs: list[dict[str, Any]] = []
    last_params_np: dict[str, np.ndarray] | None = None
    last_norm: dict[str, list[float]] | None = None

    for split in splits:
        fid = split["fold"]
        tr_mask = seq_meta["trade_date"].isin(split["train_dates"]).to_numpy()
        va_mask = seq_meta["trade_date"].isin(split["valid_dates"]).to_numpy()
        Xtr_raw, Xva_raw = X_raw[tr_mask], X_raw[va_mask]
        meta_tr, meta_va = seq_meta[tr_mask], seq_meta[va_mask]
        ytr = meta_tr["label_idx"].to_numpy(dtype=np.int64)
        norm = core.fit_norm(Xtr_raw)
        Xtr = core.apply_norm(Xtr_raw, norm)
        Xva = core.apply_norm(Xva_raw, norm)
        wtr = core.class_weights(ytr)[ytr]

        log(
            f"[fold {fid}] train={len(Xtr)} valid={len(Xva)} "
            f"训练窗 {split['train_dates'][0]}~{split['train_dates'][-1]} "
            f"验证窗 {split['valid_dates'][0]}~{split['valid_dates'][-1]}"
        )
        params = init_params(tf, strategy.scope(), n_feat, args.units, args.dense, seed=args.seed + fid)
        forward = make_forward(tf, params, args.seq_len, args.units, args.dropout)
        train_secs, losses = train_fold(
            tf, strategy, forward, params, Xtr, ytr, wtr, args.epochs, global_batch, args.lr, args.seed + fid
        )

        last_params_np = params_to_numpy(params)
        last_norm = norm
        proba = predict_cpu(tf, last_params_np, Xva, args.seq_len, args.units)
        oof_probs[va_mask] = proba
        fold_ids[va_mask] = fid

        fold_metric = core.evaluate_oof(meta_va, proba, args.label_cost)
        fold_logs.append(
            {
                "fold": fid,
                "train_range": [split["train_dates"][0], split["train_dates"][-1]],
                "valid_range": [split["valid_dates"][0], split["valid_dates"][-1]],
                "n_train": int(len(Xtr)),
                "n_valid": int(len(Xva)),
                "train_secs": round(train_secs, 1),
                "train_loss_last": round(losses[-1], 4),
                "accuracy": fold_metric.get("accuracy"),
                "win_rate": fold_metric.get("win_rate"),
                "logloss": fold_metric.get("logloss"),
            }
        )
        log(
            f"[fold {fid}] acc={fmt(fold_metric.get('accuracy'))} "
            f"win_rate={fmt(fold_metric.get('win_rate'))} "
            f"logloss={fmt(fold_metric.get('logloss'))} ({train_secs:.1f}s)"
        )
        del params, forward

    assert last_params_np is not None and last_norm is not None

    # --- 汇总 OOF 指标 ---
    valid_mask = ~np.isnan(oof_probs[:, 0])
    meta_valid = seq_meta[valid_mask].reset_index(drop=True)
    probs_valid = oof_probs[valid_mask]
    metrics = core.evaluate_oof(meta_valid, probs_valid, args.label_cost)

    # --- 导出 JSON（最后一折权重当线上模型，指标用全折 OOF） ---
    export = core.export_model(
        last_params_np,
        last_norm,
        {"walk_forward": metrics, "folds": fold_logs, "n_oof": int(valid_mask.sum())},
        args.seq_len,
        args.units,
        args.dense,
        version=started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        extra={
            "trained_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "device": dev_info.get("accelerator"),
            "train_config": {
                "epochs": args.epochs,
                "global_batch": global_batch,
                "lr": args.lr,
                "train_days": args.train_days,
                "valid_days": args.valid_days,
                "step": args.step,
                "label_cost": args.label_cost,
                "universe": int(daily["symbol"].nunique()),
                "n_windows": int(len(X_raw)),
            },
            "label_definition": data_meta.get("label_definition"),
            "notes": "线上模型=最后一折权重 + 该折标准化统计；推理先按 norm 标准化再做 params 前向（GRU→relu→logits）",
        },
    )

    # --- parity check：numpy 参考前向 vs TensorFlow ---
    n_check = int(min(256, len(X_raw)))
    np_probs = core.predict_numpy(export, X_raw[:n_check])
    tf_probs = predict_cpu(tf, last_params_np, core.apply_norm(X_raw[:n_check], last_norm), args.seq_len, args.units)
    parity = float(np.max(np.abs(np_probs - tf_probs)))
    log(f"parity check（导出 JSON vs TensorFlow）：max|Δ|={parity:.2e}")

    # --- 可选：同折 LightGBM 基线 + 融合 ---
    baseline: dict[str, Any] = {}
    if args.baseline_lgbm:
        try:
            baseline = run_lgbm_baseline(df, meta_valid, probs_valid, args)
        except Exception as exc:  # noqa: BLE001
            log(f"LightGBM 基线失败（不影响主结果）：{type(exc).__name__}: {exc}")
            baseline = {"error": f"{type(exc).__name__}: {exc}"}

    # --- 落盘 ---
    model_path = out_dir / "nn_model.json"
    model_path.write_text(json.dumps(export, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    model_mb = model_path.stat().st_size / 1024 / 1024

    core.oof_frame(meta_valid, probs_valid, fold_ids[valid_mask]).to_parquet(
        out_dir / "nn_oof.parquet", index=False
    )
    summary = {
        "device": dev_info,
        "data": data_meta,
        "config": vars(args),
        "folds": fold_logs,
        "walk_forward": metrics,
        "baseline": baseline,
        "parity_max_abs_diff": parity,
        "model_size_mb": round(model_mb, 3),
        "seq_len": args.seq_len,
        "n_features": n_feat,
        "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_secs": round((datetime.now(timezone.utc) - started_at).total_seconds(), 1),
    }
    (out_dir / "nn_metrics.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=float), encoding="utf-8"
    )
    (out_dir / "env.json").write_text(json.dumps(dev_info, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    log("=" * 62)
    log(
        f"OOF acc={fmt(metrics.get('accuracy'))} logloss={fmt(metrics.get('logloss'))} "
        f"win_rate={fmt(metrics.get('win_rate'))} n={metrics.get('n')}"
    )
    if baseline and "blend" in baseline:
        log(
            f"同折 LightGBM acc={fmt(baseline['lgbm']['accuracy'])} → 融合 "
            f"{baseline['blend'].get('weight_nn')} acc={fmt(baseline['blend']['accuracy'])} "
            f"logloss={fmt(baseline['blend']['logloss'])}"
        )
    log(f"模型 {model_path}（{model_mb:.3f} MB）| parity {parity:.2e}")
    log("=" * 62)
    if parity > 1e-4:
        log("⚠ parity 超阈值：导出 JSON 与训练图不一致，先别急着上线")
        return 2
    return 0


def run_lgbm_baseline(df: pd.DataFrame, meta_valid: pd.DataFrame, probs: np.ndarray, args) -> dict[str, Any]:
    """同一批验证日期上跑 LightGBM，再和 NN 概率融合，看有没有增量。"""
    tl = core.load_train_lgbm()
    log("LightGBM 基线：同折训练中…")
    oof, _folds = tl.walk_forward(
        df,
        core.feature_columns(),
        tl.default_params(),
        train_days=args.train_days,
        valid_days=args.valid_days,
        step=args.step,
        label_cost=args.label_cost,
    )
    keep = meta_valid.copy()
    keep["_row"] = np.arange(len(keep))
    merged = keep.merge(
        oof[["symbol", "trade_date", "prob_up", "prob_flat", "prob_down"]],
        on=["symbol", "trade_date"],
        how="inner",
    )
    if len(merged) == 0:
        return {"error": "同日期交集为空"}
    p_nn = probs[merged["_row"].to_numpy()]
    p_lgbm = merged[["prob_up", "prob_flat", "prob_down"]].to_numpy(dtype=np.float32)
    result = {
        "n": int(len(merged)),
        "nn": core.evaluate_oof(merged, p_nn, args.label_cost),
        "lgbm": core.evaluate_oof(merged, p_lgbm, args.label_cost),
    }
    best: dict[str, Any] = {"accuracy": -1.0}
    for w in (0.3, 0.4, 0.5, 0.6, 0.7):
        m = core.evaluate_oof(merged, core.blend_probs(p_nn, p_lgbm, w), args.label_cost)
        m["weight_nn"] = w
        if m["accuracy"] > best["accuracy"]:
            best = m
    result["blend"] = best
    result["note"] = "同一批验证日期上对齐后比较；blend 权重在 0.3~0.7 内按准确率挑（有过拟合风险，仅参考）"
    return result


if __name__ == "__main__":
    raise SystemExit(main())
