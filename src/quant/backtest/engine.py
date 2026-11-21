"""历史回测引擎（契约 §12；训练/回测均不联网、不做实盘）。

核心口径
--------
- **walk-forward**：测试窗 = 自然季度；训练窗 = 测试窗开始前的**全部历史**（≥1 年）；
  取最近 ``walk_forward_splits`` 个季度（不足则全部），严禁用测试期数据训练/调参。
- **方向判断指标**（预测质量）：三分类 accuracy / 分方向 precision·recall·F1 /
  logloss / multiclass Brier / 3×3 混淆矩阵 / 置信度校准分桶 / 分年度·分季度准确率。
- **胜率（交易口径，与方向准确率分开）**：T 日收盘出信号 → T+1 开盘 ± 滑点成交
  （T+1 制度下最早可卖时点为 T+2 开盘）→ 持有一个 ``horizon`` 后开盘平仓；
  涨跌停/停牌按契约 §6 拒单或顺延；费用优先复用 ``quant.paper.rules.fees``。
  做空仅用于研究模拟（A 股普通账户不可直接做空）。
- **基准并列**：always_flat / majority / ma5_cross / random（固定种子）。
- 输出 ``backtest.md`` + ``backtest.json``，并把 OOS 指标同步回 ``model.json.metrics``。
"""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from quant.features.builder import (
    FEATURE_COLUMNS,
    LABEL_CLASSES,
    build_features,
    feature_matrix,
)
from quant.model._data_io import (
    load_daily_multi,
    resolve_db_path,
    resolve_model_path,
    resolve_report_dir,
)
from quant.model.predict import predict_proba_batch
from quant.model.train import _atomic_write_json, fit_softmax_model, label_rule_text, now_iso

TZ = timezone(timedelta(hours=8))
Z95 = 1.959963984540054
TRADING_DAYS_PER_YEAR = 252
BENCHMARKS = ("always_flat", "majority", "ma5_cross", "random")

#: 契约 §12 要求 ``backtest.json`` 必须包含的字段路径（自检用，缺一项视为未完成）。
REQUIRED_CONTRACT_PATHS: tuple[str, ...] = (
    "meta.eval_range",
    "meta.symbols",
    "meta.n_samples",
    "meta.model_version",
    "meta.random_seed",
    "direction.accuracy",
    "direction.n",
    "direction.coverage",
    "direction.logloss",
    "direction.brier",
    "direction.confusion.matrix",
    "direction.calibration",
    "direction.by_year",
    "direction.by_quarter",
    "direction.per_class.down.precision",
    "direction.per_class.down.recall",
    "direction.per_class.down.f1",
    "direction.per_class.flat.precision",
    "direction.per_class.flat.recall",
    "direction.per_class.flat.f1",
    "direction.per_class.up.precision",
    "direction.per_class.up.recall",
    "direction.per_class.up.f1",
    "win_rate.win_rate",
    "win_rate.win_rate_by_direction.long",
    "win_rate.win_rate_by_direction.short",
    "win_rate.trades.total",
    "win_rate.trades.win",
    "win_rate.trades.loss",
    "win_rate.trades.flat",
    "win_rate.avg_return_per_trade",
    "win_rate.payoff_ratio",
    "win_rate.max_drawdown",
    "win_rate.sharpe",
    "win_rate.total_return",
    "win_rate.wilson95",
    "win_rate.equity_curve.dates",
    "win_rate.equity_curve.equity",
    "benchmarks.always_flat",
    "benchmarks.majority",
    "benchmarks.ma5_cross",
    "benchmarks.random",
    "walk_forward.splits",
)

_CALIBRATION_BUCKETS = (
    ("le_0.40", None, 0.40),
    ("0.40-0.50", 0.40, 0.50),
    ("0.50-0.60", 0.50, 0.60),
    ("gt_0.60", 0.60, None),
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _r(value: float | None, digits: int = 6) -> float | None:
    value = _finite(value)
    return None if value is None else round(value, digits)


def _jsonable(obj):
    """递归转成严格 JSON 可序列化对象（NaN/Inf → None，numpy → 原生）。"""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _jsonable(obj.tolist())
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d")
    return obj


def wilson_interval(
    successes: int, n: int, z: float = Z95
) -> tuple[float | None, float | None]:
    """Wilson 95% 置信区间（二项比例）。``n==0`` 返回 ``(None, None)``。"""
    if n <= 0:
        return (None, None)
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _fees(side: str, amount: float, cfg: dict) -> dict:
    """交易费用：优先契约 §6 共用实现 ``quant.paper.rules.fees``，否则本地兜底。"""
    try:  # pragma: no cover - paper 模块就绪时走这里
        from quant.paper.rules import fees as rules_fees  # type: ignore

        return rules_fees(side, amount, cfg)
    except Exception:
        paper = cfg.get("paper") or {}
        commission = max(
            amount * float(paper.get("commission_rate", 0.00025)),
            float(paper.get("commission_min", 5.0)),
        )
        stamp = amount * float(paper.get("stamp_tax_sell", 0.0005)) if side == "sell" else 0.0
        transfer = amount * float(paper.get("transfer_fee", 0.00001))
        return {
            "commission": commission,
            "stamp_tax": stamp,
            "transfer_fee": transfer,
            "total": commission + stamp + transfer,
        }


def _limit_fallback(pre_close: float, symbol: str) -> tuple[float, float]:
    """涨跌停价兜底（数据列缺失时；四舍五入到分，契约 §6）。"""
    symbol = str(symbol).zfill(6)
    if symbol.startswith(("300", "301", "688")):
        pct = 0.20
    elif symbol.startswith(("8", "4")):
        pct = 0.30
    else:
        pct = 0.10
    round_up = lambda x: math.floor(x * 100 + 0.5) / 100  # noqa: E731
    return round_up(pre_close * (1 + pct)), round_up(pre_close * (1 - pct))


# ---------------------------------------------------------------------------
# §12.1 方向判断指标
# ---------------------------------------------------------------------------
def compute_direction_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    probs: np.ndarray,
    dates: np.ndarray,
    classes: tuple[str, ...] | list[str] = LABEL_CLASSES,
) -> dict:
    """方向判断指标：accuracy / 分方向 P·R·F1 / logloss / Brier / 混淆矩阵 / 校准 / 稳定性。

    ``probs`` 列顺序必须与 ``classes`` 一致。
    """
    labels = list(classes)
    y_true = np.asarray(y_true).astype(str)
    y_pred = np.asarray(y_pred).astype(str)
    probs = np.asarray(probs, dtype=float)
    dates = np.asarray(dates).astype(str)
    n = len(y_true)
    if n != len(y_pred) or n != len(probs):
        raise ValueError("y_true / y_pred / probs 长度不一致")
    if n == 0:
        raise ValueError("方向指标样本为空")
    class_idx = {c: i for i, c in enumerate(labels)}

    accuracy = float((y_true == y_pred).mean())
    acc_lo, acc_hi = wilson_interval(int((y_true == y_pred).sum()), n)

    per_class: dict[str, dict] = {}
    for c in labels:
        tp = int(np.sum((y_pred == c) & (y_true == c)))
        fp = int(np.sum((y_pred == c) & (y_true != c)))
        fn = int(np.sum((y_pred != c) & (y_true == c)))
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        if precision is not None and recall is not None and (precision + recall) > 0:
            f1 = 2 * precision * recall / (precision + recall)
        else:
            f1 = 0.0 if (precision is not None or recall is not None) else None
        per_class[c] = {
            "precision": _r(precision),
            "recall": _r(recall),
            "f1": _r(f1),
            "support": int(np.sum(y_true == c)),
            "predicted": int(np.sum(y_pred == c)),
        }

    eps = 1e-12
    true_probs = np.array(
        [probs[i, class_idx.get(label, 0)] if label in class_idx else eps for i, label in enumerate(y_true)]
    )
    logloss = float(-np.mean(np.log(np.clip(true_probs, eps, 1.0))))
    brier = float(
        np.mean(
            [
                np.sum((probs[i] - np.array([1.0 if c == y_true[i] else 0.0 for c in labels])) ** 2)
                for i in range(n)
            ]
        )
    )

    confusion_matrix = [
        [int(np.sum((y_true == t) & (y_pred == p))) for p in labels] for t in labels
    ]

    confidence = np.max(probs, axis=1)
    hit = (y_true == y_pred).astype(float)
    calibration = []
    for name, low, high in _CALIBRATION_BUCKETS:
        if low is None:
            mask = confidence <= high
        elif high is None:
            mask = confidence > low
        else:
            mask = (confidence > low) & (confidence <= high)
        cnt = int(mask.sum())
        calibration.append(
            {
                "bucket": name,
                "n": cnt,
                "hit_rate": _r(hit[mask].mean()) if cnt else None,
                "avg_confidence": _r(confidence[mask].mean()) if cnt else None,
                "share": _r(cnt / n),
            }
        )

    def _group(key_fn) -> list[dict]:
        groups: dict[str, list[int]] = {}
        for i in range(n):
            groups.setdefault(key_fn(dates[i]), []).append(i)
        out = []
        for key in sorted(groups):
            idx = np.array(groups[key])
            out.append(
                {
                    "period": key,
                    "n": int(len(idx)),
                    "accuracy": _r((y_true[idx] == y_pred[idx]).mean()),
                    "hit_rate": _r((y_true[idx] == y_pred[idx]).mean()),
                }
            )
        return out

    by_year = _group(lambda d: d[:4])
    by_quarter = _group(lambda d: f"{d[:4]}Q{(int(d[5:7]) - 1) // 3 + 1}")

    return {
        "accuracy": _r(accuracy),
        "accuracy_ci95": [_r(acc_lo), _r(acc_hi)],
        "n": n,
        "logloss": _r(logloss),
        "brier": _r(brier),
        "per_class": per_class,
        "confusion": {"labels": labels, "matrix": confusion_matrix},
        "calibration": calibration,
        "by_year": by_year,
        "by_quarter": by_quarter,
    }


