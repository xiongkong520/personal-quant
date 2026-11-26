#!/usr/bin/env python3
"""零成本训练管道 · 第 1 步：数据采集（AKShare，多源兜底）

产出（默认 ``training/data/``，Parquet + snappy 压缩）
------------------------------------------------------
- ``daily.parquet``   个股日线：**前复权** OHLC、成交量（股）、成交额（元）、换手率（%）
- ``zt_pool.parquet`` 每日涨跌停池统计：涨停/跌停家数、炸板率、连板高度、封板资金、涨停主线行业
- ``index.parquet``   基准/市场指数日线（沪深300、上证指数、深证成指；用于超额收益与全市场成交额占比）

设计要点
--------
- **失败重试 + 限流降级**：个股日线主源东财 ``stock_zh_a_hist_em``；被限流/异常时依次
  降级到腾讯 ``stock_zh_a_hist_tx``、新浪 ``stock_zh_a_hist_sina``。指数同理。
- **幂等增量**：已存在的 parquet 按主键合并去重（``symbol,trade_date`` / ``trade_date``），
  每天在 GitHub Actions / Kaggle 重跑一次即可增量累积，不会重复浪费请求。
- **涨跌停池历史窗口限制（实测 AKShare 1.20.65）**：``stock_zt_pool_em`` 只返回最近
  约 10~30 个交易日的涨停池，``stock_zt_pool_dtgc_em`` / ``stock_zt_pool_zbgc_em``
  明确只支持最近 30 个交易日。因此本脚本默认只抓最近 ``--zt-days``（默认 30）个交易日，
  并支持历史累积；更早的情绪特征由训练脚本用**股票池内代理指标**补齐（见 README）。
- 只读公开数据、不联网训练；调用间隔默认 0.45s，避免触发东财限流。

用法
----
    python training/fetch_data.py --symbols 600519,000001 --start 2024-01-01
    python training/fetch_data.py                       # 默认 40 只池 + 近 3 年
    python training/fetch_data.py --synthetic           # 离线合成数据（无网络演示/测试）
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

LOGGER = logging.getLogger("training.fetch_data")

TRAINING_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = TRAINING_DIR / "data"
DAILY_FILE = "daily.parquet"
ZT_FILE = "zt_pool.parquet"
INDEX_FILE = "index.parquet"

#: 默认股票池（40 只，覆盖主板/创业板/科创板、消费/金融/新能源/科技/周期等行业）。
DEFAULT_POOL: list[str] = [
    "600519", "000001", "300750", "601318", "600036", "000858", "002594", "600030",
    "601012", "000333", "600276", "002415", "600887", "601899", "600900", "601166",
    "000651", "002475", "300059", "600309", "601088", "600048", "000725", "002714",
    "601398", "601288", "601988", "601857", "600028", "601668", "600050", "000063",
    "002230", "300124", "688981", "600585", "601919", "600438", "002027", "000568",
]

#: 基准/市场指数（code → 用途）
BENCHMARK_INDEX = "000300"       # 沪深300，超额收益基准
MARKET_INDEX_CODES = ("000001", "399001")  # 上证指数 + 深证成指，合计≈全市场成交额

STOCK_COLUMNS = [
    "symbol", "trade_date", "open", "high", "low", "close", "pre_close",
    "volume", "amount", "pct_chg", "turnover_rate", "source",
]
INDEX_COLUMNS = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "source"]
ZT_COLUMNS = [
    "trade_date", "limit_up_count", "limit_down_count", "broken_count", "broken_rate",
    "max_consecutive_boards", "limit_up_seal_amount", "top_industry",
    "top_industry_limit_up", "has_down_data", "has_broken_data",
]


class DataSourceError(RuntimeError):
    """所有数据源都不可用时抛出。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _ak():
    """延迟导入 akshare（离线导入不影响纯特征测试）。"""
    import akshare as ak  # noqa: PLC0415

    return ak


def round_half_up(value: float, digits: int = 2) -> float:
    """四舍五入到指定小数位（A 股报价规则，不用银行家舍入）。"""
    factor = 10**digits
    return math.floor(float(value) * factor + 0.5) / factor


