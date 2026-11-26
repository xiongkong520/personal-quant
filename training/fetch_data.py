#!/usr/bin/env python3
"""零成本训练管道 · 第 1 步：数据采集（AKShare 首选 + Baostock 兜底）

数据源选型（社区主流做法，见 training/README.md）
--------------------------------------------------
1. **AKShare（首选）**：A 股量化社区最流行的免费数据源，``stock_zh_a_hist_em`` 取东财前复权日线，
   ``stock_zt_pool_em`` / ``stock_zt_pool_dtgc_em`` 取每日涨跌停池；
2. **Baostock（稳定免费兜底）**：AKShare 被限流/接口异常时自动切换（官方免费、匿名登录、
   日线自带前复权），社区长期使用的稳定兜底源；
3. 不自行爬取腾讯/新浪原始 HTTP 接口（社区不推荐，接口无 SLA），因此本脚本**不包含**这类降级路径。

产出（默认 ``training/data/``，Parquet + snappy 压缩）
------------------------------------------------------
- ``daily.parquet``   个股日线：**前复权** OHLC、成交量（股）、成交额（元）、换手率（%）
- ``zt_pool.parquet`` 每日涨跌停池统计：涨停/跌停家数、炸板率、连板高度、封板资金、涨停主线行业
- ``index.parquet``   基准/市场指数日线（沪深300 / 上证指数 / 深证成指）

口径
----
- 前复权（AKShare ``adjust="qfq"``；Baostock ``adjustflag="2"``）；
- **剔除停牌零成交行**（volume<=0 或价格为空）；
- 交易日历优先 AKShare 官方封装 ``calendar_trade_date_hist_sina``，失败时用 Baostock 交易日历；
- 幂等增量：已存在的 parquet 按主键合并去重，每天重跑只补新数据。

已知限制（实测 AKShare 1.20.65）
------------------------------
``stock_zt_pool_em`` 只返回最近约 10~30 个交易日的涨停池，``stock_zt_pool_dtgc_em`` /
``stock_zt_pool_zbgc_em`` 明确只支持最近 30 个交易日。因此本脚本默认抓最近 ``--zt-days``（30）
个交易日并支持历史累积；更早的情绪特征由训练脚本用「股票池内代理情绪」补齐（见 README）。

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

#: 默认股票池（40 只，覆盖主板/创业板/科创板，消费/金融/新能源/科技/周期等行业）。
DEFAULT_POOL: list[str] = [
    "600519", "000001", "300750", "601318", "600036", "000858", "002594", "600030",
    "601012", "000333", "600276", "002415", "600887", "601899", "600900", "601166",
    "000651", "002475", "300059", "600309", "601088", "600048", "000725", "002714",
    "601398", "601288", "601988", "601857", "600028", "601668", "600050", "000063",
    "002230", "300124", "688981", "600585", "601919", "600438", "002027", "000568",
]

BENCHMARK_INDEX = "000300"                  # 沪深300：超额收益基准
MARKET_INDEX_CODES = ("000001", "399001")   # 上证指数 + 深证成指：合计≈全市场成交额

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


_BAOSTOCK_READY = False


def _bs():
    import baostock as bs  # noqa: PLC0415

    return bs


def _bs_login() -> None:
    """Baostock 匿名登录（进程内只登录一次）。"""
    global _BAOSTOCK_READY
    if _BAOSTOCK_READY:
        return
    bs = _bs()
    result = bs.login()
    if getattr(result, "error_code", "1") != "0":
        raise RuntimeError(f"Baostock 登录失败：{getattr(result, 'error_msg', '')}")
    _BAOSTOCK_READY = True
    LOGGER.info("Baostock 登录成功（免费兜底数据源）")


def bs_logout() -> None:
    """退出 Baostock 会话（脚本结束时调用，尽力而为）。"""
    global _BAOSTOCK_READY
    if not _BAOSTOCK_READY:
        return
    try:
        _bs().logout()
    except Exception:  # pragma: no cover
        pass
    _BAOSTOCK_READY = False


def round_half_up(value: float, digits: int = 2) -> float:
    """四舍五入到指定小数位（A 股报价规则，不用银行家舍入）。"""
    factor = 10**digits
    return math.floor(float(value) * factor + 0.5) / factor


def _compact(day: str) -> str:
    return pd.Timestamp(day).strftime("%Y%m%d")


def bs_code(symbol: str) -> str:
    """6 位代码 → Baostock 代码（sh.600519 / sz.000001）。"""
    code = str(symbol).strip().lower()
    if "." in code:
        left, right = code.split(".", 1)
        return f"{right.lower()}.{left}" if right.isdigit() else code
    code = code.zfill(6)
    return f"sh.{code}" if code.startswith(("6", "9", "5")) else f"sz.{code}"


def bs_index_code(code: str) -> str:
    """指数代码 → Baostock 代码（399xxx 为深证）。"""
    code = str(code).strip().zfill(6)
    return f"sz.{code}" if code.startswith("399") else f"sh.{code}"


def limit_pct_for(symbol: str) -> float:
    """按板块给出涨跌停比例（ST 无标记，按板块近似；研究口径）。"""
    symbol = str(symbol).zfill(6)
    if symbol.startswith(("300", "301", "688")):
        return 0.20
    if symbol.startswith(("8", "4")):
        return 0.30
    return 0.10


def retry_call(
    fn: Callable[..., Any],
    *args: Any,
    retries: int = 3,
    base_sleep: float = 2.0,
    label: str = "",
    **kwargs: Any,
) -> Any:
    """带指数退避 + 抖动的重试（AKShare 限流时避免立刻重放）。"""
    last_exc: Exception | None = None
    for attempt in range(max(int(retries), 1)):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 多源兜底需要捕获数据源异常
            last_exc = exc
            wait = base_sleep * (2**attempt) + random.uniform(0, 0.5)
            LOGGER.warning(
                "%s 第 %d/%d 次失败：%s；%.1fs 后重试",
                label or getattr(fn, "__name__", "call"), attempt + 1, retries, exc, wait,
            )
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# 交易日历（AKShare 官方封装 → Baostock → 工作日兜底）
# ---------------------------------------------------------------------------
def _trading_days(start: str, end: str) -> list[str]:
    # 1) AKShare 官方封装的交易日历
    try:
        cal = _ak().calendar_trade_date_hist_sina()
        dates = pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d")
        out = [d for d in dates if start <= d <= end]
        if out:
            return out
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("AKShare 交易日历获取失败，尝试 Baostock：%s", exc)
    # 2) Baostock 交易日历（社区稳定兜底）
    try:
        _bs_login()
        rs = _bs().query_trade_dates(start_date=start, end_date=end)
        rows: list[list[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=rs.fields)
        out = [d for d, flag in zip(df["calendar_date"], df["is_trading_day"]) if str(flag) == "1"]
        if out:
            return out
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Baostock 交易日历获取失败，退回工作日：%s", exc)
    # 3) 工作日兜底（只用于合成/极端情况）
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]


# ---------------------------------------------------------------------------
# 规范化：AKShare / Baostock → 统一 schema
# ---------------------------------------------------------------------------
def _finalize_stock(df: pd.DataFrame) -> pd.DataFrame:
    """清洗：去重、去空、**剔除停牌零成交行**、日期升序、列顺序固定。"""
    out = df.copy()
    for col in ("open", "high", "low", "close", "pre_close", "amount", "pct_chg", "turnover_rate"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0)
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[(out["volume"] > 0) & (out["open"] > 0) & (out["close"] > 0)]  # 剔除停牌/零值
    out["high"] = out[["high", "open", "close"]].max(axis=1)
    out["low"] = out[["low", "open", "close"]].min(axis=1)
    out["volume"] = out["volume"].round().astype("int64")
    out = out.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    out = out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    return out[STOCK_COLUMNS]


def ingest_akshare_daily(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """AKShare ``stock_zh_a_hist_em``（adjust=qfq）→ 统一 schema。

    - 成交量单位=手 → ×100 转股；``pre_close = close - 涨跌额``（前复权序列前收）
    """
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=STOCK_COLUMNS)
    df = raw.copy()
    df["trade_date"] = pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d")
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
            "source": "akshare",
        }
    )
    if "涨跌额" in df.columns:
        out["pre_close"] = out["close"] - pd.to_numeric(df["涨跌额"], errors="coerce")
    else:
        out["pre_close"] = out["close"].shift(1)
    return _finalize_stock(out)


def ingest_baostock_daily(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Baostock 前复权日线（adjustflag=2）→ 统一 schema。volume 已是股、turn 已是 %。"""
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=STOCK_COLUMNS)
    df = raw.copy()
    df = df[df["close"].astype(str).str.strip().isin(["", "nan"]) == False]  # noqa: E712
    if len(df) == 0:
        return pd.DataFrame(columns=STOCK_COLUMNS)

    def num(col: str) -> pd.Series:
        return pd.to_numeric(df.get(col), errors="coerce")

    out = pd.DataFrame(
        {
            "symbol": str(symbol).zfill(6),
            "trade_date": pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d"),
            "open": num("open"),
            "high": num("high"),
            "low": num("low"),
            "close": num("close"),
            "volume": num("volume").fillna(0),
            "amount": num("amount"),
            "turnover_rate": num("turn"),
            "source": "baostock",
        }
    )
    out["pre_close"] = out["close"].shift(1)
    out["pct_chg"] = (out["close"] / out["pre_close"] - 1.0) * 100.0
    return _finalize_stock(out)