# ---------------------------------------------------------------------------
# 交易模拟（胜率口径）
# ---------------------------------------------------------------------------
def _series(rows: pd.DataFrame, column: str) -> np.ndarray:
    if column not in rows.columns:
        return np.full(len(rows), np.nan)
    return pd.to_numeric(rows[column], errors="coerce").to_numpy(dtype=float)


def _adj_factor_series(rows: pd.DataFrame) -> np.ndarray:
    """复权因子序列（缺失/非法值按 1.0）；用于把成交价换算到前复权口径。"""
    factors = _series(rows, "adj_factor")
    factors = np.where(np.isfinite(factors) & (factors > 0), factors, 1.0)
    return factors


def _next_entry_idx(
    open_, limit_up, limit_down, suspended, start_idx: int, side: str, max_wait: int = 10
) -> int | None:
    """从 ``start_idx`` 起找可成交的开盘（停牌顺延；涨跌停拒单）。"""
    last = min(len(open_), start_idx + max_wait + 1)
    for j in range(start_idx, last):
        if suspended[j]:
            continue
        if side == "long":
            if np.isfinite(limit_up[j]) and open_[j] >= limit_up[j] - 1e-9:
                return None  # 开盘涨停，买不进
        else:
            if np.isfinite(limit_down[j]) and open_[j] <= limit_down[j] + 1e-9:
                return None  # 开盘跌停，做空成交不了
        return j
    return None


def _next_exit_idx(
    open_, limit_up, limit_down, suspended, start_idx: int, side: str, max_wait: int = 10
) -> int | None:
    """找平仓日：停牌顺延；跌停卖不出 / 涨停买不回时顺延。"""
    last = min(len(open_), start_idx + max_wait + 1)
    for j in range(start_idx, last):
        if suspended[j]:
            continue
        if side == "long":
            if np.isfinite(limit_down[j]) and open_[j] <= limit_down[j] + 1e-9:
                continue  # 跌停无法卖出
        else:
            if np.isfinite(limit_up[j]) and open_[j] >= limit_up[j] - 1e-9:
                continue  # 涨停无法买回
        return j
    return None