def ak_symbol(code: str) -> str:
    """6 位代码 → 交易所前缀代码（sh/sz/bj），供新浪/腾讯接口使用。"""
    code = str(code).strip().zfill(6)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("0", "3", "1", "2")):
        return f"sz{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sh{code}"


def index_ak_symbol(code: str) -> str:
    """指数代码 → 交易所前缀代码（399xxx 为深证）。"""
    code = str(code).strip().zfill(6)
    return f"sz{code}" if code.startswith("399") else f"sh{code}"


def limit_pct_for(symbol: str) -> float:
    """按板块给出涨跌停比例（ST 无标记，按板块近似；教学/研究口径）。"""
    symbol = str(symbol).zfill(6)
    if symbol.startswith(("300", "301", "688")):
        return 0.20
    if symbol.startswith(("8", "4")):
        return 0.30
    return 0.10


def _compact(day: str) -> str:
    return pd.Timestamp(day).strftime("%Y%m%d")


def _trading_days(start: str, end: str) -> list[str]:
    """交易日历（新浪接口 + 工作日兜底）。"""
    try:
        cal = _ak().calendar_trade_date_hist_sina()
        dates = pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d")
        out = [d for d in dates if start <= d <= end]
        if out:
            return out
    except Exception as exc:  # pragma: no cover - 网络异常时兜底
        LOGGER.warning("交易日历获取失败，退回工作日：%s", exc)
    return [
        d.strftime("%Y-%m-%d")
        for d in pd.bdate_range(start, end)
        if start <= d.strftime("%Y-%m-%d") <= end
    ]


def retry_call(
    fn: Callable[..., Any],
    *args: Any,
    retries: int = 3,
    base_sleep: float = 1.5,
    label: str = "",
    **kwargs: Any,
) -> Any:
    """带指数退避 + 抖动的重试。"""
    last_exc: Exception | None = None
    for attempt in range(max(int(retries), 1)):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 多源兜底需要捕获所有源异常
            last_exc = exc
            wait = base_sleep * (2**attempt) + random.uniform(0, 0.4)
            LOGGER.warning(
                "%s 第 %d/%d 次失败：%s；%.1fs 后重试",
                label or getattr(fn, "__name__", "call"), attempt + 1, retries, exc, wait,
            )
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# 规范化：不同数据源 → 统一 schema
# ---------------------------------------------------------------------------
def _standardize_dates(df: pd.DataFrame, date_col: str = "date") -> pd.DataFrame:
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out[date_col]).dt.strftime("%Y-%m-%d")
    return out


def ingest_em_daily(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """东财 ``stock_zh_a_hist_em``（adjust=qfq）→ 统一 schema。

    - 成交量单位=手 → ×100 转股
    - ``pre_close = close - 涨跌额``（前复权序列的前收）
    """
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=STOCK_COLUMNS)
    df = _standardize_dates(raw, "日期")
    out = pd.DataFrame(
        {
            "symbol": str(symbol).zfill(6),
            "trade_date": df["trade_date"],
            "open": pd.to_numeric(df["开盘"], errors="coerce"),
            "high": pd.to_numeric(df["最高"], errors="coerce"),
            "low": pd.to_numeric(df["最低"], errors="coerce"),
            "close": pd.to_numeric(df["收盘"], errors="coerce"),
            "volume": pd.to_numeric(df["成交量"], errors="coerce").fillna(0) * 100,
            "amount": pd.to_numeric(df["成交额"], errors="coerce"),
            "pct_chg": pd.to_numeric(df["涨跌幅"], errors="coerce"),
            "turnover_rate": pd.to_numeric(df["换手率"], errors="coerce"),
            "source": "em",
        }
    )
    if "涨跌额" in df.columns:
        out["pre_close"] = out["close"] - pd.to_numeric(df["涨跌额"], errors="coerce")
    else:
        out["pre_close"] = out["close"].shift(1)
    out["volume"] = out["volume"].round().astype("int64")
    return _finalize_stock(out)


