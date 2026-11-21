"""特征工程（契约 §9 ``build_features``）。

设计原则
--------
- **严禁未来函数**：第 ``t`` 行的所有特征只使用 ``trade_date <= t`` 的行情；
  标签 ``label`` 使用 ``t + horizon_days`` 的收盘价，仅用于监督学习，不进入特征。
- 价格类特征用**前复权价** ``adj_close``（无 ``adj_close`` 时退回 ``close × adj_factor``
  或 ``close``），避免分红除权造成的假跳空；盘口/量能类用原始价量。
  标签口径：``r = adj_close[t+h] / adj_close[t] - 1``，与特征价口径一致。
- 缺失值保留 NaN（预热期 / 停牌无成交），由训练/回测侧 ``dropna`` 丢弃，不静默填 0。
- 多只股票输入时按 ``symbol`` 分组独立计算，组间不串数据。

输出列
------
``symbol``（输入带 symbol 时）、``trade_date``、29 个特征列、``label``
（``up`` / ``flat`` / ``down``，末尾 ``horizon_days`` 行为 NaN）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

#: 特征列顺序即 ``model.json`` 的 ``features`` 顺序（契约 §5），不要随意改动。
FEATURE_COLUMNS: list[str] = [
    # 多周期收益
    "ret_1d",
    "ret_3d",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "ret_60d",
    # 均线偏离
    "ma_dev_5",
    "ma_dev_10",
    "ma_dev_20",
    "ma_dev_60",
    # 动量指标
    "rsi_6",
    "rsi_14",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    # 波动率
    "vol_5",
    "vol_10",
    "vol_20",
    # 量能
    "volume_ratio_5",
    "volume_ratio_20",
    # 当日盘面
    "amplitude",
    "turnover_rate",
    "turnover_ma_5",
    "gap_open",
    "hl_range_ma5",
    # 位置分位 / 量价相关 / 上涨天数占比
    "close_position_20",
    "close_position_60",
    "corr_ret_volume_20",
    "up_days_5",
]

#: 标签类别顺序（与契约 §5 ``classes`` 一致）。
LABEL_CLASSES: list[str] = ["down", "flat", "up"]

_REQUIRED_COLUMNS = ("trade_date", "open", "high", "low", "close", "volume")


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    """除法，分母为 0 时返回 NaN（不产生 inf）。"""
    a = a.astype(float)
    b = b.astype(float)
    out = a / b.replace(0.0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan)


def _price_series(df: pd.DataFrame) -> pd.Series:
    """取前复权收盘价，缺失时退回 close（× adj_factor）。"""
    if "adj_close" in df.columns:
        adj = pd.to_numeric(df["adj_close"], errors="coerce")
    else:
        adj = pd.Series(np.nan, index=df.index, dtype=float)
    close = pd.to_numeric(df["close"], errors="coerce")
    if "adj_factor" in df.columns:
        factor = pd.to_numeric(df["adj_factor"], errors="coerce").fillna(1.0)
    else:
        factor = pd.Series(1.0, index=df.index, dtype=float)
    fallback = close * factor
    # adj_close 缺失（或为 0）的行用 close × adj_factor 兜底
    out = adj.where(adj.notna() & (adj > 0), fallback)
    return out.where(out.notna(), close)


def _rsi(close: pd.Series, window: int) -> pd.Series:
    """简单平均版 RSI（非 Wilder 平滑）：0~100，全涨=100，全跌=0。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.rolling(window, min_periods=window).mean()
    avg_loss = loss.rolling(window, min_periods=window).mean()
    rs = _safe_div(avg_gain, avg_loss)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    # 区间内没有下跌 → 100；完全无波动 → 50
    rsi = rsi.where(avg_loss.notna(), np.nan)
    flat = avg_loss.eq(0.0) & avg_gain.eq(0.0)
    only_up = avg_loss.eq(0.0) & avg_gain.gt(0.0)
    rsi = rsi.mask(only_up, 100.0).mask(flat, 50.0)
    return rsi


def _rolling_position(close: pd.Series, window: int) -> pd.Series:
    """收盘价在最近 window 日高低区间中的位置分位，0~1。"""
    roll_min = close.rolling(window, min_periods=window).min()
    roll_max = close.rolling(window, min_periods=window).max()
    return _safe_div(close - roll_min, roll_max - roll_min)