def simulate_trades(
    daily: pd.DataFrame,
    signals: pd.DataFrame,
    cfg: dict,
    symbols: list[str],
    horizon_days: int = 1,
    eval_start: str | None = None,
    eval_end: str | None = None,
    initial_cash: float | None = None,
) -> dict:
    """按信号方向模拟交易（T+1 开盘成交），返回成交明细 + 组合净值曲线。"""
    paper = cfg.get("paper") or {}
    slippage = float(paper.get("slippage_bps", 5.0)) / 10000.0
    lot = max(int(paper.get("lot_size", 100) or 1), 1)
    cash0 = float(initial_cash if initial_cash is not None else paper.get("initial_cash", 1_000_000))
    warns: list[str] = []
    if not symbols:
        raise ValueError("symbols 不能为空")
    notional = cash0 / len(symbols)

    eval_start = eval_start or str(daily["trade_date"].min())
    eval_end = eval_end or str(daily["trade_date"].max())

    sig_dir: dict[str, dict[str, str]] = {}
    for row in signals.itertuples(index=False):
        sig_dir.setdefault(str(row.symbol), {})[str(row.trade_date)] = str(row.direction)

    trades: list[dict] = []
    skip_reasons: Counter[str] = Counter()
    limit_slipped = 0
    suspended_rolls = 0

    for symbol in symbols:
        g = daily[daily["symbol"] == symbol].sort_values("trade_date").reset_index(drop=True)
        if len(g) == 0:
            skip_reasons["no_daily_rows"] += 1
            continue
        dates = g["trade_date"].astype(str).tolist()
        pos_of = {d: i for i, d in enumerate(dates)}
        open_ = _series(g, "open")
        close = _series(g, "close")
        pre_close = _series(g, "pre_close")
        limit_up = _series(g, "limit_up")
        limit_down = _series(g, "limit_down")
        adj_factor = _adj_factor_series(g)
        suspended = np.asarray(_series(g, "is_suspended")) == 1
        for i in range(len(g)):
            if not np.isfinite(limit_up[i]) or not np.isfinite(limit_down[i]):
                pre = pre_close[i] if np.isfinite(pre_close[i]) else close[i - 1] if i else close[i]
                up, down = _limit_fallback(float(pre), symbol)
                limit_up[i], limit_down[i] = up, down

        last_idx = max(i for i, d in enumerate(dates) if d <= eval_end) if any(
            d <= eval_end for d in dates
        ) else len(dates) - 1

        last_exit_idx = -1
        for signal_date in sorted(sig_dir.get(symbol, {})):
            direction = sig_dir[symbol][signal_date]
            if direction == "flat":
                skip_reasons["flat_signal"] += 1
                continue
            signal_idx = pos_of.get(signal_date)
            if signal_idx is None:
                skip_reasons["signal_date_not_in_daily"] += 1
                continue
            side = "long" if direction == "up" else "short"
            entry_idx = _next_entry_idx(
                open_, limit_up, limit_down, suspended, signal_idx + 1, side
            )
            if entry_idx is None:
                skip_reasons["entry_limit_reject"] += 1
                continue
            if entry_idx > last_idx:
                skip_reasons["entry_beyond_eval"] += 1
                continue
            if entry_idx > signal_idx + 1:
                suspended_rolls += 1  # 停牌顺延成交日
            if entry_idx < last_exit_idx:
                skip_reasons["overlap_with_open_position"] += 1
                continue
            exit_idx = _next_exit_idx(
                open_, limit_up, limit_down, suspended,
                entry_idx + max(horizon_days, 1), side,
            )
            if exit_idx is None or exit_idx > last_idx:
                skip_reasons["exit_beyond_eval_or_blocked"] += 1
                continue
            if exit_idx > entry_idx + max(horizon_days, 1):
                limit_slipped += 1

            # 成交价：开盘价 ± 滑点，并夹在涨跌停价内（契约 §6）
            raw_open_entry = float(open_[entry_idx])
            raw_open_exit = float(open_[exit_idx])
            if side == "long":
                entry_px = min(raw_open_entry * (1 + slippage), float(limit_up[entry_idx]))
                exit_px = max(raw_open_exit * (1 - slippage), float(limit_down[exit_idx]))
            else:
                entry_px = max(raw_open_entry * (1 - slippage), float(limit_down[entry_idx]))
                exit_px = min(raw_open_exit * (1 + slippage), float(limit_up[exit_idx]))
            if not (np.isfinite(entry_px) and np.isfinite(exit_px)) or entry_px <= 0 or exit_px <= 0:
                skip_reasons["invalid_price"] += 1
                continue

            # 整手 + 现金约束（税费按成交时点重算）
            qty = int(math.floor(notional / entry_px / lot) * lot) if lot > 1 else int(
                math.floor(notional / entry_px)
            )
            fee_side_open = "buy" if side == "long" else "sell"
            fee_side_close = "sell" if side == "long" else "buy"
            while qty > 0:
                amount_entry = qty * entry_px
                fee_entry = float(_fees(fee_side_open, amount_entry, cfg)["total"])
                if side == "long":
                    if amount_entry + fee_entry <= notional + 1e-9:
                        break
                else:
                    if fee_entry <= notional + 1e-9:
                        break
                qty -= lot if lot > 1 else 1
            if qty <= 0:
                skip_reasons["notional_too_small"] += 1
                continue
            amount_entry = qty * entry_px
            amount_exit = qty * exit_px
            fee_entry = float(_fees(fee_side_open, amount_entry, cfg)["total"])
            fee_exit = float(_fees(fee_side_close, amount_exit, cfg)["total"])
            af_entry = float(adj_factor[entry_idx])
            af_exit = float(adj_factor[exit_idx])
            # 前复权口径收益：抹平分红除权造成的价格跳空（费用按不复权成交额计）
            if side == "long":
                gross_ret = (exit_px * af_exit) / (entry_px * af_entry) - 1.0
            else:
                gross_ret = 1.0 - (exit_px * af_exit) / (entry_px * af_entry)
            pnl = amount_entry * gross_ret - fee_entry - fee_exit
            ret = pnl / amount_entry

            trades.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "direction": direction,
                    "signal_date": signal_date,
                    "entry_date": dates[entry_idx],
                    "entry_idx": entry_idx,
                    "entry_price": round(entry_px, 4),
                    "exit_date": dates[exit_idx],
                    "exit_idx": exit_idx,
                    "exit_price": round(exit_px, 4),
                    "qty": qty,
                    "amount_entry": round(amount_entry, 2),
                    "fees": round(fee_entry + fee_exit, 4),
                    "fee_entry": round(fee_entry, 4),
                    "fee_exit": round(fee_exit, 4),
                    "pnl": round(pnl, 4),
                    "return_net": round(ret, 8),
                    "hold_days": exit_idx - entry_idx,
                    "af_entry": af_entry,
                    "af_exit": af_exit,
                }
            )
            last_exit_idx = exit_idx

    equity_dates, equity_values = _portfolio_equity(
        daily, symbols, trades, notional, cash0, eval_start, eval_end
    )
    if not trades:
        warns.append("窗口内没有可成交交易（胜率/收益率指标无意义）")
    if skip_reasons.get("overlap_with_open_position"):
        warns.append(
            f"{skip_reasons['overlap_with_open_position']} 个信号因持仓未平被跳过（同一标的单仓位）"
        )
    if limit_slipped:
        warns.append(f"{limit_slipped} 笔交易因涨跌停无法按时平仓，顺延到下一可成交日")
    if suspended_rolls:
        warns.append(f"{suspended_rolls} 笔交易因停牌顺延成交日")

    return {
        "trades": trades,
        "skip_reasons": dict(skip_reasons),
        "equity_curve": {
            "dates": equity_dates,
            "equity": [round(v, 2) for v in equity_values],
            "normalized": [round(v / cash0, 6) for v in equity_values] if cash0 else [],
        },
        "initial_cash": cash0,
        "notional_per_symbol": notional,
        "warnings": warns,
    }


def _portfolio_equity(
    daily: pd.DataFrame,
    symbols: list[str],
    trades: list[dict],
    notional: float,
    cash0: float,
    eval_start: str,
    eval_end: str,
) -> tuple[list[str], list[float]]:
    """组合净值：每只股票一个独立 sleeve。

    口径：每个 sleeve 初始名义资金 ``notional`` 固定不变（不随盈利加仓），
    净值 = 初始资金 + 已实现盈亏 + 当前持仓浮盈；组合净值为各 sleeve 等权加总。
    """
    window = daily[(daily["trade_date"] >= eval_start) & (daily["trade_date"] <= eval_end)]
    all_dates = sorted(window["trade_date"].astype(str).unique())
    if not all_dates:
        return ([], [])
    trades_by_symbol: dict[str, list[dict]] = {}
    for trade in trades:
        trades_by_symbol.setdefault(trade["symbol"], []).append(trade)

    sleeves: list[pd.Series] = []
    for symbol in symbols:
        by_entry = {t["entry_idx"]: t for t in trades_by_symbol.get(symbol, [])}
        g = daily[daily["symbol"] == symbol].sort_values("trade_date").reset_index(drop=True)
        if len(g) == 0:
            sleeves.append(pd.Series(notional, index=all_dates, dtype=float))
            continue
        dates = g["trade_date"].astype(str).tolist()
        close = _series(g, "close")
        adj_factor = _adj_factor_series(g)
        idx_in_window = [i for i, d in enumerate(dates) if eval_start <= d <= eval_end]
        if not idx_in_window:
            sleeves.append(pd.Series(notional, index=all_dates, dtype=float))
            continue
        values: dict[str, float] = {}
        pos: dict | None = None
        base = notional  # 初始资金 + 已实现盈亏
        for k in range(idx_in_window[0], idx_in_window[-1] + 1):
            if pos is not None and k == pos["exit_idx"]:
                base += pos["pnl"]
                pos = None
            if pos is None and k in by_entry:
                pos = by_entry[k]
            if pos is None:
                mark = base
            elif pos["side"] == "long":
                mark = base + pos["amount_entry"] * (
                    (close[k] * adj_factor[k]) / (pos["entry_price"] * pos["af_entry"])
                    - 1.0
                ) - pos["fee_entry"]
            else:
                mark = base + pos["amount_entry"] * (
                    1.0
                    - (close[k] * adj_factor[k]) / (pos["entry_price"] * pos["af_entry"])
                ) - pos["fee_entry"]
            values[dates[k]] = mark
        series = pd.Series(values, dtype=float).reindex(all_dates).ffill().fillna(notional)
        sleeves.append(series)

    if not sleeves:
        return (all_dates, [cash0] * len(all_dates))
    total = sum(sleeves)
    return (all_dates, [float(v) for v in total.to_numpy()])