def ingest_sina_daily(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """新浪 ``stock_zh_a_hist_sina``（adjust=qfq）→ 统一 schema。

    - volume 已是股；turnover 是换手率小数 → ×100 转 %
    - 无涨跌额：pre_close = close.shift(1)（同一复权序列，连续）
    """
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=STOCK_COLUMNS)
    df = _standardize_dates(raw, "date")
    out = pd.DataFrame(
        {
            "symbol": str(symbol).zfill(6),
            "trade_date": df["trade_date"],
            "open": pd.to_numeric(df["open"], errors="coerce"),
            "high": pd.to_numeric(df["high"], errors="coerce"),
            "low": pd.to_numeric(df["low"], errors="coerce"),
            "close": pd.to_numeric(df["close"], errors="coerce"),
            "volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0).round(),
            "amount": pd.to_numeric(df["amount"], errors="coerce"),
            "turnover_rate": pd.to_numeric(df.get("turnover"), errors="coerce") * 100.0,
            "source": "sina",
        }
    )
    out["pre_close"] = out["close"].shift(1)
    out["pct_chg"] = (out["close"] / out["pre_close"] - 1.0) * 100.0
    out["volume"] = out["volume"].astype("int64")
    return _finalize_stock(out)


def ingest_tx_daily(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """腾讯 ``stock_zh_a_hist_tx``（adjust=qfq）→ 统一 schema。volume 股、turnover 小数。"""
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=STOCK_COLUMNS)
    df = _standardize_dates(raw, "date")
    out = pd.DataFrame(
        {
            "symbol": str(symbol).zfill(6),
            "trade_date": df["trade_date"],
            "open": pd.to_numeric(df["open"], errors="coerce"),
            "high": pd.to_numeric(df["high"], errors="coerce"),
            "low": pd.to_numeric(df["low"], errors="coerce"),
            "close": pd.to_numeric(df["close"], errors="coerce"),
            "volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0).round(),
            "amount": pd.to_numeric(df.get("amount"), errors="coerce"),
            "turnover_rate": pd.to_numeric(df.get("turnover"), errors="coerce") * 100.0,
            "source": "tx",
        }
    )
    out["pre_close"] = out["close"].shift(1)
    out["pct_chg"] = (out["close"] / out["pre_close"] - 1.0) * 100.0
    out["volume"] = out["volume"].astype("int64")
    return _finalize_stock(out)


def _finalize_stock(df: pd.DataFrame) -> pd.DataFrame:
    """清洗 + 排序：去重、去空、日期升序，列顺序固定。"""
    out = df.copy()
    for col in ("open", "high", "low", "close", "pre_close", "amount", "pct_chg", "turnover_rate"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    # 交叉校验/修正价格关系（极端脏数据截断）
    out["high"] = out[["high", "open", "close"]].max(axis=1)
    out["low"] = out[["low", "open", "close"]].min(axis=1)
    out = out.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    out = out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    return out[STOCK_COLUMNS]


def ingest_index(raw: pd.DataFrame, code: str, source: str) -> pd.DataFrame:
    """指数日线 → ``INDEX_COLUMNS``。"""
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=INDEX_COLUMNS)
    date_col = "日期" if "日期" in raw.columns else "date"
    df = _standardize_dates(raw, date_col)
    volume_col = "成交量" if "成交量" in raw.columns else "volume"
    amount_col = "成交额" if "成交额" in raw.columns else "amount"
    out = pd.DataFrame(
        {
            "symbol": str(code).zfill(6),
            "trade_date": df["trade_date"],
            "open": pd.to_numeric(df.get("开盘", df.get("open")), errors="coerce"),
            "high": pd.to_numeric(df.get("最高", df.get("high")), errors="coerce"),
            "low": pd.to_numeric(df.get("最低", df.get("low")), errors="coerce"),
            "close": pd.to_numeric(df.get("收盘", df.get("close")), errors="coerce"),
            "volume": pd.to_numeric(df.get(volume_col), errors="coerce"),
            "amount": pd.to_numeric(df.get(amount_col), errors="coerce") if amount_col in df.columns else np.nan,
            "source": source,
        }
    )
    out = out.dropna(subset=["close"]).drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    return out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)[INDEX_COLUMNS]