def ingest_index(raw: pd.DataFrame, code: str, source: str) -> pd.DataFrame:
    """指数日线 → ``INDEX_COLUMNS``（兼容 AKShare 中文列与 Baostock 英文列）。"""
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=INDEX_COLUMNS)
    df = raw.copy()
    date_col = "日期" if "日期" in df.columns else "date"
    col = lambda cn, en: df.get(cn, df.get(en))  # noqa: E731
    out = pd.DataFrame(
        {
            "symbol": str(code).zfill(6),
            "trade_date": pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d"),
            "open": pd.to_numeric(col("开盘", "open"), errors="coerce"),
            "high": pd.to_numeric(col("最高", "high"), errors="coerce"),
            "low": pd.to_numeric(col("最低", "low"), errors="coerce"),
            "close": pd.to_numeric(col("收盘", "close"), errors="coerce"),
            "volume": pd.to_numeric(col("成交量", "volume"), errors="coerce"),
            "amount": pd.to_numeric(col("成交额", "amount"), errors="coerce")
            if col("成交额", "amount") is not None else np.nan,
            "source": source,
        }
    )
    out = out.dropna(subset=["close"]).drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    return out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)[INDEX_COLUMNS]


# ---------------------------------------------------------------------------
# 数据源抓取：AKShare 首选 → Baostock 兜底
# ---------------------------------------------------------------------------
def _akshare_stock_hist() -> Callable[..., pd.DataFrame]:
    ak = _ak()
    fn = getattr(ak, "stock_zh_a_hist_em", None) or getattr(ak, "stock_zh_a_hist", None)
    if fn is None:
        raise AttributeError("akshare 没有 stock_zh_a_hist(_em) 接口")
    return fn


