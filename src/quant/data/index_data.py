"""指数日线数据：新浪接口 + 本地 CSV 缓存。

训练/回测优先读缓存（离线可复现）；`refresh=True` 时重新拉取并覆盖缓存。
缓存目录 data/raw/index/（data/raw 已在 .gitignore，不随仓库分发）。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant.config import PROJECT_ROOT

INDEX_CACHE_DIR = PROJECT_ROOT / "data" / "raw" / "index"
MAJOR_INDEX_SYMBOLS: list[str] = ["sh000001", "sz399001", "sz399006", "sh000300"]


def cache_path(symbol: str) -> Path:
    return INDEX_CACHE_DIR / f"{symbol}.csv"


def fetch_index_daily(symbol: str) -> pd.DataFrame:
    """AKShare 新浪指数日线（长历史）。返回 date/open/high/low/close/volume。"""
    import akshare as ak

    df = ak.index_zh_daily_sina(symbol=symbol)
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.sort_values("date").reset_index(drop=True)


def load_index_daily(symbol: str, refresh: bool = False) -> pd.DataFrame:
    """缓存优先读取；refresh=True 或缓存不存在时联网拉取并写入缓存。"""
    path = cache_path(symbol)
    if not refresh and path.exists():
        try:
            cached = pd.read_csv(path)
            if len(cached) > 0:
                return cached
        except Exception:  # noqa: BLE001 - 缓存损坏则重新拉取
            pass
    df = fetch_index_daily(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


def load_all_indices(
    symbols: list[str] | None = None,
    refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    """批量加载主要指数日线。"""
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols or MAJOR_INDEX_SYMBOLS:
        try:
            out[symbol] = load_index_daily(symbol, refresh=refresh)
        except Exception as exc:  # noqa: BLE001
            print(f"[index_data] {symbol} 拉取失败：{type(exc).__name__}: {exc}")
    return out