def compute_trade_metrics(sim: dict, initial_cash: float) -> dict:
    """由成交明细 + 净值曲线计算胜率口径指标（§12.2）。"""
    trades = sim["trades"]
    returns = np.array([t["return_net"] for t in trades], dtype=float)
    wins = int(np.sum(returns > 0))
    losses = int(np.sum(returns < 0))
    flats = int(np.sum(returns == 0))
    total = int(len(returns))
    win_rate = wins / total if total else None
    wilson = wilson_interval(wins, total) if total else (None, None)
    avg_return = float(returns.mean()) if total else None
    avg_win = float(returns[returns > 0].mean()) if wins else None
    avg_loss = float(returns[returns < 0].mean()) if losses else None
    payoff = abs(avg_win / avg_loss) if (avg_win is not None and avg_loss not in (None, 0.0)) else None

    by_direction: dict[str, dict] = {}
    for side in ("long", "short"):
        idx = [i for i, t in enumerate(trades) if t["side"] == side]
        sub = returns[idx] if idx else np.array([], dtype=float)
        w = int(np.sum(sub > 0))
        by_direction[side] = {
            "n": int(len(sub)),
            "win": w,
            "loss": int(np.sum(sub < 0)),
            "flat": int(np.sum(sub == 0)),
            "win_rate": _r(w / len(sub)) if len(sub) else None,
            "wilson95": [_r(x) for x in wilson_interval(w, len(sub))] if len(sub) else [None, None],
            "avg_return_per_trade": _r(sub.mean()) if len(sub) else None,
        }

    eq = np.array(sim["equity_curve"]["equity"], dtype=float)
    norm = eq / initial_cash if initial_cash else np.array([])
    total_return = float(eq[-1] / eq[0] - 1.0) if len(eq) >= 1 and eq[0] else None
    if len(norm) > 1:
        running_max = np.maximum.accumulate(norm)
        drawdowns = norm / running_max - 1.0
        max_drawdown = float(np.min(drawdowns))
    else:
        max_drawdown = 0.0 if len(norm) else None
    sharpe = None
    if len(norm) >= 3:
        daily_returns = norm[1:] / norm[:-1] - 1.0
        std = float(np.std(daily_returns, ddof=1))
        if std > 0:
            sharpe = float(np.mean(daily_returns) / std * math.sqrt(TRADING_DAYS_PER_YEAR))
    sample_sufficient = total >= 30
    note = "样本充足" if sample_sufficient else "样本不足（<30 笔），胜率仅供参考"
    if total == 0:
        note = "无成交，胜率无定义"

    return {
        "win_rate": _r(win_rate),
        "wilson95": [_r(x) for x in wilson],
        "sample_sufficient": sample_sufficient,
        "note": note,
        "trades": {"total": total, "win": wins, "loss": losses, "flat": flats},
        "win_rate_by_direction": by_direction,
        "avg_return_per_trade": _r(avg_return),
        "avg_win": _r(avg_win),
        "avg_loss": _r(avg_loss),
        "payoff_ratio": _r(payoff),
        "max_drawdown": _r(max_drawdown),
        "sharpe": _r(sharpe),
        "total_return": _r(total_return),
        "equity_curve": sim["equity_curve"],
        "trades_detail": [_jsonable(t) for t in trades],
        "skip_reasons": sim.get("skip_reasons", {}),
    }


# ---------------------------------------------------------------------------
# walk-forward 切分
# ---------------------------------------------------------------------------
def build_walk_forward_windows(
    start: str, end: str, splits: int | None
) -> list[dict]:
    """按自然季度切测试窗；训练窗为窗口开始前的全部历史。"""
    p_start = pd.Timestamp(start).normalize()
    p_end = pd.Timestamp(end).normalize()
    if p_start > p_end:
        raise ValueError(f"回测区间非法: {start} > {end}")
    windows: list[dict] = []
    for period in pd.period_range(p_start, p_end, freq="Q"):
        w_start = max(period.start_time.normalize(), p_start)
        w_end = min(period.end_time.normalize(), p_end)
        if w_start > w_end:
            continue
        windows.append(
            {"start": w_start.strftime("%Y-%m-%d"), "end": w_end.strftime("%Y-%m-%d")}
        )
    if splits and splits > 0 and len(windows) > splits:
        windows = windows[-splits:]
    for i, window in enumerate(windows):
        window["fold"] = i
    return windows


def _train_test_frames(
    features: pd.DataFrame, window: dict
) -> tuple[pd.DataFrame, pd.DataFrame]:
    usable = features.dropna(subset=[*FEATURE_COLUMNS, "label"])
    train = usable[usable["trade_date"] < window["start"]]
    test = usable[
        (usable["trade_date"] >= window["start"]) & (usable["trade_date"] <= window["end"])
    ]
    return train.reset_index(drop=True), test.reset_index(drop=True)


def _train_span_days(train: pd.DataFrame) -> int:
    if len(train) == 0:
        return 0
    lo = pd.Timestamp(train["trade_date"].min())
    hi = pd.Timestamp(train["trade_date"].max())
    return int((hi - lo).days)


# ---------------------------------------------------------------------------
# 基准策略
# ---------------------------------------------------------------------------
def _majority_label(labels: np.ndarray) -> str:
    counts = Counter(str(x) for x in labels)
    return max(LABEL_CLASSES, key=lambda c: (counts.get(c, 0), -LABEL_CLASSES.index(c)))


def _ma5_cross_directions(daily: pd.DataFrame) -> dict[str, dict[str, str]]:
    """ma5_cross 基准：收盘上穿/下穿 5 日均线时给方向，其余 flat。"""
    result: dict[str, dict[str, str]] = {}
    for symbol, g in daily.groupby("symbol", sort=False):
        g = g.sort_values("trade_date").reset_index(drop=True)
        if "adj_close" in g.columns and g["adj_close"].notna().any():
            close = pd.to_numeric(g["adj_close"], errors="coerce")
        else:
            close = pd.to_numeric(g["close"], errors="coerce")
        ma5 = close.rolling(5, min_periods=5).mean()
        prev_close = close.shift(1)
        prev_ma5 = ma5.shift(1)
        up = (close > ma5) & (prev_close <= prev_ma5)
        down = (close < ma5) & (prev_close >= prev_ma5)
        directions = np.where(up, "up", np.where(down, "down", "flat"))
        result[str(symbol)] = dict(zip(g["trade_date"].astype(str), directions))
    return result


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def _fmt(value, digits: int = 4, pct: bool = False) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "n/a"
    if pct:
        return f"{number * 100:.2f}%"
    return f"{number:.{digits}f}"


