"""数据层列定义、dtype 约定与基础转换工具（契约 §3、§11）。

本模块**不做任何网络调用**，只提供：
- 三张表的列名/类型常量（与 docs/CONTRACT.md §3 一一对应）；
- DataFrame 规范化函数 ``normalize_daily`` / ``normalize_minute``（pandas 3.x
  copy-on-write 安全：全部返回新对象，不原地改调用方数据）；
- A 股涨跌停价计算（契约 §6 的本地实现，供抓数时落 ``limit_up`` / ``limit_down``；
  若 ``quant.paper.rules`` 可用，fetch.py 会优先复用那份共用实现）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 列定义（顺序即写库/读库顺序，与契约 §3.1 / §3.2 / §3.3 一致）
# ---------------------------------------------------------------------------
DAILY_COLUMNS: tuple[str, ...] = (
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "pct_chg",
    "turnover_rate",
    "adj_factor",
    "adj_close",
    "limit_up",
    "limit_down",
    "is_suspended",
)

MINUTE_COLUMNS: tuple[str, ...] = (
    "symbol",
    "period",
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)

SIGNAL_COLUMNS: tuple[str, ...] = (
    "signal_id",
    "symbol",
    "asof",
    "trade_date",
    "price",
    "direction",
    "prob_up",
    "prob_flat",
    "prob_down",
    "confidence",
    "horizon",
    "expires_at",
    "model_version",
    "top_features",
    "source",
    "status",
    "created_at",
)

DAILY_STR_COLUMNS: frozenset[str] = frozenset({"symbol", "trade_date"})
DAILY_INT_COLUMNS: frozenset[str] = frozenset({"volume", "is_suspended"})
MINUTE_STR_COLUMNS: frozenset[str] = frozenset({"symbol", "ts"})
MINUTE_INT_COLUMNS: frozenset[str] = frozenset({"period", "volume"})

DIRECTIONS: tuple[str, ...] = ("up", "flat", "down")
SIGNAL_STATUSES: tuple[str, ...] = ("ok", "stale", "warming_up")
SIGNAL_SOURCES: tuple[str, ...] = ("local", "cloud")
HORIZONS: tuple[str, ...] = ("1d",)

#: 写库前允许为空的日线列（其余列由 normalize_daily 补齐/校验）
DAILY_REQUIRED: tuple[str, ...] = ("symbol", "trade_date", "close", "volume")

#: 板块涨跌停比例（契约 §6）
BOARD_LIMIT_PCT: tuple[tuple[tuple[str, ...], float], ...] = (
    (("300", "301", "688", "689"), 0.20),  # 创业板 / 科创板
    (("8", "4"), 0.30),  # 北交所
)
DEFAULT_LIMIT_PCT = 0.10  # 主板（600/601/603/605/000/001/002/003）
ST_LIMIT_PCT = 0.05


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def round_half_up(value: float, digits: int = 2) -> float:
    """四舍五入（ROUND_HALF_UP）到指定小数位，A 股涨跌停价/价格按此规则。

    Python 内置 ``round`` 是银行家舍入，且二进制浮点会让 11.055 这类边界值
    落错方向，所以用 Decimal 基于 ``str`` 结果计算。
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        raise ValueError("round_half_up 收到空值")
    quantum = Decimal(1).scaleb(-digits)  # 10**-digits
    # 先收敛到 6 位小数，避免 float 表示误差（如 11.054999999999999）改变舍入方向
    dec = Decimal(str(round(float(value), 6)))
    return float(dec.quantize(quantum, rounding=ROUND_HALF_UP))


def limit_pct(symbol: str, is_st: bool = False) -> float:
    """按板块返回涨跌停比例（契约 §6）。"""
    sym = str(symbol).strip()
    if is_st:
        return ST_LIMIT_PCT
    for prefixes, pct in BOARD_LIMIT_PCT:
        if sym.startswith(prefixes):
            return pct
    return DEFAULT_LIMIT_PCT


