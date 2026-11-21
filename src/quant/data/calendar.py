"""交易日历：本地缓存优先，AKShare 兜底（契约 §9）。

缓存文件默认 ``data/raw/trade_calendar.csv``（配置 ``data.calendar_cache``），
第一列 ``trade_date`` 为 ``YYYY-MM-DD``。网络调用统一走 ``quant.data.fetch``，
本模块只做缓存读写与日期计算，便于离线单测。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

import pandas as pd

from quant.data import fetch as _fetch

logger = logging.getLogger(__name__)


class CalendarError(RuntimeError):
    """交易日历不可用（无缓存且 AKShare 拉取失败）。"""


# ---------------------------------------------------------------------------
# 缓存读写
# ---------------------------------------------------------------------------
def _read_cache(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path)
    except Exception as exc:  # noqa: BLE001 - 缓存损坏时退回 AKShare
        logger.warning("交易日历缓存读取失败（%s）：%s", path, exc)
        return []
    if df.empty:
        return []
    col = "trade_date" if "trade_date" in df.columns else str(df.columns[0])
    try:
        dates = (
            pd.to_datetime(df[col], errors="coerce")
            .dropna()
            .dt.strftime("%Y-%m-%d")
            .drop_duplicates()
            .sort_values()
            .tolist()
        )
    except (TypeError, ValueError) as exc:
        logger.warning("交易日历缓存内容非法（%s）：%s", path, exc)
        return []
    return [str(d) for d in dates]


def _write_cache(path: Path, dates: Sequence[str]) -> None:  # pragma: no cover - 由 fetch 负责写
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"trade_date": list(dates)}).to_csv(path, index=False, encoding="utf-8")


def load_calendar(cfg: dict | None = None, force_refresh: bool = False) -> list[str]:
    """加载全量交易日历（``YYYY-MM-DD`` 升序）。

    缓存存在且非空时直接用缓存；``force_refresh=True`` 或缓存为空时调 AKShare
    （失败时若有旧缓存则降级用缓存并告警，否则抛 :class:`CalendarError`）。
    """
    path = _fetch.calendar_cache_path(cfg)
    cached = _read_cache(path)
    if not force_refresh and cached:
        return cached
    try:
        fresh = _fetch.fetch_calendar(cfg)
    except Exception as exc:  # noqa: BLE001 - 网络失败降级到缓存
        if cached:
            logger.warning("交易日历刷新失败，继续使用本地缓存：%s", exc)
            return cached
        raise CalendarError(f"交易日历不可用（无本地缓存且 AKShare 拉取失败）：{exc}") from exc
    if not fresh:
        if cached:
            return cached
        raise CalendarError("AKShare 返回空交易日历")
    return [str(d) for d in fresh]


def _refresh_once(cfg: dict | None, cached: list[str]) -> list[str]:
    """尝试刷新一次；失败沿用缓存。"""
    try:
        fresh = _fetch.fetch_calendar(cfg)
        if fresh:
            return [str(d) for d in fresh]
    except Exception as exc:  # noqa: BLE001
        logger.warning("交易日历刷新失败，继续使用本地缓存：%s", exc)
    return cached


def _calendar_for(cfg: dict | None, start: str, end: str) -> list[str]:
    """取能覆盖 [start, end] 的日历；缓存覆盖不足时刷新一次。"""
    cal = load_calendar(cfg)
    if not cal:
        raise CalendarError("交易日历为空")
    if start < cal[0] or end > cal[-1]:
        refreshed = _refresh_once(cfg, cal)
        if refreshed and (start >= refreshed[0] and end <= refreshed[-1]):
            return refreshed
        return refreshed  # 刷新也覆盖不足时按现有数据尽力返回
    return cal


# ---------------------------------------------------------------------------
# 对外接口（契约 §9）
# ---------------------------------------------------------------------------
def _iso(value: str | pd.Timestamp) -> str:
    return pd.Timestamp(str(value).strip()).strftime("%Y-%m-%d")


def trade_days(start: str, end: str, cfg: dict) -> list[str]:
    """返回 ``[start, end]`` 区间内的交易日列表（``YYYY-MM-DD`` 升序，含首尾）。

    缓存覆盖区间时完全不联网；覆盖不足才刷新一次。
    """
    start_iso, end_iso = _iso(start), _iso(end)
    if start_iso > end_iso:
        raise ValueError(f"start({start_iso}) 不能晚于 end({end_iso})")
    cal = _calendar_for(cfg, start_iso, end_iso)
    return [d for d in cal if start_iso <= d <= end_iso]


def is_trade_day(day: str, cfg: dict) -> bool:
    """判断某日是否 A 股交易日。"""
    day_iso = _iso(day)
    cal = _calendar_for(cfg, day_iso, day_iso)
    return day_iso in set(cal)


def next_trade_day(day: str, cfg: dict, inclusive: bool = False) -> str | None:
    """返回 ``day`` 之后（``inclusive=True`` 时含当天）的第一个交易日；越界返回 None。"""
    day_iso = _iso(day)
    cal = _calendar_for(cfg, day_iso, day_iso)
    if not cal:
        return None
    if inclusive:
        candidates = [d for d in cal if d >= day_iso]
    else:
        candidates = [d for d in cal if d > day_iso]
    return candidates[0] if candidates else None
