"""因子库与评估的离线单测（合成数据，不联网）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.factors.evaluation import evaluate_factor, mad_zscore
from quant.factors.library import FACTOR_NAMES, compute_factors


def _make_daily(n_days: int = 90, n_symbols: int = 30, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n_days, freq="B").strftime("%Y-%m-%d")
    rows: list[dict[str, object]] = []
    for idx in range(n_symbols):
        symbol = f"{600000 + idx:06d}"
        price = 10.0
        for trade_date in dates:
            ret = float(rng.normal(0, 0.02))
            price *= 1 + ret
            rows.append({
                "symbol": symbol,
                "trade_date": trade_date,
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "pre_close": price / (1 + ret),
                "volume": 1_000_000.0,
                "amount": 10_000_000.0,
                "turnover_rate": 1.0,
                "adj_close": price,
                "limit_up": price * 1.1,
                "limit_down": price * 0.9,
                "is_suspended": 0,
            })
    return pd.DataFrame(rows)


def test_mad_zscore_winsorizes_outliers():
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 100.0])
    z = mad_zscore(series)
    assert abs(float(z.mean())) < 1e-9
    assert abs(float(z.max())) < 5.0, "极端值应被 MAD 截断"


def test_compute_factors_has_all_columns():
    frame = compute_factors(_make_daily())
    for name in FACTOR_NAMES:
        assert name in frame.columns, f"缺因子列 {name}"
    assert "fwd_ret" in frame.columns
    assert len(frame) > 0


def test_compute_factors_no_lookahead():
    """截断未来数据后，历史因子值必须不变（无未来函数）。"""
    daily = _make_daily()
    full = compute_factors(daily)
    truncated = compute_factors(daily[daily["trade_date"] < daily["trade_date"].max()])
    cols = ["mom_5", "vol_20", "rsi_14", "close_pos_20"]
    merged = full[full["trade_date"] < daily["trade_date"].max()][["symbol", "trade_date"] + cols].merge(
        truncated[["symbol", "trade_date"] + cols], on=["symbol", "trade_date"], suffixes=("_a", "_b")
    )
    assert len(merged) > 0
    for col in cols:
        pd.testing.assert_series_equal(
            merged[f"{col}_a"], merged[f"{col}_b"], check_names=False
        )


def test_evaluate_factor_detects_constructed_relation():
    """人为构造因子与未来收益的强正相关，IC 必须显著为正。"""
    frame = compute_factors(_make_daily())
    rng = np.random.default_rng(1)
    frame["fwd_ret"] = 0.02 * frame["rev_1"].fillna(0.0) + rng.normal(0, 0.001, len(frame))
    clean = frame.dropna(subset=["rev_1", "fwd_ret"])
    result = evaluate_factor(clean, "rev_1", groups=3)
    assert result["days"] > 10
    assert result["ic"]["mean"] > 0.5, "强相关应给出高 IC"
    assert result["ic"]["t"] > 2


def test_evaluate_factor_random_has_low_ic():
    """随机因子 IC 接近 0。"""
    frame = compute_factors(_make_daily())
    rng = np.random.default_rng(3)
    frame["random_factor"] = rng.normal(0, 1, len(frame))
    clean = frame.dropna(subset=["random_factor", "fwd_ret"])
    result = evaluate_factor(clean, "random_factor", groups=3)
    assert abs(result["ic"]["mean"]) < 0.1