# ---------------------------------------------------------------------------
# 数据源抓取（多源兜底）
# ---------------------------------------------------------------------------
def fetch_stock_daily(
    symbol: str, start: str, end: str, retries: int = 3, sleep: float = 0.45
) -> pd.DataFrame:
    """个股日线：东财 → 腾讯 → 新浪 依次尝试，全部失败抛 DataSourceError。"""
    ak = _ak()
    start_c, end_c = _compact(start), _compact(end)
    errors: list[str] = []

    sources: list[tuple[str, Callable[[], pd.DataFrame]]] = []
    if hasattr(ak, "stock_zh_a_hist_em"):
        sources.append(
            ("em", lambda: ak.stock_zh_a_hist_em(
                symbol=str(symbol).zfill(6), period="daily",
                start_date=start_c, end_date=end_c, adjust="qfq",
            ))
        )
    if hasattr(ak, "stock_zh_a_hist_tx"):
        sources.append(
            ("tx", lambda: ak.stock_zh_a_hist_tx(
                symbol=ak_symbol(symbol), start_date=start_c,
                end_date=end_c, adjust="qfq",
            ))
        )
    if hasattr(ak, "stock_zh_a_hist_sina"):
        sources.append(
            ("sina", lambda: ak.stock_zh_a_hist_sina(
                symbol=ak_symbol(symbol), period="daily",
                start_date=start_c, end_date=end_c, adjust="qfq",
            ))
        )

    ingest = {"em": ingest_em_daily, "tx": ingest_tx_daily, "sina": ingest_sina_daily}
    for name, call in sources:
        try:
            raw = retry_call(call, retries=retries, label=f"{symbol}/{name}")
            frame = ingest[name](raw, symbol)
            if len(frame) > 0:
                # 统一裁到 [start, end]（部分接口忽略日期参数）
                frame = frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)]
                if len(frame) > 0:
                    LOGGER.info("fetch %s <- %s rows=%d", symbol, name, len(frame))
                    time.sleep(sleep)
                    return frame.reset_index(drop=True)
            errors.append(f"{name}: empty")
        except Exception as exc:  # noqa: BLE001 - 继续下一个数据源
            errors.append(f"{name}: {exc}")
            LOGGER.warning("fetch %s via %s failed: %s", symbol, name, exc)
        time.sleep(sleep)
    raise DataSourceError(f"{symbol} 所有数据源失败：{errors}")


def fetch_index_daily(
    code: str, start: str, end: str, retries: int = 3, sleep: float = 0.45
) -> pd.DataFrame:
    """指数日线：东财 → 新浪 依次尝试；全部失败返回空表（训练自动降级）。"""
    ak = _ak()
    start_c, end_c = _compact(start), _compact(end)
    candidates: list[tuple[str, Callable[[], pd.DataFrame]]] = []
    if hasattr(ak, "index_zh_hist_em"):
        candidates.append(
            ("em", lambda: ak.index_zh_hist_em(
                symbol=str(code).zfill(6), period="daily",
                start_date=start_c, end_date=end_c,
            ))
        )
    if hasattr(ak, "index_zh_daily_sina"):
        candidates.append(
            ("sina", lambda: ak.index_zh_daily_sina(symbol=index_ak_symbol(code)))
        )
    errors: list[str] = []
    for name, call in candidates:
        try:
            raw = retry_call(call, retries=retries, label=f"index{code}/{name}")
            frame = ingest_index(raw, code, name)
            frame = frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)]
            if len(frame) > 0:
                LOGGER.info("fetch index %s <- %s rows=%d", code, name, len(frame))
                time.sleep(sleep)
                return frame.reset_index(drop=True)
            errors.append(f"{name}: empty")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            LOGGER.warning("index %s via %s failed: %s", code, name, exc)
        time.sleep(sleep)
    LOGGER.warning("index %s 全部数据源失败（训练将使用股票池等权基准）：%s", code, errors)
    return pd.DataFrame(columns=INDEX_COLUMNS)


