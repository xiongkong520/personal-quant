"""因子库：价量因子（截面口径，P0）。

输入长表日线（symbol/trade_date/open/high/low/close/adj_close/volume/amount/
turnover_rate/limit_up/limit_down），输出每行 `symbol/trade_date` + 因子列 +
`fwd_ret`（未来 horizon 日收益，仅供评估）。

约定：
- 因子只用当日及历史数据（无未来函数）；
- 预处理（MAD 去极值、截面 z-score）在 evaluation 层进行；
- 行业/市值中性化留待 P2（需要行业与市值数据）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# 因子清单（名称 → 含义）
FACTOR_NAMES: list[str] = [
    "mom_5",          # 5 日动量
    "mom_10",         # 10 日动量
    "mom_20",         # 20 日动量
    "rev_1",          # 1 日反转（取负）
    "vol_20",         # 20 日波动率
    "ma_dev_20",      # 乖离率（20 日）
    "ma_dev_60",      # 乖离率（60 日）
    "rsi_14",         # RSI(14)
    "vol_ratio_5",    # 5 日量比
    "amount_ratio_20",# 20 日成交额比
    "range_20",       # 20 日平均振幅
    "close_pos_20",   # 收盘价在 20 日区间的位置
    "gap_1",          # 跳空
    "high_pos_1",     # 当日收盘在当日高低区间的位置
    "up_ratio_5",     # 5 日上涨天数占比
    "amihud_20",      # 流动性（|收益|/成交额，20 日均值）
    "turnover_20",    # 20 日平均换手率
    "limit_up_cnt_20",# 20 日涨停次数
]


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute_factors(daily: pd.DataFrame, horizon: int = 1) -> pd.DataFrame:
    """计算截面因子与未来收益。返回长表（symbol/trade_date/因子/fwd_ret）。"""
    df = daily.copy()
    for col in ("open", "high", "low", "close", "volume", "amount", "turnover_rate", "limit_up", "limit_down"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "adj_close" not in df.columns or df["adj_close"].isna().all():
        df["adj_close"] = df["close"]
    df = df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    parts: list[pd.DataFrame] = []
    for _, group in df.groupby("symbol", sort=False):
        g = group.copy()
        close = g["adj_close"].astype(float)
        ret1 = close.pct_change(1)
        volume = g["volume"].astype(float)
        amount = g["amount"].astype(float)

        g["mom_5"] = close.pct_change(5)
        g["mom_10"] = close.pct_change(10)
        g["mom_20"] = close.pct_change(20)
        g["rev_1"] = -ret1
        g["vol_20"] = ret1.rolling(20).std()
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()
        g["ma_dev_20"] = (close - ma20) / ma20
        g["ma_dev_60"] = (close - ma60) / ma60
        g["rsi_14"] = _rsi(close, 14) / 100.0
        g["vol_ratio_5"] = volume / volume.rolling(5).mean()
        g["amount_ratio_20"] = amount / amount.rolling(20).mean()
        g["range_20"] = ((g["high"] - g["low"]) / g["pre_close"].replace(0, np.nan)).rolling(20).mean()
        low20 = g["low"].rolling(20).min()
        high20 = g["high"].rolling(20).max()
        g["close_pos_20"] = (g["close"] - low20) / (high20 - low20).replace(0, np.nan)
        g["gap_1"] = g["open"] / g["pre_close"].replace(0, np.nan) - 1.0
        hi = g["high"] - g["low"]
        g["high_pos_1"] = (g["close"] - g["low"]) / hi.replace(0, np.nan)
        g["up_ratio_5"] = (ret1 > 0).astype(float).rolling(5).mean()
        g["amihud_20"] = ((ret1.abs() / amount.replace(0, np.nan)) * 1e9).rolling(20).mean()
        if "turnover_rate" in g.columns:
            g["turnover_20"] = g["turnover_rate"].rolling(20).mean()
        else:
            g["turnover_20"] = np.nan
        if "limit_up" in g.columns:
            near_limit = (g["close"] >= (g["limit_up"] - 0.011)).astype(float)
            g["limit_up_cnt_20"] = near_limit.rolling(20).sum()
        else:
            g["limit_up_cnt_20"] = np.nan

        # 未来收益（T+horizon 收盘 / T 收盘 - 1），只用于评估
        g["fwd_ret"] = close.shift(-horizon) / close - 1.0
        parts.append(g)

    out = pd.concat(parts, ignore_index=True)
    out = out.dropna(subset=["fwd_ret"])
    # 因子缺失保留 NaN（评估时按截面丢弃），但要求至少有价格
    return out