def _akshare_index_hist() -> Callable[..., pd.DataFrame]:
    ak = _ak()
    for name in ("index_zh_hist_em", "index_zh_daily_em"):
        fn = getattr(ak, name, None)
        if fn is not None:
            return fn
    raise AttributeError("akshare 没有指数日线接口")


def fetch_akshare_stock_daily(symbol: str, start: str, end: str, retries: int) -> pd.DataFrame:
    """AKShare（东财，前复权）个股日线。"""
    fn = _akshare_stock_hist()
    raw = retry_call(
        lambda: fn(
            symbol=str(symbol).zfill(6), period="daily",
            start_date=_compact(start), end_date=_compact(end), adjust="qfq",
        ),
        retries=retries, label=f"akshare/{symbol}",
    )
    frame = ingest_akshare_daily(raw, symbol)
    frame = frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)]
    return frame.reset_index(drop=True)


def fetch_baostock_stock_daily(symbol: str, start: str, end: str, retries: int) -> pd.DataFrame:
    """Baostock 前复权日线（免费稳定兜底）。"""
    _bs_login()
    fields = "date,open,high,low,close,volume,amount,turn,pctChg"

    def query() -> pd.DataFrame:
        rs = _bs().query_history_k_data_plus(
            bs_code(symbol), fields, start_date=start, end_date=end,
            frequency="d", adjustflag="2",  # 2=前复权
        )
        if getattr(rs, "error_code", "0") != "0":
            raise RuntimeError(f"Baostock 查询失败 {symbol}：{getattr(rs, 'error_msg', '')}")
        rows: list[list[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)

    raw = retry_call(query, retries=retries, label=f"baostock/{symbol}")
    frame = ingest_baostock_daily(raw, symbol)
    frame = frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)]
    return frame.reset_index(drop=True)