def _zt_endpoint(ak: Any, name: str, day: str) -> pd.DataFrame:
    """统一调用涨跌停池接口；不支持的历史日期返回空表。"""
    fn = getattr(ak, name, None)
    if fn is None:
        return pd.DataFrame()
    try:
        return fn(date=_compact(day))
    except Exception as exc:  # noqa: BLE001 - 历史窗口外/网络异常
        LOGGER.debug("%s(%s) 不可用：%s", name, day, exc)
        return pd.DataFrame()


def compute_zt_stats(
    zt: pd.DataFrame, dt: pd.DataFrame, zb: pd.DataFrame, day: str
) -> dict[str, Any]:
    """由涨停池/跌停池/炸板池三张原始表计算当日市场情绪统计。"""
    limit_up = 0 if zt is None else int(len(zt))
    limit_down = 0 if dt is None else int(len(dt))
    broken = 0 if zb is None else int(len(zb))
    denom = limit_up + broken
    broken_rate = (broken / denom) if denom > 0 else np.nan
    max_boards = 0
    if zt is not None and len(zt) > 0:
        for col in ("连板数", "涨停统计"):
            if col in zt.columns:
                if col == "连板数":
                    boards = pd.to_numeric(zt[col], errors="coerce")
                else:  # "涨停统计" 形如 "4/4" 或 "2天2板"
                    boards = pd.to_numeric(
                        zt[col].astype(str).str.extract(r"^(\d+)")[0], errors="coerce"
                    )
                max_boards = int(np.nanmax(boards.to_numpy(dtype=float))) if boards.notna().any() else 0
                break
    seal_amount = 0.0
    if zt is not None and "封板资金" in zt.columns:
        seal_amount = float(pd.to_numeric(zt["封板资金"], errors="coerce").fillna(0).sum())
    top_industry, top_industry_count = "", 0
    if zt is not None and "所属行业" in zt.columns and len(zt) > 0:
        counts = zt["所属行业"].astype(str).value_counts()
        if len(counts) > 0:
            top_industry = str(counts.index[0])
            top_industry_count = int(counts.iloc[0])
    return {
        "trade_date": day,
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "broken_count": broken,
        "broken_rate": float(broken_rate) if not pd.isna(broken_rate) else np.nan,
        "max_consecutive_boards": max_boards,
        "limit_up_seal_amount": seal_amount,
        "top_industry": top_industry,
        "top_industry_limit_up": top_industry_count,
        "has_down_data": int(dt is not None and len(dt) >= 0 and not (isinstance(dt, pd.DataFrame) and dt.empty and limit_up > 0)),
        "has_broken_data": int(zb is not None and len(zb) >= 0),
    }


def fetch_zt_pool_day(day: str) -> dict[str, Any] | None:
    """抓取单日涨跌停池统计；该日接口不可用（历史窗口外/非交易日）返回 None。"""
    ak = _ak()
    zt = _zt_endpoint(ak, "stock_zt_pool_em", day)
    if zt.empty:
        return None  # 涨停池不可用或当日无涨停（实测历史日期返回空，视为不可用）
    dt = _zt_endpoint(ak, "stock_zt_pool_dtgc_em", day)
    zb = _zt_endpoint(ak, "stock_zt_pool_zbgc_em", day)
    stats = compute_zt_stats(zt, dt, zb, day)
    stats["has_down_data"] = int(len(dt) >= 0 and not dt.empty)
    stats["has_broken_data"] = int(len(zb) >= 0 and not zb.empty)
    return stats