def _fmt_pair(pair) -> str:
    if not pair or pair[0] is None or pair[1] is None:
        return "n/a"
    return f"[{pair[0] * 100:.2f}%, {pair[1] * 100:.2f}%]"


def render_markdown(result: dict) -> str:
    """渲染人读版回测报告（契约 §12：方向 / 胜率 / 校准 / 稳定性 / 基准 / walk-forward）。"""
    meta = result["meta"]
    direction = result["direction"]
    win = result["win_rate"]
    wf = result["walk_forward"]
    benchmarks = result["benchmarks"]
    lines: list[str] = []
    add = lines.append

    add("# 历史回测报告（walk-forward，样本外）")
    add("")
    add("> ⚠️ 本项目仅供学习研究，输出不构成任何投资建议。市场有风险，决策自负。")
    add("")
    add(f"- 生成时间：{result['generated_at']}")
    add(f"- 数据区间：{meta['data_range'][0]} ~ {meta['data_range'][1]}")
    add(f"- 请求回测区间：{meta['eval_range'][0]} ~ {meta['eval_range'][1]}；**实际评估区间（有测试样本）**：{meta['evaluated_range'][0]} ~ {meta['evaluated_range'][1]}")
    add(f"- 股票池：{', '.join(meta['symbols'])}")
    add(f"- 评估样本量：{meta['n_samples']}（特征 {meta['n_features']} 个；horizon={meta['horizon']}；标签阈值={_fmt(meta['label_threshold'], 3, pct=True)}）")
    add(f"- 模型版本：{meta['model_version']}；随机种子：{meta['random_seed']}")
    add(f"- 切分方式：{wf['rule']}")
    add(f"- 标签规则：`{meta['label_rule']}`")
    add("")

    add("## 1. 方向判断指标（预测质量，§12.1）")
    add("")
    add("| 指标 | 值 |")
    add("|---|---|")
    add(f"| 三分类准确率 | **{_fmt(direction['accuracy'], 4, pct=True)}** |")
    add(f"| 准确率 95% Wilson CI | {_fmt_pair(direction['accuracy_ci95'])} |")
    add(f"| 样本量 n | {direction['n']} |")
    add(f"| coverage（有信号样本占比） | {_fmt(direction['coverage'], 4, pct=True)} |")
    add(f"| logloss | {_fmt(direction['logloss'])}；均匀分布基准 log(3)={math.log(3):.4f} |")
    add(f"| multiclass Brier | {_fmt(direction['brier'])}（均匀分布基准 0.6667） |")
    add("")
    add("### 分方向 precision / recall / F1")
    add("")
    add("| 方向 | precision | recall | F1 | 真实样本数 | 预测数 |")
    add("|---|---|---|---|---|---|")
    for cls in LABEL_CLASSES:
        pc = direction["per_class"][cls]
        add(
            f"| {cls} | {_fmt(pc['precision'])} | {_fmt(pc['recall'])} | "
            f"{_fmt(pc['f1'])} | {pc['support']} | {pc['predicted']} |"
        )
    add("")
    add("### 3×3 混淆矩阵（行=真实，列=预测）")
    add("")
    labels = direction["confusion"]["labels"]
    add("| 真实\\预测 | " + " | ".join(labels) + " |")
    add("|---|" + "|".join("---" for _ in labels) + "|")
    for i, cls in enumerate(labels):
        add(f"| {cls} | " + " | ".join(str(v) for v in direction["confusion"]["matrix"][i]) + " |")
    add("")
    add("### 置信度校准（按 max(prob) 分桶）")
    add("")
    add("| 置信度桶 | n | 实际命中率 | 平均置信度 | 占比 |")
    add("|---|---|---|---|---|")
    for bucket in direction["calibration"]:
        add(
            f"| {bucket['bucket']} | {bucket['n']} | {_fmt(bucket['hit_rate'], 4, pct=True)} | "
            f"{_fmt(bucket['avg_confidence'], 4, pct=True)} | {_fmt(bucket['share'], 4, pct=True)} |"
        )
    add("")
    add("### 稳定性：分年度 / 分季度准确率")
    add("")
    add("| 年度 | n | 准确率 |")
    add("|---|---|---|")
    for row in direction["by_year"]:
        add(f"| {row['period']} | {row['n']} | {_fmt(row['accuracy'], 4, pct=True)} |")
    add("")
    add("| 季度 | n | 准确率 |")
    add("|---|---|---|")
    for row in direction["by_quarter"]:
        add(f"| {row['period']} | {row['n']} | {_fmt(row['accuracy'], 4, pct=True)} |")
    add("")

    add("## 2. 胜率（交易口径，§12.2 · 与方向准确率分开）")
    add("")
    add(f"- 交易规则：T 日收盘出信号 → T+1 开盘 ± 滑点成交；持有一个 horizon 后开盘平仓；涨跌停拒单/停牌顺延；含佣金、印花税、过户费。做空为研究性模拟。")
    add(f"- 组合初始资金：{_fmt(win['equity_curve']['equity'][0] if win['equity_curve']['equity'] else None, 2)}；单票名义资金：{_fmt(result['meta']['notional_per_symbol'], 2)}")
    add("")
    add("| 指标 | 值 |")
    add("|---|---|")
    add(f"| 胜率（盈利平仓/平仓次数） | **{_fmt(win['win_rate'], 4, pct=True)}** {('· ' + win['note']) if win['win_rate'] is not None else ''} |")
    add(f"| 胜率 95% Wilson CI | {_fmt_pair(win['wilson95'])} |")
    add(f"| 交易笔数（总/盈/亏/平） | {win['trades']['total']} / {win['trades']['win']} / {win['trades']['loss']} / {win['trades']['flat']} |")
    add(f"| 单笔平均收益 | {_fmt(win['avg_return_per_trade'], 6, pct=True)} |")
    add(f"| 盈亏比 payoff（平均盈利/平均亏损） | {_fmt(win['payoff_ratio'])} |")
    add(f"| 累计收益 total_return | {_fmt(win['total_return'], 4, pct=True)} |")
    add(f"| 最大回撤 max_drawdown | {_fmt(win['max_drawdown'], 4, pct=True)} |")
    add(f"| Sharpe（日频年化，rf=0） | {_fmt(win['sharpe'])} |")
    add("")
    add("### 分方向胜率（long/short；flat 不参与交易）")
    add("")
    add("| 方向 | 笔数 | 胜/负/平 | 胜率 | 95% Wilson CI | 单笔平均收益 |")
    add("|---|---|---|---|---|---|")
    for side in ("long", "short"):
        w = win["win_rate_by_direction"][side]
        add(
            f"| {side} | {w['n']} | {w['win']}/{w['loss']}/{w['flat']} | "
            f"{_fmt(w['win_rate'], 4, pct=True)} | {_fmt_pair(w['wilson95'])} | "
            f"{_fmt(w['avg_return_per_trade'], 6, pct=True)} |"
        )
    add("")
    eq = win["equity_curve"]
    if eq["dates"]:
        add(
            f"净值曲线：{eq['dates'][0]} 起 {len(eq['dates'])} 个交易日，"
            f"末值 {_fmt(eq['equity'][-1], 2)}（归一 {_fmt(eq['normalized'][-1], 4)}）；"
            f"完整数据见 backtest.json 的 `win_rate.equity_curve`。"
        )
        add("")

    add("## 3. 基准对比（§12.3，同一评估样本、同一交易口径）")
    add("")
    add("| 策略 | 方向准确率 | 胜率 | 累计收益 | 交易笔数 | 相对模型准确率提升 | 结论（vs 模型 95% CI） |")
    add("|---|---|---|---|---|---|---|")
    model_acc = direction["accuracy"]
    add(
        f"| **模型（softmax）** | {_fmt(model_acc, 4, pct=True)} | {_fmt(win['win_rate'], 4, pct=True)} | "
        f"{_fmt(win['total_return'], 4, pct=True)} | {win['trades']['total']} | — | — |"
    )
    for key in BENCHMARKS:
        bench = benchmarks[key]
        cmp_row = next((c for c in benchmarks["comparison"] if c["benchmark"] == key), {})
        add(
            f"| {key} | {_fmt(bench['accuracy'], 4, pct=True)} | {_fmt(bench['win_rate'], 4, pct=True)} | "
            f"{_fmt(bench['total_return'], 4, pct=True)} | {bench['trades']['total']} | "
            f"{_fmt(cmp_row.get('accuracy_diff_pp'), 2)} pp | {cmp_row.get('verdict', 'n/a')} |"
        )
    add("")
    add("相对提升口径：准确率差（pp）= 模型准确率 − 基准准确率；若基准准确率落在模型 95% CI 之外，视为差异显著（但仍是同一测试集内的近似判断，非严格统计检验）。")
    add("")

    add("## 4. Walk-forward 切分明细（§12.4）")
    add("")
    add(f"- 规则：{wf['rule']}")
    add("")
    add("| fold | 训练区间 | 训练样本 | 测试区间 | 测试样本 | 准确率 | logloss | 胜率 | 收益 |")
    add("|---|---|---|---|---|---|---|---|---|")
    for split in wf["splits"]:
        add(
            f"| {split['fold']} | {split['train_range'][0]} ~ {split['train_range'][1]} | {split['n_train']} | "
            f"{split['test_range'][0]} ~ {split['test_range'][1]} | {split['n_test']} | "
            f"{_fmt(split['accuracy'], 4, pct=True)} | {_fmt(split['logloss'])} | "
            f"{_fmt(split['win_rate'], 4, pct=True)} | {_fmt(split['total_return'], 4, pct=True)} |"
        )
    add("")

    if result.get("warnings"):
        add("## 5. 警告与局限")
        add("")
        for warning in result["warnings"]:
            add(f"- {warning}")
        add("")
    add("## 6. 口径与局限说明")
    add("")
    add("- 方向准确率基于 T+1 收盘对收盘标签；胜率基于可成交的 T+1 开盘交易，两者口径不同，不可混用。")
    add("- 单票单仓位：同一标的持仓期间不重复开仓；组合净值按各标的 sleeve 等权盯市加总。")
    add("- 做空仅用于研究口径的交易质量评估（A 股普通账户不能直接做空，现实中需两融等条件）。")
    add("- 未建模资金冲击、停牌估值、分红再投资、手续费阶梯等细节；涨跌停/停牌按简化规则处理。")
    add("- walk-forward 每个窗口用窗口开始前的全部历史重训，禁止使用测试期数据训练或调参。")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def _resolve_ranges(start: str | None, end: str | None, cfg: dict) -> tuple[str, str]:
    bt = cfg.get("backtest") or {}
    resolved_start = str(start or bt.get("start") or "2025-01-01")
    resolved_end = str(end or bt.get("end") or datetime.now(TZ).strftime("%Y-%m-%d"))
    if pd.Timestamp(resolved_start) > pd.Timestamp(resolved_end):
        raise ValueError(f"回测区间非法: {resolved_start} > {resolved_end}")
    return resolved_start, resolved_end