def _build_one_symbol(
    df: pd.DataFrame, horizon_days: int, label_threshold: float
) -> pd.DataFrame:
    df = df.sort_values("trade_date").reset_index(drop=True)
    close = _price_series(df)
    open_ = pd.to_numeric(df["open"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce").astype(float)
    if "pre_close" in df.columns:
        pre_close = pd.to_numeric(df["pre_close"], errors="coerce")
    else:
        pre_close = close.shift(1)
    pre_close = pre_close.where(pre_close.notna() & (pre_close > 0), close.shift(1))
    if "turnover_rate" in df.columns:
        turnover = pd.to_numeric(df["turnover_rate"], errors="coerce").fillna(0.0)
    else:
        turnover = pd.Series(0.0, index=df.index, dtype=float)

    ret_1d = close.pct_change(fill_method=None)
    feats: dict[str, pd.Series] = {}
    feats["ret_1d"] = ret_1d
    for window in (3, 5, 10, 20, 60):
        feats[f"ret_{window}d"] = close / close.shift(window) - 1.0

    for window in (5, 10, 20, 60):
        ma = close.rolling(window, min_periods=window).mean()
        feats[f"ma_dev_{window}"] = _safe_div(close, ma) - 1.0

    feats["rsi_6"] = _rsi(close, 6)
    feats["rsi_14"] = _rsi(close, 14)

    ema_fast = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema_slow = close.ewm(span=26, adjust=False, min_periods=26).mean()
    macd_dif = ema_fast - ema_slow
    macd_dea = macd_dif.ewm(span=9, adjust=False, min_periods=9).mean()
    feats["macd_dif"] = macd_dif
    feats["macd_dea"] = macd_dea
    feats["macd_hist"] = macd_dif - macd_dea

    for window in (5, 10, 20):
        feats[f"vol_{window}"] = ret_1d.rolling(window, min_periods=window).std(ddof=1)

    vol_ma_5 = volume.rolling(5, min_periods=5).mean()
    vol_ma_20 = volume.rolling(20, min_periods=20).mean()
    feats["volume_ratio_5"] = _safe_div(volume, vol_ma_5)
    feats["volume_ratio_20"] = _safe_div(volume, vol_ma_20)

    feats["amplitude"] = _safe_div(high - low, pre_close)
    feats["turnover_rate"] = turnover
    feats["turnover_ma_5"] = turnover.rolling(5, min_periods=5).mean()
    feats["gap_open"] = _safe_div(open_ - pre_close, pre_close)
    feats["hl_range_ma5"] = (
        _safe_div(high - low, close).rolling(5, min_periods=5).mean()
    )

    feats["close_position_20"] = _rolling_position(close, 20)
    feats["close_position_60"] = _rolling_position(close, 60)
    feats["corr_ret_volume_20"] = ret_1d.rolling(
        20, min_periods=20
    ).corr(volume)
    feats["up_days_5"] = (
        (ret_1d > 0).astype(float).rolling(5, min_periods=5).mean()
    )

    out = df[["trade_date"]].copy() if "trade_date" in df.columns else pd.DataFrame()
    out.index = df.index
    for name in FEATURE_COLUMNS:
        out[name] = feats[name].replace([np.inf, -np.inf], np.nan).astype(float)

    # 标签：T+horizon 日前复权收益（未来 1 天，仅供监督学习）
    fwd_ret = close.shift(-horizon_days) / close - 1.0
    label = pd.Series(np.nan, index=df.index, dtype=object)
    label[fwd_ret > label_threshold] = "up"
    label[fwd_ret < -label_threshold] = "down"
    label[(fwd_ret >= -label_threshold) & (fwd_ret <= label_threshold)] = "flat"
    label[fwd_ret.isna()] = None
    out["label"] = label
    return out


def build_features(
    daily: pd.DataFrame,
    horizon_days: int = 1,
    label_threshold: float = 0.005,
) -> pd.DataFrame:
    """构建特征 + T+1 方向标签。

    参数
    ----
    daily:
        日线 DataFrame，列见契约 §3.1；至少需要
        ``trade_date``、``open/high/low/close/volume``，可选 ``symbol`` /
        ``pre_close`` / ``adj_factor`` / ``adj_close`` / ``turnover_rate``。
        带 ``symbol`` 列且多只股票时按 symbol 分组独立计算。
    horizon_days:
        标签提前天数（默认 1，即 T+1）。
    label_threshold:
        ``|r| < label_threshold`` 记为 ``flat``（默认 0.5%）。

    返回
    ----
    DataFrame：``trade_date`` + 特征列 + ``label``；输入带 symbol 时保留 ``symbol`` 列。
    预热期不足的特征为 NaN，标签末尾 ``horizon_days`` 行为 None。
    """
    if horizon_days < 1:
        raise ValueError(f"horizon_days 必须 >= 1，得到 {horizon_days}")
    if label_threshold < 0:
        raise ValueError(f"label_threshold 必须 >= 0，得到 {label_threshold}")
    if daily is None or len(daily) == 0:
        columns = ["trade_date", *FEATURE_COLUMNS, "label"]
        return pd.DataFrame(columns=columns)

    missing = [c for c in _REQUIRED_COLUMNS if c not in daily.columns]
    if missing:
        raise KeyError(f"日线缺少必要列: {missing}")

    df = daily.copy()
    df["trade_date"] = df["trade_date"].astype(str)
    multi_symbol = "symbol" in df.columns and df["symbol"].nunique(dropna=True) > 1

    if multi_symbol:
        parts = []
        for symbol, group in df.groupby("symbol", sort=True, dropna=False):
            part = _build_one_symbol(group, horizon_days, label_threshold)
            part.insert(0, "symbol", str(symbol))
            parts.append(part)
        out = pd.concat(parts, ignore_index=True)
        out = out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        return out

    out = _build_one_symbol(df, horizon_days, label_threshold)
    if "symbol" in df.columns and df["symbol"].nunique(dropna=True) == 1:
        out.insert(0, "symbol", str(df["symbol"].iloc[0]))
    return out.sort_values("trade_date").reset_index(drop=True)


def feature_matrix(
    features: pd.DataFrame, feature_names: list[str] | None = None
) -> tuple[np.ndarray, list[str]]:
    """按给定特征名取矩阵（列顺序与 model.json 的 ``features`` 一致）。

    返回 ``(X, names)``；缺列直接抛 ``KeyError``，避免静默错位。
    """
    names = list(feature_names or FEATURE_COLUMNS)
    missing = [n for n in names if n not in features.columns]
    if missing:
        raise KeyError(f"特征表缺少列: {missing}")
    x = features[names].to_numpy(dtype=float)
    return x, names
