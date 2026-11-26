#!/usr/bin/env python3
"""零成本训练管道 · 第 2 步：特征工程 + LightGBM 三分类 Walk-Forward

输入
----
``training/data/daily.parquet`` / ``zt_pool.parquet`` / ``index.parquet``（由 fetch_data.py 生成，
--synthetic 时使用内置合成数据）。

输出（默认 ``training/out/``）
-----------------------------
- ``model.txt``      LightGBM booster 文本模型（export_model.py 的输入）
- ``train_meta.json`` 特征顺序/类别顺序/标签口径/参数/特征统计/指标（导出与推理的元数据）
- ``metrics.json``    Walk-Forward 样本外指标 + 特征重要性
- ``oof.parquet``     样本外预测明细（导出裁剪重评估 / 复盘用）

防未来函数
----------
- 行 = **信号日 t**（收盘后）；个股特征只使用 ≤t 的 OHLCV；
- 市场/情绪特征（涨跌停池、股票池代理情绪）统一 **shift(1)** 后对齐到 t，比"收盘后可得"更保守；
- 标签 = **t+1 可成交收益**：``next_close / next_open - 1``（次日开盘买、收盘卖）；
- ``|label_ret| ≤ cost(0.2%)`` 记为 flat：只有跑赢双边摩擦的行情才算方向样本。

用法
----
    python training/train_lgbm.py
    python training/train_lgbm.py --train-days 250 --valid-days 20 --step 20 --num-iterations 120
    python training/train_lgbm.py --synthetic        # 离线合成数据跑通全流程
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

LOGGER = logging.getLogger("training.train_lgbm")
TRAINING_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = TRAINING_DIR / "data"
DEFAULT_OUT_DIR = TRAINING_DIR / "out"
TZ = timezone(timedelta(hours=8))

#: 训练用特征顺序（写进 model.json / feature_config.json，顺序不可随意改动）
FEATURE_COLUMNS: list[str] = [
    # 个股动量与超额收益（相对沪深300；缺失时相对股票池等权）
    "ret_1d",
    "ret_5d",
    "ret_20d",
    "excess_ret_5d",
    "excess_ret_10d",
    "excess_ret_20d",
    # 量能
    "amount_share",
    "amount_share_ma5",
    "turnover_rate",
    # K 线形态
    "upper_shadow",
    "lower_shadow",
    "body_ratio",
    "amplitude",
    "gap",
    # 均线乖离 / 波动
    "ma_bias_5",
    "ma_bias_20",
    "ma_bias_60",
    "volatility_5",
    "volatility_20",
    # 全市场涨跌停情绪温度计（lag1）
    "limit_up_count_lag1",
    "limit_down_count_lag1",
    "broken_rate_lag1",
    "max_consecutive_boards_lag1",
    "limit_up_ma5_lag1",
    "broken_rate_ma5_lag1",
    "sentiment_missing",
    # 股票池代理情绪（历史全量可用；lag1）
    "pool_limit_up_count_lag1",
    "pool_broken_rate_lag1",
    "pool_max_boards_lag1",
    # 市场基准动量（lag1）
    "bench_ret_5d_lag1",
]

FEATURE_GROUPS: dict[str, str] = {
    "ret_1d": "个股动量", "ret_5d": "个股动量", "ret_20d": "个股动量",
    "excess_ret_5d": "超额收益", "excess_ret_10d": "超额收益", "excess_ret_20d": "超额收益",
    "amount_share": "量能", "amount_share_ma5": "量能", "turnover_rate": "量能",
    "upper_shadow": "K线形态", "lower_shadow": "K线形态", "body_ratio": "K线形态",
    "amplitude": "K线形态", "gap": "K线形态",
    "ma_bias_5": "均线乖离", "ma_bias_20": "均线乖离", "ma_bias_60": "均线乖离",
    "volatility_5": "波动率", "volatility_20": "波动率",
    "limit_up_count_lag1": "涨跌停情绪", "limit_down_count_lag1": "涨跌停情绪",
    "broken_rate_lag1": "涨跌停情绪", "max_consecutive_boards_lag1": "涨跌停情绪",
    "limit_up_ma5_lag1": "涨跌停情绪", "broken_rate_ma5_lag1": "涨跌停情绪",
    "sentiment_missing": "涨跌停情绪",
    "pool_limit_up_count_lag1": "股票池代理情绪",
    "pool_broken_rate_lag1": "股票池代理情绪",
    "pool_max_boards_lag1": "股票池代理情绪",
    "bench_ret_5d_lag1": "市场基准",
}

FEATURE_FORMULAS: dict[str, str] = {
    "ret_1d": "close[t]/close[t-1]-1",
    "ret_5d": "close[t]/close[t-5]-1",
    "ret_20d": "close[t]/close[t-20]-1",
    "excess_ret_5d": "ret_5d - bench_ret_5d[t]（bench=沪深300，缺失时股票池等权）",
    "excess_ret_10d": "ret_10d - bench_ret_10d[t]",
    "excess_ret_20d": "ret_20d - bench_ret_20d[t]",
    "amount_share": "amount[t] / market_amount[t]（全市场成交额=上证+深证；缺失时股票池合计）",
    "amount_share_ma5": "amount_share 的 5 日均值",
    "turnover_rate": "换手率(%)（数据源提供；缺失填 0）",
    "upper_shadow": "(high[t]-max(open[t],close[t]))/pre_close[t]",
    "lower_shadow": "(min(open[t],close[t])-low[t])/pre_close[t]",
    "body_ratio": "(close[t]-open[t])/pre_close[t]",
    "amplitude": "(high[t]-low[t])/pre_close[t]",
    "gap": "open[t]/pre_close[t]-1",
    "ma_bias_5": "close[t]/MA5[t]-1",
    "ma_bias_20": "close[t]/MA20[t]-1",
    "ma_bias_60": "close[t]/MA60[t]-1",
    "volatility_5": "std(ret_1d, 5)",
    "volatility_20": "std(ret_1d, 20)",
    "limit_up_count_lag1": "全市场涨停家数[t-1]（AKShare 涨跌停池，缺失=0）",
    "limit_down_count_lag1": "全市场跌停家数[t-1]",
    "broken_rate_lag1": "炸板率[t-1]=炸板数/(炸板数+涨停数)",
    "max_consecutive_boards_lag1": "最高连板数[t-1]",
    "limit_up_ma5_lag1": "涨停家数 5 日均值[t-1]",
    "broken_rate_ma5_lag1": "炸板率 5 日均值[t-1]",
    "sentiment_missing": "该日之前涨跌停池数据是否缺失（1=缺失，用 0 填充）",
    "pool_limit_up_count_lag1": "股票池内收盘涨停家数[t-1]（历史代理，按板块涨跌停价判断）",
    "pool_broken_rate_lag1": "股票池内触板未封比例[t-1]",
    "pool_max_boards_lag1": "股票池内最高连板高度[t-1]",
    "bench_ret_5d_lag1": "基准 5 日收益[t-1]",
}

#: 模型/标签类别顺序（与 docs/UPGRADE_PLAN.md §3.3 一致）
SCHEMA_CLASSES: list[str] = ["up", "flat", "down"]
#: LightGBM 内部类别顺序（标签按字典序编码：down=0, flat=1, up=2）
BOOSTER_CLASSES: list[str] = ["down", "flat", "up"]
BOOSTER_CLASS_INDEX = {c: i for i, c in enumerate(BOOSTER_CLASSES)}

Z95 = 1.959963984540054


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float | None, float | None]:
    """Wilson 95% 置信区间。"""
    if n <= 0:
        return (None, None)
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _round_half_up(value: float, digits: int = 2) -> float:
    factor = 10**digits
    return math.floor(float(value) * factor + 0.5) / factor


def limit_pct_for(symbol: str) -> float:
    """按板块涨跌停比例（ST 无标记，按板块近似）。"""
    symbol = str(symbol).zfill(6)
    if symbol.startswith(("300", "301", "688")):
        return 0.20
    if symbol.startswith(("8", "4")):
        return 0.30
    return 0.10


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    out = a.astype(float) / b.astype(float).replace(0.0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan)


def load_parquet(path: Path, required: bool = False) -> pd.DataFrame:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"缺少数据文件：{path}（先运行 training/fetch_data.py）")
        LOGGER.warning("缺少数据文件 %s（相关特征将降级）", path)
        return pd.DataFrame()
    return pd.read_parquet(path)


# ---------------------------------------------------------------------------
# 特征工程（严格无未来函数）
# ---------------------------------------------------------------------------
def _benchmark_returns(index: pd.DataFrame, all_dates: list[str]) -> tuple[pd.DataFrame, str]:
    """基准收益：沪深300 优先，缺失/不可用时退回股票池等权。"""
    frame = pd.DataFrame(index=all_dates)
    source = "pool_equal_weight"
    if index is not None and len(index) > 0:
        idx = index.copy()
        idx["symbol"] = idx["symbol"].astype(str)
        bench = idx[idx["symbol"] == "000300"]
        if len(bench) >= max(30, int(0.5 * len(all_dates))):
            close = pd.Series(
                pd.to_numeric(bench["close"], errors="coerce").to_numpy(),
                index=bench["trade_date"].astype(str).to_numpy(),
            )
            close = close[~close.index.duplicated(keep="last")].sort_index()
            close = close.reindex(all_dates).ffill()
            for w in (1, 5, 10, 20):
                frame[f"bench_ret_{w}d"] = (close / close.shift(w) - 1.0).to_numpy()
            source = "000300"
    return frame, source


def _market_amount(index: pd.DataFrame, daily: pd.DataFrame) -> tuple[pd.Series, str]:
    """全市场成交额：上证指数+深证成指成交额之和；缺失时用股票池合计。"""
    pool_total = (
        daily.groupby("trade_date")["amount"].sum().rename("market_amount").sort_index()
    )
    source = "pool_total"
    if index is not None and len(index) > 0:
        idx = index.copy()
        idx["symbol"] = idx["symbol"].astype(str)
        sub = idx[idx["symbol"].isin(["000001", "399001"])]
        if "amount" in sub.columns and sub["amount"].notna().any():
            amount_sum = sub.groupby("trade_date")["amount"].sum()
            if amount_sum.notna().sum() >= max(30, int(0.5 * pool_total.shape[0])):
                source = "sh+sz_index"
                return amount_sum.sort_index(), source
    return pool_total, source


def _sentiment_frame(zt_pool: pd.DataFrame, all_dates: list[str]) -> pd.DataFrame:
    """涨跌停池情绪温度计：先按交易日 reindex，再 **shift(1)**，最后缺失填 0 + 缺失标记。"""
    frame = pd.DataFrame(index=pd.Index(all_dates, name="trade_date"))
    if zt_pool is not None and len(zt_pool) > 0:
        zt = zt_pool.copy()
        zt["trade_date"] = zt["trade_date"].astype(str)
        zt = zt.drop_duplicates(subset=["trade_date"], keep="last").set_index("trade_date")
        for col in ("limit_up_count", "limit_down_count", "broken_rate", "max_consecutive_boards"):
            frame[col] = pd.to_numeric(zt.get(col), errors="coerce").reindex(frame.index)
        frame["limit_up_ma5"] = frame["limit_up_count"].rolling(5, min_periods=1).mean()
        frame["broken_rate_ma5"] = frame["broken_rate"].rolling(5, min_periods=1).mean()
    for col in (
        "limit_up_count", "limit_down_count", "broken_rate", "max_consecutive_boards",
        "limit_up_ma5", "broken_rate_ma5",
    ):
        if col not in frame.columns:
            frame[col] = np.nan
    # 防未来函数：市场情绪统一后移一个交易日
    shifted = frame.shift(1)
    missing = shifted["limit_up_count"].isna().astype(int)
    out = pd.DataFrame(index=frame.index)
    out["limit_up_count_lag1"] = shifted["limit_up_count"].fillna(0.0)
    out["limit_down_count_lag1"] = shifted["limit_down_count"].fillna(0.0)
    out["broken_rate_lag1"] = shifted["broken_rate"].fillna(0.0)
    out["max_consecutive_boards_lag1"] = shifted["max_consecutive_boards"].fillna(0.0)
    out["limit_up_ma5_lag1"] = shifted["limit_up_ma5"].fillna(0.0)
    out["broken_rate_ma5_lag1"] = shifted["broken_rate_ma5"].fillna(0.0)
    out["sentiment_missing"] = missing.astype(float)
    return out


def _pool_sentiment_frame(daily: pd.DataFrame) -> pd.DataFrame:
    """股票池代理情绪（历史全量可用）：收盘涨停/跌停家数、触板未封率、最高连板。"""
    frame = daily.copy()
    frame["symbol"] = frame["symbol"].astype(str)
    # pre_close 缺失（每票首行）用当日 close 兜底，避免 NaN 进入涨跌停价计算
    pre_close = pd.to_numeric(frame["pre_close"], errors="coerce")
    frame["pre_close"] = pre_close.where(pre_close > 0, pd.to_numeric(frame["close"], errors="coerce"))
    pct = frame["symbol"].map(limit_pct_for)
    limit_up_price = np.array([_round_half_up(p * (1 + r), 2) for p, r in zip(frame["pre_close"], pct)])
    limit_down_price = np.array([_round_half_up(p * (1 - r), 2) for p, r in zip(frame["pre_close"], pct)])
    eps = 1e-6
    closed_up = frame["close"].to_numpy() >= limit_up_price - eps
    touched_up = frame["high"].to_numpy() >= limit_up_price - eps
    closed_down = frame["close"].to_numpy() <= limit_down_price + eps
    frame["_closed_up"] = closed_up.astype(int)
    frame["_closed_down"] = closed_down.astype(int)
    frame["_touched_not_closed"] = (touched_up & ~closed_up).astype(int)
    # 每个 symbol 的连续收盘涨停高度
    streaks: list[int] = []
    last_symbol: str | None = None
    streak = 0
    for sym, up in zip(frame["symbol"].to_numpy(), closed_up):
        if sym != last_symbol:
            streak, last_symbol = 0, sym
        streak = streak + 1 if up else 0
        streaks.append(streak)
    frame["_streak"] = streaks
    agg = frame.groupby("trade_date").agg(
        pool_limit_up_count=("_closed_up", "sum"),
        pool_limit_down_count=("_closed_down", "sum"),
        pool_touched_not_closed=("_touched_not_closed", "sum"),
        pool_max_boards=("_streak", "max"),
    )
    touched_total = agg["pool_limit_up_count"] + agg["pool_touched_not_closed"]
    agg["pool_broken_rate"] = _safe_div(agg["pool_touched_not_closed"], touched_total).fillna(0.0)
    agg = agg.reset_index().set_index("trade_date")
    return agg


def _symbol_features(group: pd.DataFrame) -> pd.DataFrame:
    """单票特征（只使用 ≤t 的 OHLCV）。"""
    g = group.sort_values("trade_date").reset_index(drop=True)
    close = pd.to_numeric(g["close"], errors="coerce")
    open_ = pd.to_numeric(g["open"], errors="coerce")
    high = pd.to_numeric(g["high"], errors="coerce")
    low = pd.to_numeric(g["low"], errors="coerce")
    pre_close = pd.to_numeric(g["pre_close"], errors="coerce")
    pre_close = pre_close.where(pre_close > 0, close.shift(1))
    amount = pd.to_numeric(g["amount"], errors="coerce")
    turnover = pd.to_numeric(g.get("turnover_rate"), errors="coerce").fillna(0.0)

    out = pd.DataFrame({"symbol": g["symbol"].astype(str), "trade_date": g["trade_date"].astype(str)})
    ret = close / close.shift(1) - 1.0
    out["ret_1d"] = ret
    for w in (5, 10, 20):
        out[f"ret_{w}d"] = close / close.shift(w) - 1.0
    out["upper_shadow"] = _safe_div(high - np.maximum(open_, close), pre_close)
    out["lower_shadow"] = _safe_div(np.minimum(open_, close) - low, pre_close)
    out["body_ratio"] = _safe_div(close - open_, pre_close)
    out["amplitude"] = _safe_div(high - low, pre_close)
    out["gap"] = _safe_div(open_ - pre_close, pre_close)
    for w in (5, 20, 60):
        out[f"ma_bias_{w}"] = _safe_div(close, close.rolling(w, min_periods=w).mean()) - 1.0
    out["volatility_5"] = ret.rolling(5, min_periods=3).std(ddof=1)
    out["volatility_20"] = ret.rolling(20, min_periods=10).std(ddof=1)
    out["turnover_rate"] = turnover
    out["_amount"] = amount
    out["_open"] = open_
    out["_close"] = close
    return out


def build_training_dataset(
    daily: pd.DataFrame,
    zt_pool: pd.DataFrame | None = None,
    index: pd.DataFrame | None = None,
    label_cost: float = 0.002,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """构造训练集：特征（无未来函数）+ T+1 可成交标签。返回 (df, meta)。"""
    if daily is None or len(daily) == 0:
        raise ValueError("daily 为空，无法构造训练集")
    daily = daily.copy()
    daily["symbol"] = daily["symbol"].astype(str).str.zfill(6)
    daily["trade_date"] = pd.to_datetime(daily["trade_date"]).dt.strftime("%Y-%m-%d")
    daily = daily.drop_duplicates(subset=["symbol", "trade_date"], keep="last").sort_values(
        ["symbol", "trade_date"]
    ).reset_index(drop=True)
    for col in ("open", "high", "low", "close", "pre_close", "amount"):
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    daily = daily.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    all_dates = sorted(daily["trade_date"].unique())

    # 个股特征
    parts = [_symbol_features(group) for _, group in daily.groupby("symbol", sort=True)]
    feats = pd.concat(parts, ignore_index=True)

    # 基准收益 / 市场成交额
    bench, benchmark_source = _benchmark_returns(index, all_dates)
    market_amount, market_amount_source = _market_amount(index, daily)
    # 股票池等权基准（指数缺失时使用）
    pool_ret: dict[int, pd.Series] = {}
    for w in (5, 10, 20):
        col = f"ret_{w}d"
        pool_ret[w] = feats.groupby("trade_date")[col].mean()
    for w in (5, 10, 20):
        pool_bench = pool_ret[w].rename(f"bench_ret_{w}d")
        if f"bench_ret_{w}d" in bench.columns:
            bench[f"bench_ret_{w}d"] = bench[f"bench_ret_{w}d"].fillna(pool_bench)
        else:
            bench[f"bench_ret_{w}d"] = pool_bench
        if f"bench_ret_{w}d" not in bench.columns or bench[f"bench_ret_{w}d"].isna().all():
            benchmark_source = "pool_equal_weight"
    bench = bench.reset_index().rename(columns={"index": "trade_date"})

    # 市场级特征（shift(1) 后的情绪 + 当日已知的个股成交额占比）
    sentiment = _sentiment_frame(zt_pool if zt_pool is not None else pd.DataFrame(), all_dates).reset_index()
    pool_sent = _pool_sentiment_frame(daily).reset_index()
    pool_sent["pool_limit_up_count_lag1"] = pool_sent["pool_limit_up_count"].shift(1).fillna(0.0)
    pool_sent["pool_broken_rate_lag1"] = pool_sent["pool_broken_rate"].shift(1).fillna(0.0)
    pool_sent["pool_max_boards_lag1"] = pool_sent["pool_max_boards"].shift(1).fillna(0.0)
    pool_sent = pool_sent[["trade_date", "pool_limit_up_count_lag1", "pool_broken_rate_lag1", "pool_max_boards_lag1"]]
    bench = bench.copy()
    for w in (5, 10, 20):
        bench[f"bench_ret_{w}d_lag1"] = bench[f"bench_ret_{w}d"].shift(1)

    feats = feats.merge(bench, on="trade_date", how="left")
    feats = feats.merge(sentiment, on="trade_date", how="left")
    feats = feats.merge(pool_sent, on="trade_date", how="left")

    for w in (5, 10, 20):
        feats[f"excess_ret_{w}d"] = feats[f"ret_{w}d"] - feats[f"bench_ret_{w}d"]

    # 成交额占比：amount / market_amount（同一交易日，收盘后即可知）
    feats["amount_share"] = feats["_amount"] / feats["trade_date"].map(market_amount).replace(0.0, np.nan)
    feats["amount_share_ma5"] = (
        feats.sort_values(["symbol", "trade_date"])
        .groupby("symbol")["amount_share"]
        .transform(lambda s: s.rolling(5, min_periods=1).mean())
    )
    feats["bench_ret_5d_lag1"] = feats["bench_ret_5d_lag1"].fillna(0.0)

    # 标签：T+1 可成交收益（次日开盘买、收盘卖），扣除双边摩擦后三分类
    feats = feats.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    next_open = feats.groupby("symbol")["_open"].shift(-1)
    next_close = feats.groupby("symbol")["_close"].shift(-1)
    feats["label_ret"] = next_close / next_open - 1.0
    feats["label"] = np.where(
        feats["label_ret"] > label_cost, "up",
        np.where(feats["label_ret"] < -label_cost, "down", "flat"),
    )
    feats.loc[feats["label_ret"].isna(), "label"] = None

    # 缺失特征填 0（训练/推理同口径；LightGBM 无需再处理 NaN）
    feats[FEATURE_COLUMNS] = feats[FEATURE_COLUMNS].replace([np.inf, -np.inf], np.nan).fillna(0.0)

    meta = {
        "benchmark": benchmark_source,
        "market_amount_source": market_amount_source,
        "label_definition": (
            f"r = close[t+1]/open[t+1]-1（次日开盘买、收盘卖）；r > {label_cost:.3%} → up；"
            f"r < -{label_cost:.3%} → down；否则 flat"
        ),
        "label_cost": float(label_cost),
        "n_rows": int(len(feats)),
        "n_symbols": int(feats["symbol"].nunique()),
        "date_range": [all_dates[0], all_dates[-1]] if all_dates else None,
        "n_dates": len(all_dates),
        "label_dist": {
            k: int(v) for k, v in feats["label"].value_counts(dropna=True).items()
        },
    }
    return feats, meta


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------
def evaluate_predictions(
    y_true: np.ndarray,
    proba: np.ndarray,
    label_ret: np.ndarray,
    cost: float = 0.002,
    classes: list[str] | None = None,
) -> dict[str, Any]:
    """三分类方向指标 + 交易口径胜率（与方向准确率分开）。"""
    classes = list(classes or SCHEMA_CLASSES)
    y_true = np.asarray(y_true).astype(str)
    proba = np.asarray(proba, dtype=float)
    label_ret = np.asarray(label_ret, dtype=float)
    n = len(y_true)
    if n == 0:
        raise ValueError("评估样本为空")
    cls_idx = {c: i for i, c in enumerate(classes)}
    pred_idx = np.argmax(proba, axis=1)
    pred = np.array([classes[i] for i in pred_idx])
    accuracy = float((pred == y_true).mean())

    eps = 1e-12
    true_p = np.array([proba[i, cls_idx.get(lab, 0)] for i, lab in enumerate(y_true)])
    logloss = float(-np.mean(np.log(np.clip(true_p, eps, 1.0))))
    brier = float(
        np.mean([
            np.sum((proba[i] - np.array([1.0 if c == y_true[i] else 0.0 for c in classes])) ** 2)
            for i in range(n)
        ])
    )

    per_class: dict[str, dict[str, Any]] = {}
    for c in classes:
        tp = int(((pred == c) & (y_true == c)).sum())
        fp = int(((pred == c) & (y_true != c)).sum())
        fn = int(((pred != c) & (y_true == c)).sum())
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision is not None and recall is not None and precision + recall > 0)
            else None
        )
        per_class[c] = {
            "precision": round(precision, 6) if precision is not None else None,
            "recall": round(recall, 6) if recall is not None else None,
            "f1": round(f1, 6) if f1 is not None else None,
            "support": int((y_true == c).sum()),
            "predicted": int((pred == c).sum()),
        }
    confusion = {"labels": classes, "matrix": [
        [int(((y_true == t) & (pred == p)).sum()) for p in classes] for t in classes
    ]}

    confidence = proba.max(axis=1)
    hit = (pred == y_true).astype(float)
    buckets = [("le_0.40", None, 0.40), ("0.40-0.50", 0.40, 0.50), ("0.50-0.60", 0.50, 0.60), ("gt_0.60", 0.60, None)]
    calibration = []
    for name, low, high in buckets:
        if low is None:
            mask = confidence <= high
        elif high is None:
            mask = confidence > low
        else:
            mask = (confidence > low) & (confidence <= high)
        cnt = int(mask.sum())
        calibration.append({
            "bucket": name,
            "n": cnt,
            "hit_rate": round(float(hit[mask].mean()), 6) if cnt else None,
            "avg_confidence": round(float(confidence[mask].mean()), 6) if cnt else None,
        })

    # 交易口径：up=做多、down=研究性做空；均为 T+1 开盘买/卖，扣 cost 双边摩擦
    trades = np.where(
        pred == "up", label_ret - cost,
        np.where(pred == "down", -label_ret - cost, np.nan),
    )
    traded = ~np.isnan(trades)
    n_trades = int(traded.sum())
    wins = int((trades[traded] > 0).sum()) if n_trades else 0
    win_rate = wins / n_trades if n_trades else None
    lo, hi = wilson_interval(wins, n_trades) if n_trades else (None, None)
    by_direction: dict[str, Any] = {}
    for side, name in (("up", "long"), ("down", "short")):
        mask = pred == side
        cnt = int(mask.sum())
        sub = trades[mask]
        w = int((sub > 0).sum()) if cnt else 0
        by_direction[name] = {
            "n": cnt,
            "win": w,
            "win_rate": round(w / cnt, 6) if cnt else None,
            "wilson95": [round(x, 6) for x in wilson_interval(w, cnt)] if cnt else [None, None],
            "avg_net_return": round(float(np.nanmean(sub)), 6) if cnt else None,
        }
    avg_net = float(np.nanmean(trades)) if n_trades else None
    win_returns = trades[traded & np.isfinite(trades)]
    pos = win_returns[win_returns > 0]
    neg = win_returns[win_returns < 0]
    payoff = float(pos.mean() / abs(neg.mean())) if len(pos) and len(neg) else None

    return {
        "accuracy": round(accuracy, 6),
        "n": n,
        "logloss": round(logloss, 6),
        "brier": round(brier, 6),
        "per_class": per_class,
        "confusion": confusion,
        "calibration": calibration,
        "win_rate": round(win_rate, 6) if win_rate is not None else None,
        "win_rate_n": n_trades,
        "wilson95": [round(x, 6) for x in (lo, hi)] if n_trades else [None, None],
        "win_rate_by_direction": by_direction,
        "avg_net_return": round(avg_net, 6) if avg_net is not None else None,
        "payoff_ratio": round(payoff, 6) if payoff is not None else None,
        "cost": float(cost),
        "pred_dist": {c: int((pred == c).sum()) for c in classes},
    }


# ---------------------------------------------------------------------------
# LightGBM 训练 / Walk-Forward
# ---------------------------------------------------------------------------
def default_params(
    num_iterations: int = 120,
    num_leaves: int = 15,
    learning_rate: float = 0.05,
    seed: int = 42,
    num_threads: int = 2,
) -> dict[str, Any]:
    """确定性参数（同一数据 + 同种子 → 同模型）。"""
    return {
        "objective": "multiclass",
        "num_class": 3,
        "metric": "multi_logloss",
        "boosting_type": "gbdt",
        "num_leaves": int(num_leaves),
        "learning_rate": float(learning_rate),
        "min_data_in_leaf": 40,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 1,
        "lambda_l2": 1.0,
        "seed": int(seed),
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": int(num_threads),
        "verbosity": -1,
    }


def _lgb():
    """延迟导入 lightgbm。"""
    import lightgbm as lgb  # noqa: PLC0415

    return lgb


def encode_labels(labels: pd.Series) -> np.ndarray:
    """标签 → LightGBM 内部编码（down=0, flat=1, up=2）。"""
    mapping = BOOSTER_CLASS_INDEX
    out = labels.map(mapping)
    if out.isna().any():
        bad = labels[out.isna()].unique()[:5]
        raise ValueError(f"未知标签：{bad}")
    return out.to_numpy(dtype=int)


def fit_booster(
    X: np.ndarray,
    y: np.ndarray,
    params: dict[str, Any],
    num_boost_round: int | None = None,
):
    """训练 LightGBM booster（低层 API，类别顺序固定 down/flat/up）。"""
    lgb = _lgb()
    dataset = lgb.Dataset(np.asarray(X, dtype=np.float32), label=np.asarray(y, dtype=int))
    rounds = int(num_boost_round if num_boost_round is not None else params.get("n_estimators", 120))
    return lgb.train(params, dataset, num_boost_round=rounds)


def predict_proba_booster(booster, X: np.ndarray) -> np.ndarray:
    """booster → 概率矩阵，列顺序重排为 ``SCHEMA_CLASSES``（up/flat/down）。"""
    raw = np.asarray(booster.predict(np.asarray(X, dtype=np.float32)), dtype=float)
    if raw.ndim == 1:
        raw = raw.reshape(1, -1)
    order = [BOOSTER_CLASS_INDEX[c] for c in SCHEMA_CLASSES]
    return raw[:, order]


def walk_forward(
    df: pd.DataFrame,
    feature_cols: list[str],
    params: dict[str, Any],
    train_days: int = 250,
    valid_days: int = 20,
    step: int = 20,
    label_cost: float = 0.002,
    num_boost_round: int | None = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """滚动 Walk-Forward：训练窗在验证窗之前，逐折重训，收集样本外预测。"""
    dates = sorted(df["trade_date"].unique())
    if len(dates) < train_days + valid_days:
        raise RuntimeError(
            f"数据只有 {len(dates)} 个交易日，不足 train_days({train_days})+valid_days({valid_days})"
        )
    oof_parts: list[pd.DataFrame] = []
    folds: list[dict[str, Any]] = []
    start = int(train_days)
    fold = 0
    while start + valid_days <= len(dates):
        train_dates = dates[start - train_days:start]
        valid_dates = dates[start:start + valid_days]
        train = df[df["trade_date"].isin(train_dates)]
        valid = df[df["trade_date"].isin(valid_dates)]
        if len(train) == 0 or len(valid) == 0:
            start += step
            continue
        if train["label"].nunique() < 3:
            LOGGER.warning("fold %d 训练窗类别不足，跳过", fold)
            start += step
            continue
        booster = fit_booster(
            train[feature_cols].to_numpy(dtype=np.float32),
            encode_labels(train["label"]),
            params,
            num_boost_round,
        )
        proba = predict_proba_booster(booster, valid[feature_cols].to_numpy(dtype=np.float32))
        fold_metrics = evaluate_predictions(
            valid["label"].to_numpy(), proba, valid["label_ret"].to_numpy(), cost=label_cost
        )
        part = valid[["symbol", "trade_date", "label", "label_ret", *feature_cols]].copy()
        for i, c in enumerate(SCHEMA_CLASSES):
            part[f"prob_{c}"] = proba[:, i]
        part["pred"] = [SCHEMA_CLASSES[j] for j in np.argmax(proba, axis=1)]
        part["fold"] = fold
        oof_parts.append(part)
        folds.append(
            {
                "fold": fold,
                "train_range": [train_dates[0], train_dates[-1]],
                "valid_range": [valid_dates[0], valid_dates[-1]],
                "n_train": int(len(train)),
                "n_valid": int(len(valid)),
                "accuracy": fold_metrics["accuracy"],
                "win_rate": fold_metrics["win_rate"],
                "win_rate_n": fold_metrics["win_rate_n"],
                "logloss": fold_metrics["logloss"],
            }
        )
        LOGGER.info(
            "fold %d: train %s~%s valid %s~%s acc=%.4f win_rate=%s",
            fold, train_dates[0], train_dates[-1], valid_dates[0], valid_dates[-1],
            fold_metrics["accuracy"], fold_metrics["win_rate"],
        )
        fold += 1
        start += step
    if not oof_parts:
        raise RuntimeError("Walk-Forward 没有产生任何有效折（数据太少或类别缺失）")
    return pd.concat(oof_parts, ignore_index=True), folds


def _feature_importance(booster, feature_cols: list[str]) -> list[dict[str, Any]]:
    gain = np.asarray(booster.feature_importance(importance_type="gain"), dtype=float)
    split = np.asarray(booster.feature_importance(importance_type="split"), dtype=float)
    total = gain.sum() if gain.sum() > 0 else 1.0
    order = np.argsort(-gain)
    return [
        {
            "name": feature_cols[i],
            "gain": round(float(gain[i]), 4),
            "gain_share": round(float(gain[i] / total), 6),
            "split": int(split[i]),
            "group": FEATURE_GROUPS.get(feature_cols[i], "其他"),
        }
        for i in order
    ]


def _feature_stats(df: pd.DataFrame, feature_cols: list[str]) -> dict[str, list[float]]:
    X = df[feature_cols].to_numpy(dtype=float)
    mean = X.mean(axis=0)
    std = X.std(axis=0, ddof=0)
    std = np.where(std == 0, 1.0, std)
    lower = np.quantile(X, 0.01, axis=0)
    upper = np.quantile(X, 0.99, axis=0)
    return {
        "mean": [round(float(x), 8) for x in mean],
        "std": [round(float(x), 8) for x in std],
        "winsor_lower": [round(float(x), 8) for x in lower],
        "winsor_upper": [round(float(x), 8) for x in upper],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="特征工程 + LightGBM 三分类 Walk-Forward（零成本训练管道第 2 步）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--daily", default=str(DEFAULT_DATA_DIR / "daily.parquet"))
    parser.add_argument("--zt-pool", default=str(DEFAULT_DATA_DIR / "zt_pool.parquet"))
    parser.add_argument("--index", default=str(DEFAULT_DATA_DIR / "index.parquet"))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--train-days", type=int, default=250, help="训练窗交易日数")
    parser.add_argument("--valid-days", type=int, default=20, help="验证窗交易日数")
    parser.add_argument("--step", type=int, default=20, help="滚动步长（交易日）")
    parser.add_argument("--num-iterations", type=int, default=120, help="LightGBM 迭代轮数（=导出树数/类）")
    parser.add_argument("--num-leaves", type=int, default=15, help="每棵树叶子数")
    parser.add_argument("--learning-rate", type=float, default=0.05)
    parser.add_argument("--label-cost", type=float, default=0.002, help="双边摩擦（0.002=0.2%）")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-threads", type=int, default=2)
    parser.add_argument("--synthetic", action="store_true", help="离线合成数据跑通全流程（不联网）")
    parser.add_argument("--synthetic-days", type=int, default=420)
    parser.add_argument("--synthetic-symbols", type=int, default=8)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def _load_frames(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    if args.synthetic:
        sys.path.insert(0, str(TRAINING_DIR))
        import fetch_data  # noqa: PLC0415

        frames = fetch_data.generate_synthetic_frames(
            fetch_data.DEFAULT_POOL[: max(int(args.synthetic_symbols), 1)],
            days=max(int(args.synthetic_days), 200),
            seed=int(args.seed),
        )
        return frames["daily"], frames["zt_pool"], frames["index"], {"mode": "synthetic"}
    daily = load_parquet(Path(args.daily), required=True)
    zt = load_parquet(Path(args.zt_pool), required=False)
    index = load_parquet(Path(args.index), required=False)
    return daily, zt, index, {"mode": "real"}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    daily, zt, index, mode_meta = _load_frames(args)
    LOGGER.info("daily rows=%d symbols=%d zt=%d index=%d", len(daily), daily["symbol"].nunique(), len(zt), len(index))

    df, data_meta = build_training_dataset(daily, zt, index, label_cost=args.label_cost)
    labeled = df.dropna(subset=["label"]).reset_index(drop=True)
    if len(labeled) == 0:
        raise RuntimeError("没有可训练样本（标签为空）")
    LOGGER.info("训练样本 %d（%s）", len(labeled), data_meta["label_dist"])

    params = default_params(
        num_iterations=args.num_iterations,
        num_leaves=args.num_leaves,
        learning_rate=args.learning_rate,
        seed=args.seed,
        num_threads=args.num_threads,
    )
    params["n_estimators"] = args.num_iterations

    oof, folds = walk_forward(
        labeled,
        FEATURE_COLUMNS,
        params,
        train_days=args.train_days,
        valid_days=args.valid_days,
        step=args.step,
        label_cost=args.label_cost,
        num_boost_round=args.num_iterations,
    )
    proba_cols = [f"prob_{c}" for c in SCHEMA_CLASSES]
    oof_metrics = evaluate_predictions(
        oof["label"].to_numpy(),
        oof[proba_cols].to_numpy(dtype=float),
        oof["label_ret"].to_numpy(),
        cost=args.label_cost,
    )

    # 最终模型：用全部有标签样本重训（线上推理用；指标仍为 Walk-Forward 样本外）
    booster = fit_booster(
        labeled[FEATURE_COLUMNS].to_numpy(dtype=np.float32),
        encode_labels(labeled["label"]),
        params,
        num_boost_round=args.num_iterations,
    )
    booster_path = out_dir / "model.txt"
    booster.save_model(str(booster_path))

    version = backtest_version = f"lgbm-{datetime.now(TZ).strftime('%Y-%m-%d')}"
    stats = _feature_stats(labeled, FEATURE_COLUMNS)
    importance = _feature_importance(booster, FEATURE_COLUMNS)
    metrics = {
        "walk_forward": oof_metrics,
        "folds": folds,
        "feature_importance": importance,
        "cost": float(args.label_cost),
    }
    train_meta = {
        "version": version,
        "trained_at": now_iso(),
        "features": FEATURE_COLUMNS,
        "feature_groups": FEATURE_GROUPS,
        "feature_formulas": FEATURE_FORMULAS,
        "booster_classes": BOOSTER_CLASSES,
        "schema_classes": SCHEMA_CLASSES,
        "label": {
            "horizon": 1,
            "definition": data_meta["label_definition"],
            "classes": SCHEMA_CLASSES,
            "cost": float(args.label_cost),
        },
        "params": params,
        "data": data_meta,
        "mode": mode_meta,
        "feature_stats": stats,
        "metrics": metrics,
        "booster_path": str(booster_path),
    }
    (out_dir / "train_meta.json").write_text(
        json.dumps(train_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    oof.to_parquet(out_dir / "oof.parquet", compression="snappy", index=False)

    summary = {
        "ok": True,
        "mode": mode_meta["mode"],
        "version": version,
        "booster": str(booster_path),
        "metrics": str(out_dir / "metrics.json"),
        "train_meta": str(out_dir / "train_meta.json"),
        "oof": str(out_dir / "oof.parquet"),
        "n_rows": int(len(labeled)),
        "n_symbols": data_meta["n_symbols"],
        "date_range": data_meta["date_range"],
        "benchmark": data_meta["benchmark"],
        "market_amount_source": data_meta["market_amount_source"],
        "label_dist": data_meta["label_dist"],
        "walk_forward": {
            "folds": len(folds),
            "n": oof_metrics["n"],
            "accuracy": oof_metrics["accuracy"],
            "logloss": oof_metrics["logloss"],
            "win_rate": oof_metrics["win_rate"],
            "win_rate_n": oof_metrics["win_rate_n"],
            "wilson95": oof_metrics["wilson95"],
        },
        "top_features": [f["name"] for f in importance[:8]],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