# ---------------------------------------------------------------------------
# Parquet 增量合并
# ---------------------------------------------------------------------------
def merge_parquet(path: Path, new_df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """读取旧 parquet + 新数据，按主键去重（新覆盖旧）后原子写回。"""
    if new_df is None or len(new_df) == 0:
        if path.exists():
            return pd.read_parquet(path)
        return pd.DataFrame()
    old = pd.read_parquet(path) if path.exists() else None
    if old is not None and len(old) > 0:
        # 统一列：旧文件可能缺新列
        for col in new_df.columns:
            if col not in old.columns:
                old[col] = np.nan
        for col in old.columns:
            if col not in new_df.columns:
                new_df = new_df.copy()
                new_df[col] = np.nan
        combined = pd.concat([old, new_df], ignore_index=True)
    else:
        combined = new_df.copy()
    combined = combined.drop_duplicates(subset=keys, keep="last")
    sort_keys = [k for k in keys if k in combined.columns]
    combined = combined.sort_values(sort_keys).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    combined.to_parquet(tmp, compression="snappy", index=False)
    tmp.replace(path)
    return combined


# ---------------------------------------------------------------------------
# 离线合成数据（无网络演示 / 测试用；训练默认仍用真实数据）
# ---------------------------------------------------------------------------
def generate_synthetic_frames(
    symbols: Iterable[str] | None = None,
    days: int = 420,
    seed: int = 42,
    start: str | None = None,
) -> dict[str, pd.DataFrame]:
    """生成与真实 schema 完全一致的合成日线/指数/涨跌停池（离线用）。"""
    symbols = list(symbols or DEFAULT_POOL[:5])
    rng = np.random.default_rng(seed)
    if start:
        dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=days)]
    else:
        end = date.today()
        dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(end, periods=days)]
    market_ret = rng.normal(0.0003, 0.010, len(dates))
    daily_frames, index_frames = [], []
    for k, symbol in enumerate(symbols):
        base = 10.0 + 20.0 * (k % 5)
        eps = rng.normal(0.0, 0.014, len(dates))
        ret = 0.25 * np.roll(eps, 1) + eps * 0.8 + market_ret * 0.9
        ret[0] = 0.0
        close = base * np.exp(np.cumsum(ret))
        pre_close = np.concatenate([[close[0] / (1 + ret[0])], close[:-1]])
        gap = rng.normal(0, 0.004, len(dates))
        open_ = pre_close * (1 + gap)
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, len(dates))))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, len(dates))))
        volume = (rng.lognormal(14.0, 0.4, len(dates))).astype("int64")
        amount = volume * close
        frame = pd.DataFrame(
            {
                "symbol": str(symbol).zfill(6),
                "trade_date": dates,
                "open": np.round(open_, 2),
                "high": np.round(high, 2),
                "low": np.round(low, 2),
                "close": np.round(close, 2),
                "pre_close": np.round(pre_close, 2),
                "volume": volume,
                "amount": np.round(amount, 2),
                "pct_chg": np.round((close / pre_close - 1) * 100, 4),
                "turnover_rate": np.round(rng.uniform(0.2, 5.0, len(dates)), 4),
                "source": "synthetic",
            }
        )
        daily_frames.append(frame)
    daily = pd.concat(daily_frames, ignore_index=True)
    for code in (BENCHMARK_INDEX, *MARKET_INDEX_CODES):
        base = 3000.0
        idx_close = base * np.exp(np.cumsum(market_ret))
        index_frames.append(
            pd.DataFrame(
                {
                    "symbol": code,
                    "trade_date": dates,
                    "open": np.round(idx_close * (1 + rng.normal(0, 0.002, len(dates))), 3),
                    "high": np.round(idx_close * (1 + np.abs(rng.normal(0, 0.004, len(dates)))), 3),
                    "low": np.round(idx_close * (1 - np.abs(rng.normal(0, 0.004, len(dates)))), 3),
                    "close": np.round(idx_close, 3),
                    "volume": rng.integers(1e8, 5e8, len(dates)).astype("int64"),
                    "amount": np.round(rng.uniform(2e11, 8e11, len(dates)), 2),
                    "source": "synthetic",
                }
            )
        )
    index = pd.concat(index_frames, ignore_index=True)
    # 合成情绪：涨停数随市场收益放大，跌停数反向；只有最近 30 天"真实可用"，其余缺失
    zt_rows = []
    for i, d in enumerate(dates):
        recent = i >= len(dates) - 30
        if not recent:
            continue
        up = int(max(1, round(40 + 800 * market_ret[i] + rng.uniform(-15, 15))))
        down = int(max(0, round(8 - 600 * market_ret[i] + rng.uniform(-4, 6))))
        broken = int(max(0, round(up * rng.uniform(0.1, 0.35))))
        zt_rows.append(
            {
                "trade_date": d,
                "limit_up_count": up,
                "limit_down_count": down,
                "broken_count": broken,
                "broken_rate": broken / (up + broken) if (up + broken) else np.nan,
                "max_consecutive_boards": int(max(1, round(rng.uniform(1, 7)))),
                "limit_up_seal_amount": float(rng.uniform(1e9, 2e10)),
                "top_industry": "合成行业",
                "top_industry_limit_up": int(max(1, round(up * rng.uniform(0.1, 0.3)))),
                "has_down_data": 1,
                "has_broken_data": 1,
            }
        )
    zt = pd.DataFrame(zt_rows, columns=ZT_COLUMNS)
    return {"daily": daily[STOCK_COLUMNS], "index": index[INDEX_COLUMNS], "zt_pool": zt}