def limit_prices(
    pre_close: float, symbol: str, is_st: bool = False
) -> tuple[float, float]:
    """返回 ``(limit_up, limit_down)``，涨跌停价 = round(pre_close × (1±比例), 2)。

    契约 §6 的共享实现在 ``src/quant/paper/rules.py``；本函数是数据层同口径的
    本地实现，用于抓数时直接落库（paper 模块可用时 fetch.py 会优先用共享版）。
    """
    pct = limit_pct(symbol, is_st=is_st)
    pre = float(pre_close)
    return (
        round_half_up(pre * (1.0 + pct), 2),
        round_half_up(pre * (1.0 - pct), 2),
    )


# A 股时区固定 +08:00（契约 §3.3/§4：asof/created_at 统一带 +08:00，CI 的 UTC 环境也一致）
TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    """当前时间 ISO 字符串（固定 +08:00，不随宿主时区变化）。"""
    return datetime.now(TZ).isoformat(timespec="seconds")


def to_compact_asof(asof: str) -> str:
    """把 ISO 时间压成 ``YYYYMMDDTHHMMSS``，用于拼 signal_id。"""
    dt = pd.Timestamp(asof)
    return dt.strftime("%Y%m%dT%H%M%S")


def _to_date_str_series(values: pd.Series) -> pd.Series:
    """任意日期表示 → ``YYYY-MM-DD`` 字符串 Series（不可解析的变 NaT 后丢弃由调用方处理）。"""
    dt = pd.to_datetime(values, errors="coerce")
    return dt.dt.strftime("%Y-%m-%d")


def _as_python(value):
    """numpy/pandas 标量 → sqlite3 可接受的 Python 标量；缺省值 → None。"""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return int(bool(value))
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return str(value)
    return value


def records(df: pd.DataFrame, columns: tuple[str, ...] | list[str]) -> list[tuple]:
    """DataFrame → sqlite3 可直接 executemany 的原生类型元组列表。"""
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"DataFrame 缺少列: {missing}")
    out: list[tuple] = []
    for row in df[list(columns)].itertuples(index=False, name=None):
        out.append(tuple(_as_python(v) for v in row))
    return out


# ---------------------------------------------------------------------------
# 空表构造（读库无行时返回带 dtype 的空 DataFrame，调用方无需特判）
# ---------------------------------------------------------------------------
def empty_frame(
    columns: tuple[str, ...],
    str_columns: frozenset[str],
    int_columns: frozenset[str],
) -> pd.DataFrame:
    data: dict[str, pd.Series] = {}
    for col in columns:
        if col in str_columns:
            data[col] = pd.Series([], dtype="object")
        elif col in int_columns:
            data[col] = pd.Series([], dtype="int64")
        else:
            data[col] = pd.Series([], dtype="float64")
    return pd.DataFrame(data)


def empty_daily() -> pd.DataFrame:
    return empty_frame(DAILY_COLUMNS, DAILY_STR_COLUMNS, DAILY_INT_COLUMNS)


def empty_minute() -> pd.DataFrame:
    return empty_frame(MINUTE_COLUMNS, MINUTE_STR_COLUMNS, MINUTE_INT_COLUMNS)


