"""Baostock 免费数据源兜底（免注册、匿名登录，立即可用）。

用途：本地数据层在 AKShare 东财/新浪都失败时的第三兜底（iFinD 审批期间的过渡源）。
特点：无需注册/审核、官方服务器、日线含前复权；有每日请求上限。
云端 Worker 无法使用（Python SDK），云端仍走东财→腾讯→新浪 HTTP 兜底。
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from quant.data.models import DAILY_COLUMNS

logger = logging.getLogger(__name__)
_SESSION_READY = False


def _baostock_code(symbol: str) -> str:
    raw = symbol.strip().lower()
    if raw.startswith(("sh", "sz", "bj")):
        return f"{raw[:2]}.{raw[2:]}"
    if "." in raw:
        code, exchange = raw.split(".", 1)
        return f"{exchange.lower()}.{code}"
    code = raw.zfill(6)
    return f"sh.{code}" if code.startswith(("6", "9", "5")) else f"sz.{code}"


def _ensure_login() -> None:
    global _SESSION_READY
    if _SESSION_READY:
        return
    import baostock as bs  # noqa: PLC0415

    result = bs.login()
    if result.error_code != "0":
        raise RuntimeError(f"Baostock 登录失败：{result.error_msg}")
    _SESSION_READY = True


def _query(symbol: str, start: str, end: str, adjustflag: str) -> pd.DataFrame:
    import baostock as bs  # noqa: PLC0415

    rs = bs.query_history_k_data_plus(
        _baostock_code(symbol),
        "date,open,high,low,close,volume,amount,pctChg,turn",
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag=adjustflag,
    )
    if rs.error_code != "0":
        raise RuntimeError(f"Baostock 查询失败 {symbol}：{rs.error_msg}")
    rows: list[list[str]] = []
    while rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)


def _limit_ratio(symbol: str) -> float:
    code = symbol.strip().lower()
    code = code[2:] if code.startswith(("sh", "sz", "bj")) else code.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("8", "4")):
        return 0.30
    return 0.10


def fetch_daily_baostock(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Baostock 日线 → 契约 §3.1 标准列（不复权 close + 前复权 adj_close）。"""
    _ensure_login()
    raw = _query(symbol, start, end, "3")  # 3=不复权
    if raw.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)
    adjusted = _query(symbol, start, end, "2")  # 2=前复权

    frame = raw.copy()
    frame = frame[frame["close"].astype(str).str.strip() != ""]
    if frame.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)

    def to_num(series: pd.Series) -> pd.Series:
        return pd.to_numeric(series, errors="coerce")

    close = to_num(frame["close"])
    pct = to_num(frame["pctChg"])
    pre_close = close / (1 + pct / 100.0)
    ratio = _limit_ratio(symbol)

    adj_map = {row["date"]: row["close"] for _, row in adjusted.iterrows()}
    adj_close = [to_num(pd.Series([adj_map.get(d, c)])).iloc[0] for d, c in zip(frame["date"], close)]

    out = pd.DataFrame({
        "symbol": symbol.strip().zfill(6),
        "trade_date": frame["date"].astype(str),
        "open": to_num(frame["open"]),
        "high": to_num(frame["high"]),
        "low": to_num(frame["low"]),
        "close": close,
        "pre_close": pre_close.round(4),
        "volume": to_num(frame["volume"]),
        "amount": to_num(frame["amount"]),
        "pct_chg": pct.round(4),
        "turnover_rate": to_num(frame["turn"]),
        "adj_factor": 1.0,
        "adj_close": pd.to_numeric(adj_close, errors="coerce"),
        "limit_up": (pre_close * (1 + ratio)).round(2),
        "limit_down": (pre_close * (1 - ratio)).round(2),
        "is_suspended": 0,
    })
    out["adj_close"] = out["adj_close"].fillna(out["close"])
    return out[list(DAILY_COLUMNS)]


def fetch_index_daily_baostock(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Baostock 指数日线（sh.000001 等）。"""
    _ensure_login()
    raw = _query(symbol, start, end, "3")
    if raw.empty:
        return pd.DataFrame(columns=["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"])
    def to_num(series: pd.Series) -> pd.Series:
        return pd.to_numeric(series, errors="coerce")
    return pd.DataFrame({
        "symbol": symbol.strip().lower(),
        "trade_date": raw["date"].astype(str),
        "open": to_num(raw["open"]),
        "high": to_num(raw["high"]),
        "low": to_num(raw["low"]),
        "close": to_num(raw["close"]),
        "volume": to_num(raw["volume"]),
        "amount": to_num(raw["amount"]),
        "pct_chg": to_num(raw["pctChg"]),
    })


def is_available() -> bool:
    """快速探测 Baostock 是否可用（登录成功）。"""
    try:
        _ensure_login()
        return True
    except Exception:  # noqa: BLE001
        return False