def fetch_stock_daily(symbol: str, start: str, end: str, retries: int = 2, sleep: float = 0.45) -> pd.DataFrame:
    """个股日线：AKShare 首选，失败自动切 Baostock；两者都失败抛 DataSourceError。"""
    errors: list[str] = []
    # 1) AKShare（社区主流首选）
    try:
        frame = fetch_akshare_stock_daily(symbol, start, end, retries)
        if len(frame) > 0:
            LOGGER.info("fetch %s <- akshare rows=%d", symbol, len(frame))
            time.sleep(sleep)
            return frame
        errors.append("akshare: empty")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"akshare: {exc}")
        LOGGER.warning("AKShare %s 失败，切换 Baostock：%s", symbol, exc)
    # 2) Baostock（社区稳定免费兜底）
    try:
        frame = fetch_baostock_stock_daily(symbol, start, end, retries)
        if len(frame) > 0:
            LOGGER.info("fetch %s <- baostock rows=%d", symbol, len(frame))
            time.sleep(sleep)
            return frame
        errors.append("baostock: empty")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"baostock: {exc}")
        LOGGER.warning("Baostock %s 也失败：%s", symbol, exc)
    raise DataSourceError(f"{symbol} 所有数据源失败：{errors}")


def fetch_index_daily(code: str, start: str, end: str, retries: int = 2, sleep: float = 0.45) -> pd.DataFrame:
    """指数日线：AKShare 首选 → Baostock 兜底；都失败返回空表（训练降级为股票池等权基准）。"""
    errors: list[str] = []
    try:
        fn = _akshare_index_hist()
        raw = retry_call(
            lambda: fn(symbol=str(code).zfill(6), period="daily",
                       start_date=_compact(start), end_date=_compact(end)),
            retries=retries, label=f"akshare/index{code}",
        )
        frame = ingest_index(raw, code, "akshare")
        frame = frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)]
        if len(frame) > 0:
            LOGGER.info("index %s <- akshare rows=%d", code, len(frame))
            time.sleep(sleep)
            return frame.reset_index(drop=True)
        errors.append("akshare: empty")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"akshare: {exc}")
        LOGGER.warning("AKShare 指数 %s 失败，切换 Baostock：%s", code, exc)

    try:
        _bs_login()
        fields = "date,open,high,low,close,volume,amount"

        def query() -> pd.DataFrame:
            rs = _bs().query_history_k_data_plus(
                bs_index_code(code), fields, start_date=start, end_date=end, frequency="d"
            )
            if getattr(rs, "error_code", "0") != "0":
                raise RuntimeError(f"Baostock 指数查询失败 {code}：{getattr(rs, 'error_msg', '')}")
            rows: list[list[str]] = []
            while rs.next():
                rows.append(rs.get_row_data())
            return pd.DataFrame(rows, columns=rs.fields)

        raw = retry_call(query, retries=retries, label=f"baostock/index{code}")
        frame = ingest_index(raw, code, "baostock")
        frame = frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)]
        if len(frame) > 0:
            LOGGER.info("index %s <- baostock rows=%d", code, len(frame))
            time.sleep(sleep)
            return frame.reset_index(drop=True)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"baostock: {exc}")
    LOGGER.warning("指数 %s 全部数据源失败（训练将使用股票池等权基准）：%s", code, errors)
    return pd.DataFrame(columns=INDEX_COLUMNS)