def write_synthetic_data(out_dir: Path, args: argparse.Namespace) -> dict[str, int]:
    """生成合成 parquet（离线）。"""
    symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or DEFAULT_POOL[:5]
    days = max(int(args.synthetic_days), 120)
    frames = generate_synthetic_frames(symbols, days=days, seed=args.seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, filename, keys in (
        ("daily", DAILY_FILE, ["symbol", "trade_date"]),
        ("index", INDEX_FILE, ["symbol", "trade_date"]),
        ("zt_pool", ZT_FILE, ["trade_date"]),
    ):
        frame = frames[name]
        merge_parquet(out_dir / filename, frame, keys)
    return {
        "daily_rows": len(frames["daily"]),
        "index_rows": len(frames["index"]),
        "zt_rows": len(frames["zt_pool"]),
        "symbols": len(symbols),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    end_default = date.today().strftime("%Y-%m-%d")
    start_default = (date.today() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(
        description="AKShare 训练数据采集（近 3 年前复权日线 + 涨跌停池 → Parquet）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--symbols", default=None, help="逗号分隔股票代码；缺省用内置 40 只池")
    parser.add_argument("--start", default=start_default, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=end_default, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--out-dir", default=str(DEFAULT_DATA_DIR), help="输出目录")
    parser.add_argument("--retries", type=int, default=3, help="单接口重试次数")
    parser.add_argument("--sleep", type=float, default=0.45, help="接口间隔秒数（避免限流）")
    parser.add_argument("--zt-days", type=int, default=30, help="涨跌停池抓取最近 N 个交易日（0=全部；接口仅支持近 30 日）")
    parser.add_argument("--skip-zt", action="store_true", help="跳过涨跌停池")
    parser.add_argument("--skip-index", action="store_true", help="跳过指数基准")
    parser.add_argument("--synthetic", action="store_true", help="离线合成数据（不联网）")
    parser.add_argument("--synthetic-days", type=int, default=420, help="--synthetic 生成的交易日数")
    parser.add_argument("--seed", type=int, default=42, help="合成数据随机种子")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.synthetic:
        summary = write_synthetic_data(out_dir, args)
        print(json.dumps({"ok": True, "synthetic": True, "out_dir": str(out_dir), **summary}, ensure_ascii=False))
        return 0

    symbols = [s.strip().zfill(6) for s in (args.symbols or "").split(",") if s.strip()]
    if not symbols:
        symbols = list(DEFAULT_POOL)
    start, end = str(args.start), str(args.end)
    summary: dict[str, Any] = {
        "ok": False, "start": start, "end": end, "out_dir": str(out_dir),
        "symbols_requested": len(symbols), "errors": [],
    }

    # 1) 个股日线（增量合并）
    frames: list[pd.DataFrame] = []
    for i, symbol in enumerate(symbols, 1):
        try:
            frame = fetch_stock_daily(symbol, start, end, retries=args.retries, sleep=args.sleep)
            frames.append(frame)
            summary.setdefault("symbols_ok", []).append(symbol)
        except Exception as exc:  # noqa: BLE001 - 单票失败不中断整体
            summary["errors"].append({"symbol": symbol, "error": str(exc)[:300]})
            LOGGER.error("个股 %s 抓取失败：%s", symbol, exc)
        LOGGER.info("进度 %d/%d：%s", i, len(symbols), symbol)
    if frames:
        daily = merge_parquet(out_dir / DAILY_FILE, pd.concat(frames, ignore_index=True), ["symbol", "trade_date"])
        summary["daily_rows"] = int(len(daily))
        summary["symbols_in_file"] = int(daily["symbol"].nunique()) if len(daily) else 0
        summary["daily_range"] = [str(daily["trade_date"].min()), str(daily["trade_date"].max())] if len(daily) else None
        summary["daily_sources"] = (
            daily.groupby("source")["symbol"].nunique().to_dict() if "source" in daily.columns else {}
        )
    else:
        summary["daily_rows"] = 0

    # 2) 指数基准（失败降级，不阻塞）
    index_frames: list[pd.DataFrame] = []
    if not args.skip_index:
        for code in (BENCHMARK_INDEX, *MARKET_INDEX_CODES):
            frame = fetch_index_daily(code, start, end, retries=args.retries, sleep=args.sleep)
            if len(frame) > 0:
                index_frames.append(frame)
            time.sleep(args.sleep)
        if index_frames:
            index = merge_parquet(out_dir / INDEX_FILE, pd.concat(index_frames, ignore_index=True), ["symbol", "trade_date"])
            summary["index_rows"] = int(len(index))
            summary["index_codes"] = sorted(index["symbol"].unique().tolist()) if len(index) else []
        else:
            summary["index_rows"] = 0
            summary["errors"].append({"index": "全部指数源不可用，训练将使用股票池等权基准"})
    else:
        summary["index_rows"] = None

    # 3) 涨跌停池（只有最近约 30 个交易日可用；增量累积）
    zt_rows: list[dict[str, Any]] = []
    if not args.skip_zt:
        zt_path = out_dir / ZT_FILE
        existing_dates: set[str] = set()
        if zt_path.exists():
            try:
                existing_dates = set(pd.read_parquet(zt_path)["trade_date"].astype(str))
            except Exception:  # pragma: no cover - 旧文件损坏时忽略
                existing_dates = set()
        days = _trading_days(start, end)
        if args.zt_days and args.zt_days > 0:
            days = days[-args.zt_days:]
        days = [d for d in days if d not in existing_dates]
        consecutive_fail = 0
        for day in days:
            if consecutive_fail >= 8:
                summary["errors"].append({"zt_pool": f"连续 {consecutive_fail} 日不可用，停止抓取（历史窗口限制）"})
                LOGGER.warning("涨跌停池连续 %d 日不可用，提前停止", consecutive_fail)
                break
            stats = None
            if day not in existing_dates:
                try:
                    stats = fetch_zt_pool_day(day)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("涨跌停池 %s 失败：%s", day, exc)
                time.sleep(args.sleep)
            if stats is None:
                consecutive_fail += 1
                continue
            consecutive_fail = 0
            zt_rows.append(stats)
        if zt_rows:
            zt = merge_parquet(out_dir / ZT_FILE, pd.DataFrame(zt_rows, columns=ZT_COLUMNS), ["trade_date"])
        else:
            zt = pd.read_parquet(zt_path) if zt_path.exists() else pd.DataFrame(columns=ZT_COLUMNS)
        summary["zt_rows"] = int(len(zt))
        summary["zt_range"] = [str(zt["trade_date"].min()), str(zt["trade_date"].max())] if len(zt) else None
        summary["zt_note"] = "AKShare 涨跌停池免费接口仅覆盖最近约 10~30 个交易日，建议每日增量运行累积"
    else:
        summary["zt_rows"] = None

    summary["ok"] = bool(summary.get("daily_rows", 0) > 0)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["ok"]:
        LOGGER.error("未采到任何个股日线，请检查网络/接口是否被限流")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
