"""指数方向模型：特征、训练、导出 JSON、walk-forward 回测。

与个股模型（§5 口径）同构：softmax 回归 + JSON 权重，可被 Worker/前端消费。
指数日线只有 OHLCV（无成交额/换手），因此特征为指数专用版本；标签为 T+1 收盘
收益三分类（涨/跌/平，阈值默认 0.3%，指数波动小于个股）。

用法：
  python -m quant.cli index-train     # 训练并导出 data/models/index_model.json
  python -m quant.cli index-backtest  # walk-forward 回测，输出 data/reports/index_backtest.*
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quant.config import PROJECT_ROOT, cfg_path
from quant.data.index_data import MAJOR_INDEX_SYMBOLS, load_index_daily

TZ = timezone(timedelta(hours=8))
MODEL_VERSION = "idx-v0.1.0"
LABEL_THRESHOLD = 0.003  # 指数 T+1 收益阈值（0.3%）
HORIZON_DAYS = 1
MODEL_FILE = PROJECT_ROOT / "data" / "models" / "index_model.json"
REPORT_DIR = PROJECT_ROOT / "data" / "reports"

FEATURE_COLUMNS: list[str] = [
    "ret_1d", "ret_3d", "ret_5d", "ret_10d", "ret_20d",
    "ma_dev_5", "ma_dev_20", "ma_dev_60",
    "rsi_6", "rsi_14",
    "macd_dif", "macd_dea", "macd_hist",
    "vol_5", "vol_20",
    "volume_ratio_5", "volume_ratio_20",
    "close_position_20",
    "up_ratio_5",
    "gap_1d",
]


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def build_features(daily: pd.DataFrame, label_threshold: float = LABEL_THRESHOLD) -> pd.DataFrame:
    """指数特征 + 标签（T+1 收益三分类）。只使用当日及历史数据，无未来函数。"""
    df = daily.copy().sort_values("date").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    close = df["close"]
    volume = df["volume"]

    df["ret_1d"] = close.pct_change(1)
    df["ret_3d"] = close.pct_change(3)
    df["ret_5d"] = close.pct_change(5)
    df["ret_10d"] = close.pct_change(10)
    df["ret_20d"] = close.pct_change(20)

    for window in (5, 20, 60):
        ma = close.rolling(window).mean()
        df[f"ma_dev_{window}"] = (close - ma) / ma

    df["rsi_6"] = _rsi(close, 6) / 100.0
    df["rsi_14"] = _rsi(close, 14) / 100.0

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    df["macd_dif"] = dif / close
    df["macd_dea"] = dea / close
    df["macd_hist"] = (dif - dea) / close

    ret1 = df["ret_1d"]
    df["vol_5"] = ret1.rolling(5).std()
    df["vol_20"] = ret1.rolling(20).std()
    df["volume_ratio_5"] = volume / volume.rolling(5).mean()
    df["volume_ratio_20"] = volume / volume.rolling(20).mean()

    low20 = df["low"].rolling(20).min()
    high20 = df["high"].rolling(20).max()
    df["close_position_20"] = (close - low20) / (high20 - low20).replace(0, np.nan)
    df["up_ratio_5"] = (ret1 > 0).astype(float).rolling(5).mean()
    df["gap_1d"] = df["open"] / close.shift(1) - 1.0

    future_ret = close.shift(-HORIZON_DAYS) / close - 1.0

    def label(value: float) -> str | None:
        if pd.isna(value):
            return None
        if value > label_threshold:
            return "up"
        if value < -label_threshold:
            return "down"
        return "flat"

    df["label"] = future_ret.map(label)
    df["future_ret"] = future_ret
    df = df.dropna(subset=FEATURE_COLUMNS + ["label"])
    return df.reset_index(drop=True)


def _merge_indices(symbols: list[str] | None, refresh: bool, frames: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    """合并多个指数的特征样本（指数名称作为附加列，不进入特征）。"""
    frames = frames if frames is not None else {s: load_index_daily(s, refresh=refresh) for s in (symbols or MAJOR_INDEX_SYMBOLS)}
    parts: list[pd.DataFrame] = []
    for symbol, raw in frames.items():
        feat = build_features(raw)
        if len(feat) == 0:
            continue
        feat = feat.assign(symbol=symbol)
        parts.append(feat)
    if not parts:
        raise RuntimeError("没有可用的指数样本（数据为空）")
    out = pd.concat(parts, ignore_index=True)
    out["trade_date"] = out["date"]
    return out.sort_values(["trade_date", "symbol"]).reset_index(drop=True)


def _design_matrix(samples: pd.DataFrame, features: list[str], mean: np.ndarray | None = None,
                    scale: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = samples[features].to_numpy(dtype=float)
    if mean is None or scale is None:
        mean = x.mean(axis=0)
        scale = x.std(axis=0, ddof=0)
        scale = np.where(scale == 0, 1.0, scale)
    z = (x - mean) / scale
    return z, mean, scale


def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def train(
    refresh: bool = True,
    symbols: list[str] | None = None,
    label_threshold: float = LABEL_THRESHOLD,
    train_years: int = 6,
) -> dict[str, Any]:
    """训练指数方向模型（softmax 回归）并导出 JSON。"""
    from sklearn.linear_model import LogisticRegression

    samples = _merge_indices(symbols, refresh)
    samples["label"] = samples["label"].astype(str)

    cutoff = pd.to_datetime(samples["trade_date"]).max()
    start = cutoff - pd.Timedelta(days=365 * train_years)
    train_mask = pd.to_datetime(samples["trade_date"]) >= start
    train_set = samples[train_mask]
    if len(train_set) < 500 or train_set["label"].nunique() < 2:
        raise RuntimeError(f"训练样本不足：{len(train_set)}")

    x = train_set[FEATURE_COLUMNS].to_numpy(dtype=float)
    y = train_set["label"].to_numpy()
    mean = x.mean(axis=0)
    scale = np.where(x.std(axis=0, ddof=0) == 0, 1.0, x.std(axis=0, ddof=0))
    z = (x - mean) / scale

    clf = LogisticRegression(max_iter=3000, C=1.0, random_state=42)
    clf.fit(z, y)
    classes = [str(c) for c in clf.classes_]
    prob = clf.predict_proba(z)
    pred = np.array(classes)[prob.argmax(axis=1)]
    accuracy = float((pred == y).mean())
    logloss = float(-np.mean(np.log(np.clip(prob[np.arange(len(y)), np.searchsorted(classes, y)], 1e-9, 1.0))))

    model = {
        "schema": 1,
        "version": MODEL_VERSION,
        "kind": "softmax_regression",
        "scope": "index",
        "classes": classes,
        "features": FEATURE_COLUMNS,
        "mean": [float(v) for v in mean],
        "scale": [float(v) for v in scale],
        "coef": [[float(v) for v in row] for row in clf.coef_],
        "intercept": [float(v) for v in clf.intercept_],
        "trained_at": _now(),
        "train_range": [str(start.date()), str(cutoff.date())],
        "horizon": f"{HORIZON_DAYS}d",
        "label_rule": f"r = close[t+1]/close[t]-1; r>{label_threshold:.2%} -> up, r<-{label_threshold:.2%} -> down, else flat",
        "metrics": {"accuracy": round(accuracy, 4), "logloss": round(logloss, 4), "n": int(len(y))},
        "indices": sorted(samples["symbol"].unique().tolist()),
    }
    MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODEL_FILE.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"model_path": str(MODEL_FILE), "metrics": model["metrics"], "classes": classes,
            "train_range": model["train_range"], "indices": model["indices"]}


def load_model(path: Path | None = None) -> dict[str, Any]:
    data = json.loads(Path(path or MODEL_FILE).read_text(encoding="utf-8"))
    return data


def predict_proba(model: dict[str, Any], feature_values: dict[str, float]) -> dict[str, float]:
    """与导出 JSON 公式严格一致的推理（供 Python 与测试使用）。"""
    features = model["features"]
    x = np.array([float(feature_values.get(f, 0.0)) for f in features])
    mean = np.array(model["mean"])
    scale = np.array(model["scale"])
    z = (x - mean) / scale
    coef = np.array(model["coef"])
    intercept = np.array(model["intercept"])
    logits = coef @ z + intercept
    logits = logits - logits.max()
    e = np.exp(logits)
    p = e / e.sum()
    classes = model["classes"]
    return {cls: float(v) for cls, v in zip(classes, p)}


def _direction_view(prob: dict[str, float]) -> tuple[str, float]:
    best = max(("up", "flat", "down"), key=lambda k: prob.get(k, 0.0))
    return best, float(prob.get(best, 0.0))


def infer_index_direction(model: dict[str, Any], daily: pd.DataFrame) -> dict[str, Any] | None:
    """用最新一根 K 线推理指数方向；数据不足返回 None。"""
    feat = build_features(daily)
    if len(feat) == 0:
        return None
    row = feat.iloc[-1]
    values = {f: float(row[f]) for f in FEATURE_COLUMNS}
    prob = predict_proba(model, values)
    direction, confidence = _direction_view(prob)
    ma5 = float(daily["close"].astype(float).rolling(5).mean().iloc[-1])
    ma20 = float(daily["close"].astype(float).rolling(20).mean().iloc[-1])
    ma60 = float(daily["close"].astype(float).rolling(60).mean().iloc[-1])
    ma_state = "多头排列" if ma5 > ma20 > ma60 else ("空头排列" if ma5 < ma20 < ma60 else "震荡")
    return {"direction": direction, "prob": {k: round(v, 4) for k, v in prob.items()},
            "confidence": round(confidence, 4), "ma_state": ma_state}


# ---------------------------------------------------------------- 回测 ----


def _trade_returns(labels: np.ndarray, preds: np.ndarray, future_ret: np.ndarray,
                    fee_bps: float = 2.0) -> tuple[np.ndarray, np.ndarray]:
    """交易口径：up 做多、down 做空、flat 空仓；按 T+1 收盘收益计（往返费用 fee_bps*2）。"""
    cost = fee_bps * 2 / 10000.0
    returns = np.zeros(len(preds))
    traded = preds != "flat"
    sign = np.where(preds == "up", 1.0, np.where(preds == "down", -1.0, 0.0))
    returns[traded] = sign[traded] * future_ret[traded] - cost
    return returns[traded], traded


def _wilson_ci(success: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = success / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom))


def _metrics_block(labels: np.ndarray, preds: np.ndarray, prob: np.ndarray, classes: list[str],
                    future_ret: np.ndarray, fee_bps: float = 2.0) -> dict[str, Any]:
    from sklearn.metrics import precision_recall_fscore_support

    classes_arr = np.array(classes)
    idx = {c: i for i, c in enumerate(classes)}
    y = np.array([idx[v] for v in labels])
    p = np.array([idx[v] for v in preds])
    accuracy = float((y == p).mean())
    n = int(len(y))
    # 分方向 P/R/F1
    prf = {}
    for cls in classes:
        tp = int(((y == idx[cls]) & (p == idx[cls])).sum())
        fp = int(((y != idx[cls]) & (p == idx[cls])).sum())
        fn = int(((y == idx[cls]) & (p != idx[cls])).sum())
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        prf[cls] = {"precision": round(precision, 4), "recall": round(recall, 4),
                    "f1": round(f1, 4), "support": int((y == idx[cls]).sum())}
    # 3x3 混淆矩阵
    confusion = [[int(((y == i) & (p == j)).sum()) for j in range(len(classes))] for i in range(len(classes))]
    logloss = float(-np.mean(np.log(np.clip(prob[np.arange(n), y], 1e-9, 1.0))))
    # multiclass Brier
    onehot = np.zeros((n, len(classes)))
    onehot[np.arange(n), y] = 1.0
    brier = float(np.mean(np.sum((prob - onehot) ** 2, axis=1)))

    trade_ret, mask = _trade_returns(labels, preds, future_ret, fee_bps)
    win = int((trade_ret > 0).sum())
    trades = int(mask.sum())
    win_rate = win / trades if trades else 0.0
    lo, hi = _wilson_ci(win, trades)
    equity = np.cumprod(1 + trade_ret) if trades else np.array([1.0])
    total_return = float(equity[-1] - 1.0)
    drawdown = float((equity / np.maximum.accumulate(equity) - 1).min()) if trades else 0.0
    return {
        "n": n,
        "accuracy": round(accuracy, 4),
        "logloss": round(logloss, 4),
        "brier": round(brier, 4),
        "by_direction": prf,
        "confusion": confusion,
        "classes": classes,
        "win_rate": round(win_rate, 4),
        "win_rate_ci95": [round(lo, 4), round(hi, 4)],
        "trades": trades,
        "wins": win,
        "losses": trades - win,
        "avg_return_per_trade": round(float(trade_ret.mean()), 6) if trades else 0.0,
        "total_return": round(total_return, 4),
        "max_drawdown": round(drawdown, 4),
        "sharpe": round(float(trade_ret.mean() / trade_ret.std(ddof=0) * np.sqrt(252)) if trades and trade_ret.std(ddof=0) > 0 else 0.0, 3),
    }


def run_backtest(
    start: str | None = None,
    end: str | None = None,
    refresh: bool = False,
    symbols: list[str] | None = None,
    label_threshold: float = LABEL_THRESHOLD,
    min_train_days: int = 750,
    fee_bps: float = 2.0,
) -> dict[str, Any]:
    """walk-forward 回测：测试窗=自然季度，训练窗=测试窗前全部（至少 min_train_days 样本）。"""
    from sklearn.linear_model import LogisticRegression

    samples = _merge_indices(symbols, refresh)
    dates = pd.to_datetime(samples["trade_date"])
    start = start or "2021-01-01"
    end = end or str(dates.max().date())
    test_mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    samples = samples[test_mask].copy()
    if len(samples) == 0:
        raise RuntimeError("测试区间无样本")

    samples["quarter"] = dates[test_mask].dt.to_period("Q").astype(str)
    all_labels: list[str] = []
    all_preds: list[str] = []
    all_probs: list[np.ndarray] = []
    all_future: list[float] = []
    classes_ref = ["down", "flat", "up"]
    fold_classes: list[list[str]] = []
    all_ma5: list[float] = []
    folds: list[dict[str, Any]] = []

    for quarter in sorted(samples["quarter"].unique()):
        test_set = samples[samples["quarter"] == quarter]
        train_set = samples[(pd.to_datetime(samples["trade_date"]) < pd.to_datetime(test_set["trade_date"]).min())]
        train_set = train_set.tail(4000)
        if len(train_set) < min_train_days or train_set["label"].nunique() < 2:
            folds.append({"quarter": quarter, "skipped": True, "reason": f"train_n={len(train_set)}"})
            continue
        x_tr = train_set[FEATURE_COLUMNS].to_numpy(dtype=float)
        y_tr = train_set["label"].to_numpy()
        mean = x_tr.mean(axis=0)
        scale = np.where(x_tr.std(axis=0, ddof=0) == 0, 1.0, x_tr.std(axis=0, ddof=0))
        clf = LogisticRegression(max_iter=3000, C=1.0, random_state=42)
        clf.fit((x_tr - mean) / scale, y_tr)
        classes = [str(c) for c in clf.classes_]

        x_te = test_set[FEATURE_COLUMNS].to_numpy(dtype=float)
        prob = clf.predict_proba((x_te - mean) / scale)
        preds = np.array(classes)[prob.argmax(axis=1)]
        all_labels.extend(test_set["label"].tolist())
        all_preds.extend(preds.tolist())
        all_future.extend(test_set["future_ret"].astype(float).tolist())
        all_ma5.extend(test_set["ma_dev_5"].astype(float).tolist())
        all_probs.append(prob)
        fold_classes.append(classes)
        folds.append({"quarter": quarter, "train_n": int(len(train_set)), "test_n": int(len(test_set)),
                      "accuracy": round(float((preds == test_set["label"].to_numpy()).mean()), 4)})

    if not all_preds:
        raise RuntimeError("回测无有效折")

    y = np.array(all_labels)
    p = np.array(all_preds)
    prob_all = np.vstack(all_probs)
    future_ret = np.array(all_future)

    # 按统一 classes 顺序重排概率列（用类名映射，不对折内列顺序做假设）
    prob_unified = np.zeros((len(p), len(classes_ref)))
    pos = 0
    for prob, cls in zip(all_probs, fold_classes):
        col = [cls.index(c) for c in classes_ref]
        prob_unified[pos:pos + len(prob)] = prob[:, col]
        pos += len(prob)

    metrics = _metrics_block(y, p, prob_unified, classes_ref, future_ret, fee_bps=fee_bps)

    # 基准
    def baseline_pred(name: str) -> np.ndarray:
        if name == "always_flat":
            return np.array(["flat"] * len(y))
        if name == "majority":
            values, counts = np.unique(y, return_counts=True)
            return np.array([values[counts.argmax()]] * len(y))
        if name == "random":
            rng = np.random.default_rng(42)
            return rng.choice(["flat", "up", "down"], size=len(y))
        if name == "ma5_cross":
            # 无未来函数：当日收盘价在 MA5 之上看多、之下看空
            return np.where(np.array(all_ma5) > 0, "up", "down")
        raise ValueError(name)

    baselines: dict[str, Any] = {}
    for name in ("always_flat", "majority", "random", "ma5_cross"):
        bp = baseline_pred(name)
        baselines[name] = _metrics_block(y, bp, np.ones((len(y), 3)) / 3, classes_ref, future_ret, fee_bps=fee_bps)

    try:
        threshold_experiment = evaluate_thresholds(start=start, end=end, symbols=symbols, fee_bps=fee_bps)
    except Exception as exc:  # noqa: BLE001
        threshold_experiment = {"error": f"{type(exc).__name__}: {exc}"}

    report = {
        "schema": 1,
        "model": MODEL_VERSION,
        "scope": "index",
        "interval": {"start": start, "end": end},
        "n": len(y),
        "label_threshold": label_threshold,
        "fee_bps": fee_bps,
        "folds": folds,
        "metrics": metrics,
        "baselines": baselines,
        "threshold_experiment": threshold_experiment,
        "created_at": _now(),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "index_backtest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORT_DIR / "index_backtest.md").write_text(_render_md(report), encoding="utf-8")
    return report


def _render_md(report: dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        "# 指数方向模型 · walk-forward 回测",
        "",
        f"- 区间：{report['interval']['start']} ~ {report['interval']['end']}，样本 n={report['n']}",
        f"- 标签：T+1 收盘收益三分类（阈值 ±{report['label_threshold']:.2%}）",
        f"- 费用：往返 {report['fee_bps'] * 2:.0f} bps（交易口径必扣）",
        f"- 模型：softmax 回归（{MODEL_VERSION}）",
        "",
        "## 方向指标",
        f"- accuracy：{m['accuracy']:.2%}（n={m['n']}）",
        f"- logloss：{m['logloss']:.4f}，Brier：{m['brier']:.4f}",
        "",
        "| 方向 | precision | recall | F1 | support |",
        "|---|---|---|---|---|",
    ]
    for cls, row in m["by_direction"].items():
        lines.append(f"| {cls} | {row['precision']:.2%} | {row['recall']:.2%} | {row['f1']:.2%} | {row['support']} |")
    lines += [
        "",
        "## 交易口径（多空双向）",
        f"- 胜率：{m['win_rate']:.2%}（Wilson 95% CI {m['win_rate_ci95'][0]:.2%} ~ {m['win_rate_ci95'][1]:.2%}）",
        f"- 交易笔数：{m['trades']}（胜 {m['wins']} / 负 {m['losses']}）",
        f"- 单笔平均收益：{m['avg_return_per_trade']:.4%}",
        f"- 总收益：{m['total_return']:.2%}，最大回撤：{m['max_drawdown']:.2%}，Sharpe：{m['sharpe']}",
        "",
        "## 基准对比",
        "| 基准 | accuracy | 胜率 | 总收益 |",
        "|---|---|---|---|",
    ]
    for name, b in report["baselines"].items():
        lines.append(f"| {name} | {b['accuracy']:.2%} | {b['win_rate']:.2%} | {b['total_return']:.2%} |")
    te = report.get("threshold_experiment") or {}
    if te.get("thresholds"):
        lines += ["", "## 置信度门槛实验（少交易是否更稳）", "",
                  "| 门槛 | 笔数 | 胜率 | 单笔平均 | 总收益 | Sharpe |", "|---|---|---|---|---|---|"]
        for th, value in te["thresholds"].items():
            if value.get("trades"):
                lines.append(
                    f"| {th} | {value['trades']} | {value['win_rate']:.2%} | {value['avg_return']:.4%} | "
                    f"{value['total_return']:.2%} | {value['sharpe']} |"
                )
    lines += ["", "> 历史回测不代表未来；本项目仅供研究，不构成投资建议。", ""]
    return "\n".join(lines)


def evaluate_thresholds(
    start: str | None = None,
    end: str | None = None,
    thresholds: tuple[float, ...] = (0.35, 0.4, 0.45, 0.5, 0.55),
    refresh: bool = False,
    symbols: list[str] | None = None,
    min_train_days: int = 750,
    fee_bps: float = 2.0,
) -> dict[str, Any]:
    """不同置信度门槛下的多空交易表现（含往返费用），用于评估「少交易是否更稳」。"""
    from sklearn.linear_model import LogisticRegression

    samples = _merge_indices(symbols, refresh)
    dates = pd.to_datetime(samples["trade_date"])
    start = start or "2021-01-01"
    end = end or str(dates.max().date())
    mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    samples = samples[mask].copy()
    samples["quarter"] = dates[mask].dt.to_period("Q").astype(str)
    labels: list[str] = []
    preds: list[str] = []
    probs: list[np.ndarray] = []
    future: list[float] = []
    for quarter in sorted(samples["quarter"].unique()):
        test_set = samples[samples["quarter"] == quarter]
        train_set = samples[pd.to_datetime(samples["trade_date"]) < pd.to_datetime(test_set["trade_date"]).min()].tail(4000)
        if len(train_set) < min_train_days or train_set["label"].nunique() < 2:
            continue
        x_tr = train_set[FEATURE_COLUMNS].to_numpy(dtype=float)
        y_tr = train_set["label"].to_numpy()
        mean = x_tr.mean(axis=0)
        scale = np.where(x_tr.std(axis=0, ddof=0) == 0, 1.0, x_tr.std(axis=0, ddof=0))
        clf = LogisticRegression(max_iter=3000, C=1.0, random_state=42)
        clf.fit((x_tr - mean) / scale, y_tr)
        classes = [str(c) for c in clf.classes_]
        x_te = test_set[FEATURE_COLUMNS].to_numpy(dtype=float)
        prob = clf.predict_proba((x_te - mean) / scale)
        col = [classes.index(c) for c in ["down", "flat", "up"]]
        prob = prob[:, col]
        pred = np.array(["down", "flat", "up"])[prob.argmax(axis=1)]
        labels.extend(test_set["label"].tolist())
        preds.extend(pred.tolist())
        probs.append(prob)
        future.extend(test_set["future_ret"].astype(float).tolist())
    if not probs:
        raise RuntimeError("无有效折")

    prob_all = np.vstack(probs)
    confidence = prob_all.max(axis=1)
    preds_arr = np.array(preds)
    future_arr = np.array(future)
    cost = fee_bps * 2 / 10000.0
    results: dict[str, Any] = {}
    for threshold in thresholds:
        tradable = (preds_arr != "flat") & (confidence >= threshold)
        trades = int(tradable.sum())
        if trades == 0:
            results[f"{threshold:.2f}"] = {"trades": 0}
            continue
        sign = np.where(preds_arr[tradable] == "up", 1.0, -1.0)
        ret = sign * future_arr[tradable] - cost
        win = int((ret > 0).sum())
        equity = np.cumprod(1 + ret)
        results[f"{threshold:.2f}"] = {
            "trades": trades,
            "win_rate": round(win / trades, 4),
            "avg_return": round(float(ret.mean()), 6),
            "total_return": round(float(equity[-1] - 1), 4),
            "max_drawdown": round(float((equity / np.maximum.accumulate(equity) - 1).min()), 4),
            "sharpe": round(float(ret.mean() / ret.std(ddof=0) * np.sqrt(252)) if ret.std(ddof=0) > 0 else 0.0, 3),
        }
    return {
        "model": MODEL_VERSION,
        "interval": {"start": start, "end": end},
        "fee_bps": fee_bps,
        "n_samples": int(len(preds_arr)),
        "thresholds": results,
    }