# ---------------------------------------------------------------------------
# 涨跌停池（AKShare 官方接口；历史窗口有限）
# ---------------------------------------------------------------------------
def _zt_endpoint(ak: Any, name: str, day: str) -> pd.DataFrame:
    fn = getattr(ak, name, None)
    if fn is None:
        return pd.DataFrame()
    try:
        return fn(date=_compact(day))
    except Exception as exc:  # noqa: BLE001 - 历史窗口外/网络异常
        LOGGER.debug("%s(%s) 不可用：%s", name, day, exc)
        return pd.DataFrame()


def compute_zt_stats(zt: pd.DataFrame, dt: pd.DataFrame, zb: pd.DataFrame, day: str) -> dict[str, Any]:
    """由涨停池/跌停池/炸板池计算当日市场情绪统计。"""
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
        "has_down_data": int(dt is not None and len(dt) > 0),
        "has_broken_data": int(zb is not None and len(zb) > 0),
    }


def fetch_zt_pool_day(day: str) -> dict[str, Any] | None:
    """抓取单日涨跌停池统计；接口不可用（历史窗口外/非交易日）返回 None。"""
    ak = _ak()
    zt = _zt_endpoint(ak, "stock_zt_pool_em", day)
    if zt.empty:
        return None  # 涨停池不可用（实测历史日期返回空），视为不可用
    dt = _zt_endpoint(ak, "stock_zt_pool_dtgc_em", day)
    zb = _zt_endpoint(ak, "stock_zt_pool_zbgc_em", day)
    return compute_zt_stats(zt, dt, zb, day)


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
    """生成与真实 schema 一致的合成日线/指数/涨跌停池（离线测试用）。"""
    symbols = list(symbols or DEFAULT_POOL[:5])
    rng = np.random.default_rng(seed)
    if start:
        dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start=start, periods=days)]
    else:
        dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(end=date.today(), periods=days)]
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
        volume = rng.lognormal(14.0, 0.4, len(dates)).astype("int64")
        daily_frames.append(
            pd.DataFrame({
                "symbol": str(symbol).zfill(6),
                "trade_date": dates,
                "open": np.round(open_, 2),
                "high": np.round(high, 2),
                "low": np.round(low, 2),
                "close": np.round(close, 2),
                "pre_close": np.round(pre_close, 2),
                "volume": volume,
                "amount": np.round(volume * close, 2),
                "pct_chg": np.round((close / pre_close - 1) * 100, 4),
                "turnover_rate": np.round(rng.uniform(0.2, 5.0, len(dates)), 4),
                "source": "synthetic",
            })
        )
    for code in (BENCHMARK_INDEX, *MARKET_INDEX_CODES):
        idx_close = 3000.0 * np.exp(np.cumsum(market_ret))
        index_frames.append(
            pd.DataFrame({
                "symbol": code,
                "trade_date": dates,
                "open": np.round(idx_close * (1 + rng.normal(0, 0.002, len(dates))), 3),
                "high": np.round(idx_close * (1 + np.abs(rng.normal(0, 0.004, len(dates)))), 3),
                "low": np.round(idx_close * (1 - np.abs(rng.normal(0, 0.004, len(dates)))), 3),
                "close": np.round(idx_close, 3),
                "volume": rng.integers(1e8, 5e8, len(dates)).astype("int64"),
                "amount": np.round(rng.uniform(2e11, 8e11, len(dates)), 2),
                "source": "synthetic",
            })
        )
    zt_rows = []
    for i, d in enumerate(dates):
        if i < len(dates) - 30:
            continue  # 模拟真实接口：只有最近 30 天有涨跌停池数据
        up = int(max(1, round(40 + 800 * market_ret[i] + rng.uniform(-15, 15))))
        down = int(max(0, round(8 - 600 * market_ret[i] + rng.uniform(-4, 6))))
        broken = int(max(0, round(up * rng.uniform(0.1, 0.35))))
        zt_rows.append({
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
        })
    return {
        "daily": pd.concat(daily_frames, ignore_index=True)[STOCK_COLUMNS],
        "index": pd.concat(index_frames, ignore_index=True)[INDEX_COLUMNS],
        "zt_pool": pd.DataFrame(zt_rows, columns=ZT_COLUMNS),
    }


