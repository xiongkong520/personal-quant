"""Softmax 回归训练与 ``model.json`` 导出（契约 §5 / §9）。

- 时间切分：用最近 ``train_years`` 年数据，最后约 1 个季度做 holdout 评估；
- 导出权重用整个窗口（含 holdout）重训，``metrics`` 记录 holdout 指标
  （字段 ``basis=holdout_last_quarter`` 标明口径）；
- 固定 ``random_seed``（默认 42）+ 确定性求解器，同数据重训结果逐位一致；
- 只读本地 SQLite，不联网；不做实盘。
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from quant.features.builder import (
    FEATURE_COLUMNS,
    LABEL_CLASSES,
    build_features,
    feature_matrix,
)
from quant.model._data_io import load_daily_multi, resolve_model_path

MODEL_KIND = "softmax_regression"
DEFAULT_VERSION = "v0.1.0"
TZ = timezone(timedelta(hours=8))

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)


def label_rule_text(horizon_days: int, label_threshold: float) -> str:
    pct_up = f"{label_threshold * 100:g}%"
    return (
        f"r = adj_close[t+{horizon_days}]/adj_close[t]-1; "
        f"r>{pct_up} -> up, r<-{pct_up} -> down, else flat"
    )


def evaluate_model(model: dict, X: np.ndarray, y: np.ndarray) -> dict:
    """用与线上推理完全相同的 ``predict_proba`` 计算 holdout 指标。"""
    from quant.model.predict import predict_proba

    features = model["features"]
    probs: list[dict[str, float]] = []
    preds: list[str] = []
    for row in X:
        x = {name: float(v) for name, v in zip(features, row)}
        p = predict_proba(model, x)
        probs.append(p)
        preds.append(max(p, key=p.get))
    y = np.asarray(y).astype(str)
    preds_arr = np.asarray(preds)
    labels = list(model["classes"])
    accuracy = float((preds_arr == y).mean()) if len(y) else 0.0
    eps = 1e-12
    logloss = float(
        -np.mean([np.log(max(p[label], eps)) for p, label in zip(probs, y)])
    )
    brier = float(
        np.mean(
            [
                sum((p[c] - (1.0 if c == label else 0.0)) ** 2 for c in labels)
                for p, label in zip(probs, y)
            ]
        )
    )
    confusion = {
        c_true: {c_pred: int(((y == c_true) & (preds_arr == c_pred)).sum()) for c_pred in labels}
        for c_true in labels
    }
    return {
        "accuracy": accuracy,
        "logloss": logloss,
        "brier": brier,
        "n": int(len(y)),
        "confusion": confusion,
    }


def fit_softmax_model(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: list[str] | None = None,
    seed: int = 42,
) -> dict:
    """拟合标准化 + 多项逻辑回归，返回可导出的权重字典（§5 顺序）。"""
    features = list(feature_names or FEATURE_COLUMNS)
    X = np.asarray(X, dtype=float)
    y = np.asarray(y).astype(str)
    if X.ndim != 2 or X.shape[1] != len(features):
        raise ValueError(f"特征矩阵形状 {X.shape} 与特征数 {len(features)} 不一致")
    if len(X) == 0:
        raise ValueError("训练集为空")
    present = set(np.unique(y))
    missing = [c for c in LABEL_CLASSES if c not in present]
    if missing:
        raise RuntimeError(
            f"训练集缺少类别 {missing}（现有 {sorted(present)}）：样本量或阈值设置不足"
        )
    if not np.isfinite(X).all():
        raise ValueError("特征矩阵含 NaN/Inf，请先 dropna")

    _seed_everything(seed)
    scaler = StandardScaler().fit(X)
    Xs = scaler.transform(X)
    clf = LogisticRegression(
        C=1.0,
        solver="lbfgs",
        max_iter=2000,
        tol=1e-6,
        random_state=seed,
    ).fit(Xs, y)

    raw_classes = [str(c) for c in clf.classes_]
    order = [raw_classes.index(c) for c in LABEL_CLASSES]
    coef = np.asarray(clf.coef_, dtype=float)
    intercept = np.asarray(clf.intercept_, dtype=float)
    if coef.shape[0] != len(LABEL_CLASSES):  # 二分类兜底（正常不会走到）
        raise RuntimeError(f"模型类别数异常: {raw_classes}")
    coef = coef[order]
    intercept = intercept[order]
    scale = np.asarray(scaler.scale_, dtype=float)
    scale = np.where(scale == 0.0, 1.0, scale)
    return {
        "schema": 1,
        "version": DEFAULT_VERSION,
        "kind": MODEL_KIND,
        "classes": list(LABEL_CLASSES),
        "features": features,
        "mean": np.asarray(scaler.mean_, dtype=float).tolist(),
        "scale": scale.tolist(),
        "coef": coef.tolist(),
        "intercept": intercept.tolist(),
        "trained_at": now_iso(),
        "train_range": ["", ""],
        "horizon": "1d",
        "label_rule": label_rule_text(1, 0.005),
        "metrics": {},
    }


def _prepare_training_frame(
    symbols: list[str], cfg: dict
) -> tuple[pd.DataFrame, int, int, float]:
    """读库 → 特征 → 丢弃 NaN；返回 (features, horizon_days, seed, threshold)。"""
    model_cfg = cfg.get("model") or {}
    horizon_days = int(model_cfg.get("horizon_days", 1))
    label_threshold = float(model_cfg.get("label_threshold", 0.005))
    seed = int(model_cfg.get("random_seed", 42))
    data_cfg = cfg.get("data") or {}
    start = data_cfg.get("daily_start")
    daily = load_daily_multi(list(symbols), cfg, start=start)
    if len(daily) == 0:
        raise RuntimeError(f"行情库中没有 {symbols} 的日线数据，请先 fetch")
    available = sorted(str(x) for x in daily["symbol"].unique())
    feats = build_features(daily, horizon_days, label_threshold)
    feats = feats.dropna(subset=[*FEATURE_COLUMNS, "label"]).reset_index(drop=True)
    if len(feats) == 0:
        raise RuntimeError("特征全为空（数据长度可能不足 60 个交易日）")
    return feats, horizon_days, seed, label_threshold, available


def _time_split(
    feats: pd.DataFrame, train_years: float
) -> tuple[pd.DataFrame, pd.DataFrame, str, str]:
    """按时间切分 holdout 评估集（最后约 1 个季度）。"""
    end_date = str(feats["trade_date"].max())
    start_ts = pd.Timestamp(end_date) - pd.DateOffset(years=max(int(train_years), 1))
    window = feats[feats["trade_date"] >= start_ts.strftime("%Y-%m-%d")]
    dates = sorted(window["trade_date"].unique())
    if len(dates) < 120:
        raise RuntimeError(f"训练窗口仅 {len(dates)} 个交易日，不足 120（至少约半年）")
    holdout_n = max(40, int(round(len(dates) * 0.15)))
    holdout_n = min(holdout_n, max(1, len(dates) // 3))
    holdout_start = dates[-holdout_n]
    train = window[window["trade_date"] < holdout_start].reset_index(drop=True)
    test = window[window["trade_date"] >= holdout_start].reset_index(drop=True)
    return train, test, str(window["trade_date"].min()), str(holdout_start)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def train(symbols: list[str], cfg: dict) -> dict:
    """训练并写 ``data/models/model.json``，返回 metrics（契约 §9）。"""
    model_cfg = cfg.get("model") or {}
    random_seed = int(model_cfg.get("random_seed", 42))
    train_years = float(model_cfg.get("train_years", 2))
    version = str(model_cfg.get("version") or DEFAULT_VERSION)

    _seed_everything(random_seed)
    feats, horizon_days, seed, label_threshold, available_symbols = _prepare_training_frame(
        symbols, cfg
    )

    train_df, test_df, window_start, holdout_start = _time_split(feats, train_years)
    if len(train_df) < 300:
        raise RuntimeError(
            f"训练样本仅 {len(train_df)} 条（<300），无法可靠训练；请扩大数据区间"
        )
    if len(test_df) < 30:
        raise RuntimeError(f"holdout 样本仅 {len(test_df)} 条（<30），请扩大数据区间")

    X_train, names = feature_matrix(train_df, FEATURE_COLUMNS)
    y_train = train_df["label"].to_numpy()
    eval_model = fit_softmax_model(X_train, y_train, names, seed=seed)
    X_test, _ = feature_matrix(test_df, names)
    y_test = test_df["label"].to_numpy()
    holdout_metrics = evaluate_model(eval_model, X_test, y_test)

    # 线上模型：整个训练窗口（含 holdout）重训，指标仍用 holdout 口径
    X_all, names = feature_matrix(feats[feats["trade_date"] >= window_start], names)
    y_all = feats[feats["trade_date"] >= window_start]["label"].to_numpy()
    model = fit_softmax_model(X_all, y_all, names, seed=seed)

    train_dates = sorted(feats[feats["trade_date"] >= window_start]["trade_date"].unique())
    metrics = {
        "accuracy": holdout_metrics["accuracy"],
        "logloss": holdout_metrics["logloss"],
        "brier": holdout_metrics["brier"],
        "n": holdout_metrics["n"],
        "confusion": holdout_metrics["confusion"],
        "basis": "holdout_last_quarter",
        "holdout_range": [holdout_start, str(test_df["trade_date"].max())],
        "train_range": [window_start, str(train_df["trade_date"].max())],
        "n_train": int(len(y_train)),
        "n_export_rows": int(len(y_all)),
        "random_seed": seed,
        "horizon_days": horizon_days,
        "label_threshold": label_threshold,
        "symbols": available_symbols,
    }

    model.update(
        {
            "version": version,
            "trained_at": now_iso(),
            "train_range": [window_start, str(feats["trade_date"].max())],
            "horizon": f"{horizon_days}d",
            "label_rule": label_rule_text(horizon_days, label_threshold),
            "metrics": metrics,
        }
    )
    out_path = resolve_model_path(cfg)
    _atomic_write_json(out_path, model)

    return {
        **{k: metrics[k] for k in ("accuracy", "logloss", "brier", "n")},
        "confusion": metrics["confusion"],
        "basis": metrics["basis"],
        "holdout_range": metrics["holdout_range"],
        "train_range": metrics["train_range"],
        "n_train": metrics["n_train"],
        "n_export_rows": metrics["n_export_rows"],
        "random_seed": seed,
        "model_version": version,
        "features": len(names),
        "symbols": available_symbols,
        "model_path": str(out_path),
        "trained_at": model["trained_at"],
        "train_days": int(len(train_dates)),
    }
