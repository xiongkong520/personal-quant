"""日线读取与规范化（train / predict / backtest 共用）。

优先调用数据层 ``quant.data.store.read_daily_multi``（契约 §9），
数据层尚未实现或不可用时退回直连 SQLite 读 ``daily_bars``（只读，不写库）。
数据只从本地 ``data/db/quant.db`` 读，不联网。
"""
from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from quant.config import cfg_path, project_path

logger = logging.getLogger(__name__)

DAILY_COLUMNS = [
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
]

_NUMERIC_COLUMNS = [
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
]


# ---------------------------------------------------------------------------
# 路径解析（cfg 里可选的 model.out / backtest.report_dir 覆盖默认值）
# ---------------------------------------------------------------------------
def resolve_db_path(cfg: dict) -> Path:
    return cfg_path(cfg, "data.db")


def resolve_model_path(cfg: dict) -> Path:
    """模型产物路径：默认 ``data/models/model.json``，可用 ``model.out`` 覆盖。"""
    model_cfg = cfg.get("model") or {}
    rel = model_cfg.get("out") or "data/models/model.json"
    return project_path(rel)


def resolve_report_dir(cfg: dict) -> Path:
    bt = cfg.get("backtest") or {}
    rel = bt.get("report_dir") or "data/reports"
    return project_path(rel)


# ---------------------------------------------------------------------------
# 涨跌停兜底（数据层未提供 limit_up/limit_down 时用；优先走 paper.rules）
# ---------------------------------------------------------------------------
def limit_pct(symbol: str) -> float:
    symbol = str(symbol).zfill(6)
    if symbol.startswith(("300", "301", "688")):
        return 0.20
    if symbol.startswith(("8", "4")):
        return 0.30
    return 0.10


def _round_price(value: float) -> float:
    """四舍五入到分（A 股报价规则）。"""
    return math.floor(float(value) * 100 + 0.5) / 100


def _limit_prices_fallback(pre_close: float, symbol: str) -> tuple[float, float]:
    pct = limit_pct(symbol)
    return _round_price(pre_close * (1 + pct)), _round_price(pre_close * (1 - pct))


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------
def normalize_daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    """把任意来源的日线表规范成契约 §3.1 的列与类型（排序、补齐可选列）。"""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=DAILY_COLUMNS)

    out = df.copy()
    if "symbol" not in out.columns:
        raise KeyError("日线缺少 symbol 列")
    out["symbol"] = out["symbol"].astype(str).str.strip().str.zfill(6)
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y-%m-%d")

    for col in _NUMERIC_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        else:
            out[col] = np.nan

    out = out.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    out = out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    # 前收：缺失用同票上一日收盘兜底
    fallback_pre = out.groupby("symbol", sort=False)["close"].shift(1)
    out["pre_close"] = out["pre_close"].where(
        out["pre_close"].notna() & (out["pre_close"] > 0), fallback_pre
    )

    # 复权因子 / 前复权价
    out["adj_factor"] = out["adj_factor"].where(
        out["adj_factor"].notna() & (out["adj_factor"] > 0), 1.0
    )
    computed_adj = out["close"] * out["adj_factor"]
    out["adj_close"] = out["adj_close"].where(
        out["adj_close"].notna() & (out["adj_close"] > 0), computed_adj
    )

    # 涨跌幅 / 换手 / 停牌
    computed_pct = (out["close"] / out["pre_close"] - 1.0) * 100.0
    out["pct_chg"] = out["pct_chg"].where(out["pct_chg"].notna(), computed_pct)
    out["turnover_rate"] = out["turnover_rate"].fillna(0.0)
    out["is_suspended"] = out["is_suspended"].fillna(0).astype(int)

    # 涨跌停价兜底
    need_limit = out["limit_up"].isna() | out["limit_down"].isna()
    if need_limit.any():
        for idx in out.index[need_limit]:
            pre = out.at[idx, "pre_close"]
            if pd.isna(pre) or pre <= 0:
                continue
            up, down = _limit_prices_fallback(float(pre), str(out.at[idx, "symbol"]))
            out.at[idx, "limit_up"] = up
            out.at[idx, "limit_down"] = down

    out["volume"] = out["volume"].fillna(0).astype("int64")
    return out[[c for c in DAILY_COLUMNS if c in out.columns]]


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def _read_sql_daily(
    symbols: list[str],
    db_path: Path,
    start: str | None,
    end: str | None,
) -> pd.DataFrame:
    if not db_path.exists():
        raise FileNotFoundError(
            f"行情库不存在: {db_path}（请先运行 python -m quant.cli fetch 采集数据）"
        )
    syms = [str(s).strip().zfill(6) for s in symbols if str(s).strip()]
    clauses: list[str] = []
    params: list[object] = []
    if syms:
        clauses.append(f"symbol IN ({', '.join('?' for _ in syms)})")
        params.extend(syms)
    if start:
        clauses.append("trade_date >= ?")
        params.append(pd.Timestamp(start).strftime("%Y-%m-%d"))
    if end:
        clauses.append("trade_date <= ?")
        params.append(pd.Timestamp(end).strftime("%Y-%m-%d"))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM daily_bars{where} ORDER BY symbol, trade_date"
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            df = pd.read_sql_query(sql, con, params=params)
    except Exception as exc:
        raise RuntimeError(f"读取 daily_bars 失败（{db_path}）: {exc}") from exc
    return normalize_daily_frame(df)


def load_daily_multi(
    symbols: list[str],
    cfg: dict,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """读多只股票日线；优先数据层实现，缺失时直连 SQLite 兜底。"""
    db_path = resolve_db_path(cfg)
    df = None
    try:
        from quant.data.store import read_daily_multi as store_read  # type: ignore
    except Exception:  # pragma: no cover - 数据层未就绪时走兜底
        store_read = None
    if store_read is not None:
        try:
            df = store_read(list(symbols), db_path, start=start, end=end)
        except Exception as exc:  # 数据层异常时退回本地 SQL（保持可离线）
            logger.debug("data.store.read_daily_multi 不可用，走 SQL 兜底: %s", exc)
            df = None
    if df is None or len(df) == 0:
        df = _read_sql_daily(list(symbols), db_path, start, end)
    return normalize_daily_frame(df)


def load_daily(
    symbol: str,
    cfg: dict,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """读单只股票日线。"""
    return load_daily_multi([symbol], cfg, start=start, end=end)