def write_synthetic_data(out_dir: Path, args: argparse.Namespace) -> dict[str, int]:
    symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or DEFAULT_POOL[:5]
    frames = generate_synthetic_frames(
        symbols, days=max(int(args.synthetic_days), 120), seed=args.seed
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, filename, keys in (
        ("daily", DAILY_FILE, ["symbol", "trade_date"]),
        ("index", INDEX_FILE, ["symbol", "trade_date"]),
        ("zt_pool", ZT_FILE, ["trade_date"]),
    ):
        merge_parquet(out_dir / filename, frames[name], keys)
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
        description="训练数据采集：AKShare 首选 + Baostock 兜底（前复权日线 + 涨跌停池 → Parquet）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--symbols", default=None, help="逗号分隔股票代码；缺省用内置 40 只池")
    parser.add_argument("--start", default=start_default, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=end_default, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--out-dir", default=str(DEFAULT_DATA_DIR), help="输出目录")
    parser.add_argument("--retries", type=int, default=2, help="单接口重试次数")
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
        "symbols_requested": len(symbols), "errors": [], "sources": {},
    }

    try:
        # 1) 个股日线（AKShare 首选 → Baostock 兜底；增量合并）
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
            summary["sources"] = {
                "daily": daily.groupby("source")["symbol"].nunique().to_dict() if "source" in daily.columns else {}
            }
        else:
            summary["daily_rows"] = 0

        # 2) 指数基准（失败降级，不阻塞训练）
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
                summary["sources"]["index"] = index.groupby("source")["symbol"].nunique().to_dict() if len(index) else {}
            else:
                summary["index_rows"] = 0
                summary["errors"].append({"index": "全部指数源不可用，训练将使用股票池等权基准"})
        else:
            summary["index_rows"] = None

        # 3) 涨跌停池（AKShare 官方接口；只有最近约 30 个交易日可用；增量累积）
        if not args.skip_zt:
            zt_path = out_dir / ZT_FILE
            existing_dates: set[str] = set()
            if zt_path.exists():
                try:
                    existing_dates = set(pd.read_parquet(zt_path)["trade_date"].astype(str))
                except Exception:  # pragma: no cover
                    existing_dates = set()
            days = _trading_days(start, end)
            if args.zt_days and args.zt_days > 0:
                days = days[-args.zt_days:]
            days = [d for d in days if d not in existing_dates]
            zt_rows: list[dict[str, Any]] = []
            consecutive_fail = 0
            for day in days:
                if consecutive_fail >= 8:
                    summary["errors"].append({"zt_pool": f"连续 {consecutive_fail} 日不可用，停止抓取（接口仅支持近 30 个交易日）"})
                    LOGGER.warning("涨跌停池连续 %d 日不可用，提前停止", consecutive_fail)
                    break
                stats = None
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
    finally:
        bs_logout()

    summary["ok"] = bool(summary.get("daily_rows", 0) > 0)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["ok"]:
        LOGGER.error("未采到任何个股日线，请检查网络/接口是否被限流")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