def _model_version(cfg: dict) -> tuple[str, dict | None]:
    path = resolve_model_path(cfg)
    if path.exists():
        try:
            model = json.loads(path.read_text(encoding="utf-8"))
            return str(model.get("version", "unknown")), model
        except Exception:  # pragma: no cover - 损坏模型文件不影响回测
            return "unknown", None
    return "untrained", None


def _sync_model_metrics(cfg: dict, metrics: dict) -> bool:
    """把样本外指标同步进 ``model.json.metrics``（契约 §12.4）。"""
    path = resolve_model_path(cfg)
    if not path.exists():
        return False
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    model["metrics"] = _jsonable(metrics)
    _atomic_write_json(path, model)
    return True


def _check_contract(result: dict) -> list[str]:
    """自检 §12 必备字段，返回缺失路径列表。"""
    missing: list[str] = []

    def walk(node, path: str) -> None:
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                missing.append(path)
                return
            node = node[key]

    for path in REQUIRED_CONTRACT_PATHS:
        walk(result, path)
    return missing


def run_backtest(start: str | None, end: str | None, cfg: dict) -> dict:
    """执行 walk-forward 回测，写 ``backtest.md`` + ``backtest.json``，返回指标 dict。"""
    start, end = _resolve_ranges(start, end, cfg)
    model_cfg = cfg.get("model") or {}
    symbols = [str(s).strip().zfill(6) for s in (cfg.get("symbols") or [])]
    if not symbols:
        raise ValueError("配置里没有 symbols，无法回测")
    paper = cfg.get("paper") or {}
    initial_cash = float(paper.get("initial_cash", 1_000_000))
    horizon_days = int(model_cfg.get("horizon_days", 1))
    label_threshold = float(model_cfg.get("label_threshold", 0.005))
    random_seed = int(model_cfg.get("random_seed", 42))
    train_years = float(model_cfg.get("train_years", 2))
    splits = int(model_cfg.get("walk_forward_splits", 4))
    min_train_days = 365
    min_train_rows = 300
    min_test_rows = 20
    warnings: list[str] = []

    # 载入历史：至少覆盖回测起点前 1.5 年，保证首个窗口训练窗 ≥ 1 年
    data_cfg = cfg.get("data") or {}
    configured_start = str(data_cfg.get("daily_start") or "2022-01-01")
    needed_start = (pd.Timestamp(start) - pd.Timedelta(days=int(365 * 1.5) + 20)).strftime("%Y-%m-%d")
    load_start = min(configured_start, needed_start)
    daily = load_daily_multi(symbols, cfg, start=load_start, end=end)
    if len(daily) == 0:
        raise RuntimeError(
            f"行情库中没有 {symbols} 在 {load_start}~{end} 的日线数据（db={resolve_db_path(cfg)}），请先 fetch"
        )
    available_symbols = sorted(str(x) for x in daily["symbol"].unique())
    missing_symbols = [s for s in symbols if s not in available_symbols]
    if missing_symbols:
        warnings.append(
            f"行情库缺少 {missing_symbols} 的日线，回测仅用实际有数据的 {available_symbols}"
        )
        symbols = [s for s in symbols if s in available_symbols]
    if not symbols:
        raise RuntimeError(f"行情库中没有配置 symbols {cfg.get('symbols')} 的任何数据")
    eval_daily = daily[(daily["trade_date"] >= start) & (daily["trade_date"] <= end)]
    if len(eval_daily) == 0:
        raise RuntimeError(f"行情库在评估区间 {start}~{end} 没有数据")

    features = build_features(daily, horizon_days, label_threshold)
    usable = features.dropna(subset=[*FEATURE_COLUMNS, "label"])
    if len(usable) == 0:
        raise RuntimeError("特征样本全为空（历史数据可能不足 60 个交易日）")

    all_windows = build_walk_forward_windows(start, end, 0)
    windows = build_walk_forward_windows(start, end, splits)
    if not windows:
        raise RuntimeError(f"无法在 {start}~{end} 内构造 walk-forward 窗口")
    if len(windows) < len(all_windows):
        warnings.append(
            f"请求区间共 {len(all_windows)} 个自然季度，按 walk_forward_splits={splits} "
            f"只评估最近 {len(windows)} 个季度（{windows[0]['start']} ~ {windows[-1]['end']}）；"
            "如需覆盖整段请调大 model.walk_forward_splits"
        )

    oof_parts: list[pd.DataFrame] = []
    fold_records: list[dict] = []
    for window in windows:
        train, test = _train_test_frames(features, window)
        span = _train_span_days(train)
        if len(test) < min_test_rows:
            warnings.append(
                f"fold {window['fold']} 测试样本 {len(test)} < {min_test_rows}，已跳过（{window['start']}~{window['end']}）"
            )
            continue
        if len(train) < min_train_rows or span < min_train_days:
            warnings.append(
                f"fold {window['fold']} 训练样本 {len(train)} / 跨度 {span} 天不足（需 ≥{min_train_rows} 且 ≥{min_train_days} 天），已跳过"
            )
            continue
        X_train, names = feature_matrix(train, FEATURE_COLUMNS)
        y_train = train["label"].to_numpy()
        try:
            model = fit_softmax_model(X_train, y_train, names, seed=random_seed)
        except (RuntimeError, ValueError) as exc:
            warnings.append(f"fold {window['fold']} 训练失败并跳过: {exc}")
            continue
        X_test, _ = feature_matrix(test, names)
        probs = predict_proba_batch(model, X_test)
        classes = [str(c) for c in model["classes"]]
        pred = np.array([classes[int(np.argmax(row))] for row in probs])
        y_test = test["label"].to_numpy()
        fold = window["fold"]
        part = pd.DataFrame(
            {
                "symbol": test["symbol"].astype(str).to_numpy(),
                "trade_date": test["trade_date"].astype(str).to_numpy(),
                "y_true": y_test,
                "y_pred": pred,
                "prob_down": probs[:, classes.index("down")],
                "prob_flat": probs[:, classes.index("flat")],
                "prob_up": probs[:, classes.index("up")],
                "fold": fold,
                "train_rows": len(train),
                "train_start": str(train["trade_date"].min()),
                "train_end": str(train["trade_date"].max()),
            }
        )
        oof_parts.append(part)

        fold_dir = compute_direction_metrics(
            y_test, pred, probs, test["trade_date"].to_numpy(), classes
        )
        fold_records.append(
            {
                "fold": fold,
                "train_range": [str(train["trade_date"].min()), str(train["trade_date"].max())],
                "test_range": [window["start"], window["end"]],
                "n_train": int(len(train)),
                "n_test": int(len(test)),
                "train_span_days": int(span),
                "accuracy": fold_dir["accuracy"],
                "logloss": fold_dir["logloss"],
                "win_rate": None,
                "total_return": None,
            }
        )

    if not oof_parts:
        raise RuntimeError("所有 walk-forward 窗口都因数据不足被跳过，无法回测（请扩大数据区间）")
    oof = pd.concat(oof_parts, ignore_index=True).sort_values(
        ["trade_date", "symbol"]
    ).reset_index(drop=True)
    fold_models = len(fold_records)

    # 方向指标（样本外 OOF 汇总）
    y_true = oof["y_true"].to_numpy()
    y_pred = oof["y_pred"].to_numpy()
    prob_cols = ["prob_down", "prob_flat", "prob_up"]
    probs = oof[prob_cols].to_numpy(dtype=float)
    direction = compute_direction_metrics(
        y_true, y_pred, probs, oof["trade_date"].to_numpy(), LABEL_CLASSES
    )

    used_ranges = [(r["test_range"][0], r["test_range"][1]) for r in fold_records]
    used_mask = pd.Series(False, index=eval_daily.index)
    for range_start, range_end in used_ranges:
        used_mask |= (eval_daily["trade_date"] >= range_start) & (
            eval_daily["trade_date"] <= range_end
        )
    expected_pairs = (
        eval_daily.loc[used_mask, ["symbol", "trade_date"]].drop_duplicates().shape[0]
    )
    coverage = len(oof) / expected_pairs if expected_pairs else None
    direction["coverage"] = _r(coverage)
    direction["signal_rate"] = _r(float((oof["y_pred"] != "flat").mean()))

    # 交易模拟（模型 + 4 个基准，同一口径；净值只在实际评估区间上计算）
    sim_start = str(oof["trade_date"].min())
    sim_end = str(oof["trade_date"].max())
    signals = oof[["symbol", "trade_date", "y_pred"]].rename(columns={"y_pred": "direction"})
    model_sim = simulate_trades(
        daily, signals, cfg, symbols, horizon_days, sim_start, sim_end, initial_cash
    )
    win = compute_trade_metrics(model_sim, initial_cash)
    win["trades"]["skipped"] = int(sum(model_sim["skip_reasons"].values()))
    warnings.extend(model_sim["warnings"])

    trade_fold = dict(zip(oof["trade_date"] + "|" + oof["symbol"], oof["fold"]))
    fold_wins: dict[int, list[float]] = {}
    fold_returns: dict[int, list[float]] = {}
    for trade in model_sim["trades"]:
        key = f"{trade['signal_date']}|{trade['symbol']}"
        fold = trade_fold.get(key)
        if fold is not None:
            fold_wins.setdefault(int(fold), []).append(1.0 if trade["return_net"] > 0 else 0.0)
            fold_returns.setdefault(int(fold), []).append(trade["return_net"])
    for record in fold_records:
        fold = record["fold"]
        outcomes = fold_wins.get(fold, [])
        record["win_rate"] = _r(float(np.mean(outcomes))) if outcomes else None
        record["trades"] = int(len(outcomes))
        fold_pnl = [
            t["pnl"] for t in model_sim["trades"]
            if trade_fold.get(f"{t['signal_date']}|{t['symbol']}") == fold
        ]
        record["total_return"] = (
            _r(sum(fold_pnl) / initial_cash) if fold_pnl else None
        )

    # 基准策略方向与指标（majority 按 fold 训练窗多数类，禁止用测试期标签）
    ma5_map = _ma5_cross_directions(daily)
    majority_by_fold: dict[int, str] = {}
    for window in windows:
        train, _test = _train_test_frames(features, window)
        if len(train) > 0:
            majority_by_fold[window["fold"]] = _majority_label(train["label"].to_numpy())

    random_dirs = np.empty(len(oof), dtype=object)
    for fold in sorted(oof["fold"].unique()):
        mask = (oof["fold"] == fold).to_numpy()
        rng_fold = np.random.RandomState(random_seed + int(fold) * 17)
        random_dirs[mask] = rng_fold.choice(
            ["up", "flat", "down"], size=int(mask.sum()), p=[1 / 3, 1 / 3, 1 / 3]
        )

    benchmark_directions: dict[str, np.ndarray] = {
        "always_flat": np.array(["flat"] * len(oof), dtype=object),
        "majority": np.array(
            [majority_by_fold.get(int(f), "flat") for f in oof["fold"]], dtype=object
        ),
        "ma5_cross": np.array(
            [
                ma5_map.get(str(sym), {}).get(str(date), "flat")
                for sym, date in zip(oof["symbol"], oof["trade_date"])
            ],
            dtype=object,
        ),
        "random": random_dirs,
    }

    benchmarks: dict[str, dict] = {}
    comparison: list[dict] = []
    model_acc = direction["accuracy"]
    acc_lo, acc_hi = direction["accuracy_ci95"]
    for name in BENCHMARKS:
        bench_dir = benchmark_directions[name]
        bench_metrics = compute_direction_metrics(
            y_true, bench_dir, probs, oof["trade_date"].to_numpy(), LABEL_CLASSES
        )
        bench_signals = pd.DataFrame(
            {"symbol": oof["symbol"], "trade_date": oof["trade_date"], "direction": bench_dir}
        )
        bench_sim = simulate_trades(
            daily, bench_signals, cfg, symbols, horizon_days, sim_start, sim_end, initial_cash
        )
        bench_win = compute_trade_metrics(bench_sim, initial_cash)
        benchmarks[name] = {
            "accuracy": bench_metrics["accuracy"],
            "win_rate": bench_win["win_rate"],
            "total_return": bench_win["total_return"],
            "max_drawdown": bench_win["max_drawdown"],
            "sharpe": bench_win["sharpe"],
            "trades": bench_win["trades"],
            "sample_sufficient": bench_win["sample_sufficient"],
        }
        diff_pp = (model_acc - bench_metrics["accuracy"]) * 100 if model_acc is not None else None
        if bench_metrics["accuracy"] is not None and acc_lo is not None and acc_hi is not None:
            if bench_metrics["accuracy"] < acc_lo:
                verdict = "显著优于基准（基准在模型 95% CI 外）"
            elif bench_metrics["accuracy"] > acc_hi:
                verdict = "显著劣于基准（基准在模型 95% CI 外）"
            else:
                verdict = "差异不显著（基准落在模型 95% CI 内）"
        else:
            verdict = "n/a"
        comparison.append(
            {
                "benchmark": name,
                "accuracy": bench_metrics["accuracy"],
                "accuracy_diff_pp": _r(diff_pp, 3),
                "accuracy_relative": _r(
                    (model_acc - bench_metrics["accuracy"]) / bench_metrics["accuracy"]
                    if bench_metrics["accuracy"]
                    else None
                ),
                "win_rate": bench_win["win_rate"],
                "win_rate_diff": _r(
                    win["win_rate"] - bench_win["win_rate"]
                    if (win["win_rate"] is not None and bench_win["win_rate"] is not None)
                    else None
                ),
                "total_return": bench_win["total_return"],
                "total_return_diff": _r(
                    win["total_return"] - bench_win["total_return"]
                    if (win["total_return"] is not None and bench_win["total_return"] is not None)
                    else None
                ),
                "verdict": verdict,
            }
        )

    model_version, _model = _model_version(cfg)
    report_dir = resolve_report_dir(cfg)
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / "backtest.md"
    json_path = report_dir / "backtest.json"

    data_range = [str(daily["trade_date"].min()), str(daily["trade_date"].max())]
    result: dict = {
        "schema": 1,
        "generated_at": now_iso(),
        "meta": {
            "eval_range": [start, end],
            "evaluated_range": [
                str(oof["trade_date"].min()),
                str(oof["trade_date"].max()),
            ],
            "data_range": data_range,
            "symbols": symbols,
            "n_samples": int(len(oof)),
            "n_features": len(FEATURE_COLUMNS),
            "model_version": model_version,
            "random_seed": random_seed,
            "horizon": f"{horizon_days}d",
            "horizon_days": horizon_days,
            "label_threshold": label_threshold,
            "label_rule": label_rule_text(horizon_days, label_threshold),
            "train_years_cfg": train_years,
            "walk_forward_folds": fold_models,
            "walk_forward_splits_cfg": splits,
            "notional_per_symbol": _r(initial_cash / len(symbols), 2),
            "initial_cash": initial_cash,
            "db_path": str(resolve_db_path(cfg)),
            "report_md": str(md_path),
            "report_json": str(json_path),
            "disclaimer": "仅供研究学习，非投资建议；不接券商、不做实盘。",
        },
        "walk_forward": {
            "rule": (
                "测试窗=自然季度；训练窗=该测试窗开始前的全部历史（≥1 年，≥300 行）；"
                f"取最近 min(walk_forward_splits={splits}, 季度数) 个窗口；"
                "每个窗口重新训练模型，禁止用测试期数据训练或调参"
            ),
            "splits": fold_records,
        },
        "direction": direction,
        "win_rate": win,
        "benchmarks": {"comparison": comparison, **benchmarks},
        "warnings": warnings,
    }

    missing = _check_contract(_jsonable(result))
    if missing:
        raise RuntimeError(f"回测结果缺少契约 §12 字段: {missing}")

    _atomic_write_json(json_path, _jsonable(result))
    md_path.write_text(render_markdown(_jsonable(result)), encoding="utf-8")

    sync_metrics = {
        "accuracy": direction["accuracy"],
        "logloss": direction["logloss"],
        "brier": direction["brier"],
        "n": direction["n"],
        "coverage": direction["coverage"],
        "win_rate": win["win_rate"],
        "wilson95": win["wilson95"],
        "trades": win["trades"],
        "avg_return_per_trade": win["avg_return_per_trade"],
        "payoff_ratio": win["payoff_ratio"],
        "max_drawdown": win["max_drawdown"],
        "sharpe": win["sharpe"],
        "total_return": win["total_return"],
        "basis": "walk_forward_oos",
        "backtest_range": [start, end],
        "walk_forward_folds": fold_models,
        "random_seed": random_seed,
        "updated_at": now_iso(),
    }
    if not _sync_model_metrics(cfg, sync_metrics):
        warnings.append("model.json 不存在，本次回测指标未同步进 model.json（先跑 train 即可）")
        # 回写报告中已生成内容可能缺少该警告，重写一次保持 md/json 一致
        result["warnings"] = warnings
        _atomic_write_json(json_path, _jsonable(result))
        md_path.write_text(render_markdown(_jsonable(result)), encoding="utf-8")

    result["sync_metrics"] = sync_metrics
    summary = _jsonable(result)
    detail = summary["win_rate"].pop("trades_detail", [])
    summary["win_rate"]["trades_detail_count"] = len(detail)
    return summary