# ---------------------------------------------------------------------------
# DataFrame 规范化
# ---------------------------------------------------------------------------
def normalize_daily(df: pd.DataFrame | None) -> pd.DataFrame:
    """把日线 DataFrame 规范成契约 §3.1 的列/dtype，并补齐可推导字段。

    - 要求至少有 ``symbol`` / ``trade_date`` / ``close`` / ``volume``；
    - 价格保留 2 位；``volume`` 为整数股；``is_suspended`` 为 0/1；
    - ``turnover_rate`` 缺失填 0，``adj_factor`` 缺失填 1.0，
      ``adj_close`` 缺失时用 ``close × adj_factor`` 推导；
    - 返回新 DataFrame（copy-on-write 安全），按 ``(symbol, trade_date)`` 排序去重。
    """
    if df is None or len(df) == 0:
        return empty_daily()
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"normalize_daily 需要 DataFrame，收到 {type(df)!r}")

    missing = [c for c in DAILY_REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"日线 DataFrame 缺少必需列: {missing}")

    out = df.copy()
    out["symbol"] = out["symbol"].astype(str).str.strip().str.zfill(6)
    out["trade_date"] = _to_date_str_series(out["trade_date"])

    numeric = [
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "amount",
        "pct_chg",
        "turnover_rate",
        "adj_factor",
        "adj_close",
        "limit_up",
        "limit_down",
    ]
    for col in numeric:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")

    out["volume"] = (
        pd.to_numeric(out["volume"], errors="coerce").fillna(0).round().astype("int64")
    )
    if "is_suspended" not in out.columns:
        out["is_suspended"] = np.where(out["volume"] <= 0, 1, 0)
    out["is_suspended"] = (
        pd.to_numeric(out["is_suspended"], errors="coerce").fillna(0).round().astype("int64")
    )
    out.loc[out["volume"] <= 0, "is_suspended"] = 1

    # 可推导字段兜底
    out["turnover_rate"] = out["turnover_rate"].fillna(0.0)
    out["adj_factor"] = out["adj_factor"].fillna(1.0)
    out["adj_close"] = out["adj_close"].fillna(out["close"] * out["adj_factor"])
    # pre_close 缺失：有涨跌幅则反推，否则用 close 兜底
    pct_ratio = out["pct_chg"] / 100.0
    derived_pre = out["close"] / (1.0 + pct_ratio)
    out["pre_close"] = out["pre_close"].fillna(derived_pre).fillna(out["close"])
    out["pct_chg"] = out["pct_chg"].fillna(
        (out["close"] / out["pre_close"].replace(0, np.nan) - 1.0) * 100.0
    )
    out["pct_chg"] = out["pct_chg"].fillna(0.0)

    # 价格/金额精度（契约 §3.1：价格保留 2 位）
    for col in ("open", "high", "low", "close", "pre_close", "adj_close"):
        out[col] = out[col].round(2)
    for col in ("limit_up", "limit_down"):
        out[col] = out[col].round(2)
    out["amount"] = out["amount"].round(2)
    out["pct_chg"] = out["pct_chg"].round(4)
    out["turnover_rate"] = out["turnover_rate"].round(4)
    out["adj_factor"] = out["adj_factor"].round(8)

    out = out.dropna(subset=["symbol", "trade_date"])
    out = (
        out.sort_values(["symbol", "trade_date"], kind="stable")
        .drop_duplicates(["symbol", "trade_date"], keep="last")
        .reset_index(drop=True)
    )
    out["symbol"] = out["symbol"].astype(object)
    out["trade_date"] = out["trade_date"].astype(object)
    return out[list(DAILY_COLUMNS)]


def normalize_minute(df: pd.DataFrame | None) -> pd.DataFrame:
    """把分钟线 DataFrame 规范成契约 §3.2 的列/dtype。

    要求至少有 ``symbol`` / ``period`` / ``ts`` / ``close`` / ``volume``；
    ``volume`` 为整数股，``ts`` 为 ``YYYY-MM-DD HH:MM`` 本地收盘时刻。
    """
    if df is None or len(df) == 0:
        return empty_minute()
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"normalize_minute 需要 DataFrame，收到 {type(df)!r}")

    required = ("symbol", "period", "ts", "close", "volume")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"分钟线 DataFrame 缺少必需列: {missing}")

    out = df.copy()
    out["symbol"] = out["symbol"].astype(str).str.strip().str.zfill(6)
    ts = pd.to_datetime(out["ts"], errors="coerce")
    out["ts"] = ts.dt.strftime("%Y-%m-%d %H:%M")
    out["period"] = pd.to_numeric(out["period"], errors="coerce").round()
    out["volume"] = (
        pd.to_numeric(out["volume"], errors="coerce").fillna(0).round().astype("int64")
    )
    for col in ("open", "high", "low", "close", "amount"):
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].round(2)
    out["amount"] = out["amount"].round(2)

    out = out.dropna(subset=["symbol", "ts", "period"])
    out["period"] = out["period"].astype("int64")
    out = (
        out.sort_values(["symbol", "period", "ts"], kind="stable")
        .drop_duplicates(["symbol", "period", "ts"], keep="last")
        .reset_index(drop=True)
    )
    out["symbol"] = out["symbol"].astype(object)
    out["ts"] = out["ts"].astype(object)
    return out[list(MINUTE_COLUMNS)]
