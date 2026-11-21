"""AKShare 采集层：日线 / 分钟线 / 交易日历（契约 §11）。

设计原则：
- **网络调用全部集中在本模块**（``_akshare()`` / ``fetch_daily_raw`` /
  ``fetch_minute_raw`` / ``fetch_calendar``），便于单测用 ``monkeypatch``
  替换成固定 fixture，测试不联网；
- 列名/单位转换（手→股、pre_close、不复权 + 前复权、涨跌停价、停牌）是**纯函数**
  （``convert_daily`` / ``convert_minute``），可用构造的 AKShare 原始 DataFrame
  离线断言；
- 采集间隔 ≥0.3s；失败重试 2 次退避；单个 symbol 失败不中断整体；
- 写库交给 ``store.upsert_*``，天然幂等。

口径要点（契约 §3.1 / §11）：
- ``close`` 用**不复权**原始价（``adjust=""`` 的接口数据）；
- ``adj_close`` 用**前复权**收盘（``adjust="qfq"`` 的"收盘"列）；
- ``pre_close = 收盘 - 涨跌额``（不复权）；``volume`` 手 ×100 → 股；
- 涨跌停价按契约 §6，优先复用 ``quant.paper.rules`` 的共享实现（可用时）。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from quant.config import cfg_path, project_path
from quant.data import models
from quant.data.models import (
    DAILY_COLUMNS,
    MINUTE_COLUMNS,
    normalize_daily,
    normalize_minute,
)
from quant.data.store import init_db, upsert_daily, upsert_minute

logger = logging.getLogger(__name__)

#: 接口最小采集间隔（秒），契约 §11 要求 ≥0.3s
MIN_REQUEST_INTERVAL = 0.3
#: 首次 + 重试 2 次
RETRY_ATTEMPTS = 3
#: 重试退避（秒）
RETRY_BACKOFF = (1.0, 2.0)
#: 分钟线只保留最近窗口（契约 §3.2）
MINUTE_WINDOW_DAYS = {1: 5, 5: 30, 15: 60, 30: 90, 60: 180}

_last_request_at: float = 0.0


class DataFetchError(RuntimeError):
    """采集失败（重试耗尽 / 被限流 / 返回空数据）。"""


# ---------------------------------------------------------------------------
# 节流 + 重试
# ---------------------------------------------------------------------------
def _cfg_interval(cfg: dict | None) -> float:
    raw = ((cfg or {}).get("data") or {}).get("min_request_interval", MIN_REQUEST_INTERVAL)
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        interval = MIN_REQUEST_INTERVAL
    return max(MIN_REQUEST_INTERVAL, interval)


def _throttle(interval: float | None = None) -> None:
    """保证两次真实网络请求间隔 ≥ interval 秒。"""
    global _last_request_at
    gap = _cfg_interval(None) if interval is None else max(0.0, float(interval))
    now = time.monotonic()
    wait = _last_request_at + gap - now
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _is_rate_limit(exc: BaseException) -> bool:
    """东财 push2 限流异常：立即停止重试，避免延长封禁。"""
    name = type(exc).__name__
    text = str(exc)
    return "RateLimit" in name or "限流" in text or "过多" in text


def _call_akshare(fn: Callable[..., Any], /, *args: Any, cfg: dict | None = None, **kwargs: Any) -> Any:
    """调用一次 AKShare 网络接口：节流 + 失败退避重试 2 次。"""
    interval = _cfg_interval(cfg)
    last_exc: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        _throttle(interval)
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 第三方库异常族不稳定，统一包装
            last_exc = exc
            if _is_rate_limit(exc):
                raise DataFetchError(
                    f"AKShare 接口被限流，已停止重试以免加重限流：{exc}"
                ) from exc
            if attempt < RETRY_ATTEMPTS - 1:
                delay = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                logger.warning(
                    "AKShare 调用失败（第 %d/%d 次），%.1fs 后重试：%s",
                    attempt + 1,
                    RETRY_ATTEMPTS,
                    delay,
                    exc,
                )
                time.sleep(delay)
    raise DataFetchError(f"AKShare 调用失败（已重试 {RETRY_ATTEMPTS - 1} 次）：{last_exc}") from last_exc


def _akshare() -> Any:
    """延迟导入 akshare（导入本身较慢，测试替换掉网络函数时无需加载）。"""
    import akshare as ak  # noqa: PLC0415

    return ak


# ---------------------------------------------------------------------------
# 日期/路径工具
# ---------------------------------------------------------------------------
def _iso_date(value: str | date | None, default: str | None = None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    return pd.Timestamp(str(value).strip()).strftime("%Y-%m-%d")


def _compact_date(value: str | date) -> str:
    return pd.Timestamp(str(value)).strftime("%Y%m%d")


def calendar_cache_path(cfg: dict | None) -> Path:
    """交易日历本地缓存路径（契约：data/raw/trade_calendar.csv）。"""
    rel = ((cfg or {}).get("data") or {}).get(
        "calendar_cache", "data/raw/trade_calendar.csv"
    )
    return project_path(rel)


def _db_path(cfg: dict) -> Path:
    return cfg_path(cfg, "data.db")


# ---------------------------------------------------------------------------
# 交易日历
# ---------------------------------------------------------------------------
def fetch_calendar(cfg: dict | None = None) -> list[str]:
    """从 AKShare 拉全量 A 股交易日历，写本地缓存 CSV，返回 ``YYYY-MM-DD`` 升序列表。"""
    ak = _akshare()
    raw = _call_akshare(ak.calendar_trade_date_hist_sina, cfg=cfg)
    if raw is None or len(raw) == 0:
        raise DataFetchError("AKShare 交易日历返回空数据")
    dates = (
        pd.to_datetime(raw["trade_date"], errors="coerce")
        .dropna()
        .dt.strftime("%Y-%m-%d")
        .drop_duplicates()
        .sort_values()
        .tolist()
    )
    if not dates:
        raise DataFetchError("AKShare 交易日历解析为空")
    path = calendar_cache_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"trade_date": dates}).to_csv(path, index=False, encoding="utf-8")
    logger.info("交易日历已缓存 %d 天 → %s", len(dates), path)
    return dates


# ---------------------------------------------------------------------------
# 日线
# ---------------------------------------------------------------------------
_DAILY_RENAME = {
    "日期": "trade_date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct_chg",
    "涨跌额": "chg",
    "换手率": "turnover_rate",
}


def resolve_limit_fn(cfg: dict | None = None) -> Callable[[float, str], tuple[float, float]]:
    """涨跌停价实现：优先复用契约 §6 的 ``quant.paper.rules.limit_prices``。

    可用性/口径做一次探针校验（pre_close=10 的主板应为 11.00/9.00），不通过就回退
    到数据层本地实现 ``models.limit_prices``，保证抓数不依赖其它模块是否已就绪。
    """
    try:
        from quant.paper.rules import limit_prices as shared  # noqa: PLC0415

        probe = shared(10.0, "600519")
        if tuple(round(float(x), 2) for x in probe) == (11.0, 9.0):
            return shared
        logger.warning("quant.paper.rules.limit_prices 口径异常，回退数据层本地实现")
    except Exception:  # noqa: BLE001 - 模块未实现/导入失败都回退
        pass
    return models.limit_prices


# ---------------------------------------------------------------------------
# 备用数据源：东财 push2 被临时限流时的自动降级（仍属 AKShare 公开接口）
# ---------------------------------------------------------------------------
#: 最近一次同步实际使用的数据源（symbol -> "em" / "sina"），供脚本/排查读取
LAST_SOURCES: dict[str, str] = {}

_LIMIT_HINT = "东方财富 push2 数据接口"


def preferred_source(cfg: dict | None = None, kind: str = "daily") -> str:
    """读配置 ``data.<kind>_source``：``auto``（默认，东财失败降级新浪）/ ``em`` / ``sina``。"""
    raw = ((cfg or {}).get("data") or {}).get(f"{kind}_source", "auto")
    value = str(raw or "auto").strip().lower()
    return value if value in ("auto", "em", "sina") else "auto"


def _sina_symbol(symbol: str) -> str:
    """6 位代码 → 新浪接口的带前缀代码（sh/sz/bj）。"""
    code = str(symbol).strip().lower()
    if code.startswith(("sh", "sz", "bj")):
        return code
    code = code.zfill(6)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("0", "2", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sh{code}"


def fetch_daily_sina_raw(
    symbol: str,
    start: str,
    end: str,
    adjust: str = "",
    cfg: dict | None = None,
) -> pd.DataFrame:
    """新浪接口日线原始表（网络调用，测试替身入口）。

    返回列：``date/open/high/low/close/volume(股)/amount/outstanding_share/turnover(比例)``。
    """
    ak = _akshare()
    return _call_akshare(
        ak.stock_zh_a_hist_sina,
        symbol=_sina_symbol(symbol),
        period="daily",
        start_date=_compact_date(start),
        end_date=_compact_date(end),
        adjust=adjust,
        cfg=cfg,
    )


def convert_daily_sina(
    qfq_df: pd.DataFrame | None,
    raw_df: pd.DataFrame | None,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    limit_fn: Callable[[float, str], tuple[float, float]] | None = None,
) -> pd.DataFrame:
    """新浪日线原始表 → 契约 §3.1 的 ``daily_bars`` DataFrame（纯函数）。

    参数顺序与 :func:`convert_daily` 一致（qfq 在前、不复权 raw 在后）。

    与东财口径差异：
    - 新浪 ``volume`` 单位已经是**股**（不再 ×100），``turnover`` 是比例（×100 转 %）；
    - 没有涨跌额，``pre_close`` 用序列内前一日收盘推导（调用方应多取一段 lookback
      再按 ``start`` 裁剪），``pct_chg`` 由 ``close/pre_close-1`` 计算；
    - ``adj_close`` 取 qfq 表的 ``close``。
    """
    base = raw_df if raw_df is not None and len(raw_df) else qfq_df
    if base is None or len(base) == 0:
        return models.empty_daily()
    if not isinstance(base, pd.DataFrame):
        raise TypeError(f"convert_daily_sina 需要 DataFrame，收到 {type(base)!r}")

    limit_fn = limit_fn or resolve_limit_fn()
    df = base.copy().rename(
        columns={
            "date": "trade_date",
            "turnover": "turnover_ratio",
        }
    )
    if "trade_date" not in df.columns:
        raise ValueError("新浪日线原始表缺少 date 列")
    for col in ("open", "high", "low", "close", "volume", "amount", "turnover_ratio"):
        if col not in df.columns:
            df[col] = float("nan")
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["trade_date"]).sort_values("trade_date", kind="stable")
    df = df.drop_duplicates("trade_date", keep="last").reset_index(drop=True)

    # 新浪成交量已是股；换手率比例 → 百分数
    df["volume"] = df["volume"].fillna(0).round().astype("int64")
    df["turnover_rate"] = (df["turnover_ratio"] * 100.0).round(4)
    # pre_close 用前一交易日收盘（lookback 段保证首行也能取到）
    df["pre_close"] = df["close"].shift(1).round(2)

    # 前复权收盘 merge
    if qfq_df is not None and len(qfq_df) and "date" in qfq_df.columns:
        adj = qfq_df.copy().rename(columns={"date": "trade_date", "close": "adj_close"})
        adj["trade_date"] = pd.to_datetime(adj["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        adj["adj_close"] = pd.to_numeric(adj["adj_close"], errors="coerce")
        adj = adj[["trade_date", "adj_close"]].dropna(subset=["trade_date"]).drop_duplicates("trade_date")
        df = df.merge(adj, on="trade_date", how="left")
    else:
        logger.warning("%s 新浪前复权数据缺失，adj_close 回退为不复权收盘", symbol)
        df["adj_close"] = df["close"]

    # 只保留请求区间（lookback 只用于推导 pre_close）
    start_iso = _iso_date(start)
    end_iso = _iso_date(end)
    if start_iso:
        df = df[df["trade_date"] >= start_iso]
    if end_iso:
        df = df[df["trade_date"] <= end_iso]
    df = df.reset_index(drop=True)
    if len(df) == 0:
        return models.empty_daily()

    df["pre_close"] = df["pre_close"].fillna(df["close"])
    df["pct_chg"] = ((df["close"] / df["pre_close"].replace(0, float("nan"))) - 1.0) * 100.0
    df["pct_chg"] = df["pct_chg"].fillna(0.0)

    # 复权因子 = 前复权收盘 / 不复权收盘
    df["adj_factor"] = 1.0
    valid = df["close"].abs() > 1e-12
    df.loc[valid, "adj_factor"] = (df.loc[valid, "adj_close"] / df.loc[valid, "close"]).round(8)
    df["adj_factor"] = df["adj_factor"].fillna(1.0)

    limits = [
        limit_fn(float(pre), str(symbol).strip().zfill(6)) if pd.notna(pre) else (float("nan"), float("nan"))
        for pre in df["pre_close"].tolist()
    ]
    df["limit_up"] = [x[0] for x in limits]
    df["limit_down"] = [x[1] for x in limits]
    df["is_suspended"] = (df["volume"] <= 0).astype("int64")
    df["symbol"] = str(symbol).strip().zfill(6)

    return normalize_daily(df[list(DAILY_COLUMNS)])


def fetch_daily_sina(
    symbol: str, start: str, end: str, cfg: dict | None = None
) -> pd.DataFrame:
    """新浪接口拉日线并转换（含 pre_close 所需的 lookback 段）。"""
    start_iso = _iso_date(start, default="1990-01-01")
    end_iso = _iso_date(end, default=None)
    if end_iso is None:
        raise ValueError("fetch_daily_sina 需要 end 日期")
    lookback = (pd.Timestamp(start_iso) - pd.Timedelta(days=30)).strftime("%Y%m%d")
    raw = fetch_daily_sina_raw(symbol, lookback, end_iso, adjust="", cfg=cfg)
    if raw is None or len(raw) == 0:
        raise DataFetchError(f"新浪接口未返回 {symbol} 日线数据（{start_iso} ~ {end_iso}）")
    try:
        qfq = fetch_daily_sina_raw(symbol, lookback, end_iso, adjust="qfq", cfg=cfg)
    except DataFetchError as exc:
        logger.warning("%s 新浪前复权数据拉取失败，adj_close 回退为不复权：%s", symbol, exc)
        qfq = pd.DataFrame()
    frame = convert_daily_sina(
        qfq, raw, symbol, start=start_iso, end=end_iso, limit_fn=resolve_limit_fn(cfg)
    )
    if len(frame) == 0:
        raise DataFetchError(f"新浪接口在 {start_iso} ~ {end_iso} 无 {symbol} 日线数据")
    return frame


def fetch_daily_raw(
    symbol: str,
    start: str,
    end: str,
    adjust: str = "",
    cfg: dict | None = None,
) -> pd.DataFrame:
    """拉单只股票日线原始 DataFrame（网络调用，测试替身入口）。

    ``adjust=""`` 不复权；``adjust="qfq"`` 前复权（其"收盘"列即 ``adj_close``）。
    """
    ak = _akshare()
    return _call_akshare(
        ak.stock_zh_a_hist_em,
        symbol=str(symbol).strip().zfill(6),
        period="daily",
        start_date=_compact_date(start),
        end_date=_compact_date(end),
        adjust=adjust,
        cfg=cfg,
    )


def convert_daily(
    qfq_df: pd.DataFrame | None,
    raw_df: pd.DataFrame | None,
    symbol: str | None = None,
    limit_fn: Callable[[float, str], tuple[float, float]] | None = None,
) -> pd.DataFrame:
    """AKShare 日线原始表 → 契约 §3.1 的 ``daily_bars`` DataFrame（纯函数）。

    参数
    ----
    qfq_df: ``adjust="qfq"`` 的原始表（提供前复权收盘 → ``adj_close``）
    raw_df: ``adjust=""`` 的原始表（提供不复权 OHLC / 涨跌额 / 成交量手数）
    symbol: 6 位代码；缺省从原始表 ``股票代码`` 列取
    limit_fn: 涨跌停价函数 ``(pre_close, symbol) -> (up, down)``；缺省自动解析
    """
    base = raw_df if raw_df is not None and len(raw_df) else qfq_df
    if base is None or len(base) == 0:
        return models.empty_daily()
    if not isinstance(base, pd.DataFrame):
        raise TypeError(f"convert_daily 需要 DataFrame，收到 {type(base)!r}")

    limit_fn = limit_fn or resolve_limit_fn()

    df = base.copy()
    df = df.rename(columns={k: v for k, v in _DAILY_RENAME.items() if k in df.columns})
    if "trade_date" not in df.columns:
        raise ValueError("AKShare 日线原始表缺少『日期』列")
    for col in ("open", "high", "low", "close", "volume", "amount", "pct_chg", "chg", "turnover_rate"):
        if col not in df.columns:
            df[col] = float("nan")

    # symbol：参数优先，其次原始表『股票代码』列
    if symbol is None and "股票代码" in df.columns and len(df):
        symbol = str(df["股票代码"].iloc[0])
    if symbol is None:
        raise ValueError("convert_daily 无法确定 symbol（请传参或保留『股票代码』列）")
    symbol = str(symbol).strip().zfill(6)

    # 不复权 OHLC / 成交量（手 → 股）/ 涨跌额
    for col in ("open", "high", "low", "close", "amount", "pct_chg", "turnover_rate"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0) * 100.0
    df["chg"] = pd.to_numeric(df["chg"], errors="coerce")
    df["pre_close"] = (df["close"] - df["chg"]).round(2)
    df["pct_chg"] = df["pct_chg"].fillna(
        (df["close"] / df["pre_close"].replace(0, float("nan")) - 1.0) * 100.0
    )

    # 前复权收盘 merge（qfq 的『收盘』即 adj_close）
    if qfq_df is not None and len(qfq_df) and "日期" in qfq_df.columns:
        adj = qfq_df.copy()
        adj = adj.rename(columns={"日期": "trade_date", "收盘": "adj_close"})
        adj["trade_date"] = pd.to_datetime(adj["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        if "adj_close" in adj.columns:
            adj["adj_close"] = pd.to_numeric(adj["adj_close"], errors="coerce")
            adj = adj[["trade_date", "adj_close"]].dropna(subset=["trade_date"]).drop_duplicates("trade_date")
        else:  # 防御：异常的 qfq 返回（无收盘列）
            logger.warning("%s 前复权数据缺『收盘』列，adj_close 回退为不复权收盘", symbol)
            adj = None
        if adj is not None:
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
            df = df.merge(adj, on="trade_date", how="left", suffixes=("", "_qfq"))
        else:
            df["adj_close"] = df["close"]
    else:
        logger.warning("%s 无前复权数据，adj_close 回退为不复权收盘", symbol)
        df["adj_close"] = df["close"]

    # 复权因子 = 前复权收盘 / 不复权收盘
    df["adj_factor"] = 1.0
    valid = df["close"].abs() > 1e-12
    df.loc[valid, "adj_factor"] = (df.loc[valid, "adj_close"] / df.loc[valid, "close"]).round(8)
    df["adj_factor"] = df["adj_factor"].fillna(1.0)

    # 涨跌停价（契约 §6）与停牌标记
    limits = [
        limit_fn(float(pre), symbol) if pd.notna(pre) else (float("nan"), float("nan"))
        for pre in df["pre_close"].tolist()
    ]
    df["limit_up"] = [x[0] for x in limits]
    df["limit_down"] = [x[1] for x in limits]
    df["is_suspended"] = (pd.to_numeric(df["volume"], errors="coerce").fillna(0) <= 0).astype("int64")
    df["symbol"] = symbol
    df["turnover_rate"] = df["turnover_rate"].fillna(0.0)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["trade_date"])

    return normalize_daily(df[list(DAILY_COLUMNS)])


def fetch_daily(symbol: str, start: str, end: str, cfg: dict | None = None) -> pd.DataFrame:
    """拉单只股票日线并完成契约 §3.1 转换（网络调用，测试替身入口）。

    主源为契约 §11 的东方财富 ``stock_zh_a_hist_em``；当东财 push2 被限流/不可用时，
    自动降级到同一 AKShare 库的新浪日线接口（``data.daily_source`` 可强制
    ``em`` / ``sina`` / ``auto``）。结果 DataFrame 的 ``attrs["source"]`` 标明实际来源。
    """
    source_pref = preferred_source(cfg, "daily")
    em_error: Exception | None = None
    if source_pref in ("auto", "em"):
        try:
            raw = fetch_daily_raw(symbol, start, end, adjust="", cfg=cfg)
            try:
                qfq = fetch_daily_raw(symbol, start, end, adjust="qfq", cfg=cfg)
            except DataFetchError as exc:  # 前复权失败时降级，日线本身仍可用
                logger.warning("%s 前复权数据拉取失败，adj_close 回退为不复权：%s", symbol, exc)
                qfq = pd.DataFrame()
            frame = convert_daily(qfq, raw, symbol=symbol, limit_fn=resolve_limit_fn(cfg))
            if len(frame):
                frame.attrs["source"] = "em"
                return frame
            em_error = DataFetchError(f"东财接口未返回 {symbol} 日线数据")
        except DataFetchError as exc:
            em_error = exc
        if source_pref == "em":
            raise em_error if em_error is not None else DataFetchError("东财日线获取失败")
        logger.warning(
            "%s 东财日线不可用（%s 限流/中断），自动降级 AKShare 新浪接口",
            symbol,
            _LIMIT_HINT,
        )
    try:
        frame = fetch_daily_sina(symbol, start, end, cfg)
    except Exception as exc:  # noqa: BLE001 - 汇总主/备源错误
        raise DataFetchError(
            f"{symbol} 日线获取失败（东财：{em_error}；新浪：{exc}）"
        ) from exc
    frame.attrs["source"] = "sina"
    return frame


def sync_daily(
    symbols: list[str],
    start: str,
    end: str,
    cfg: dict,
) -> dict[str, int]:
    """批量同步日线到 SQLite（幂等），返回 ``{symbol: 写入行数}``。

    单个 symbol 失败只记 0 并 warning，不中断整体（契约 §11）。
    """
    cfg = cfg or {}
    db = _db_path(cfg)
    init_db(db)
    start_iso = _iso_date(start, default=(cfg.get("data") or {}).get("daily_start", "2022-01-01"))
    end_iso = _iso_date(end, default=date.today().isoformat())
    stats: dict[str, int] = {}
    fetched_any = False
    errors: dict[str, str] = {}
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().zfill(6)
        try:
            frame = fetch_daily(symbol, start_iso, end_iso, cfg=cfg)
            rows = upsert_daily(frame, db) if len(frame) else 0
            stats[symbol] = rows
            fetched_any = True
            source = str(frame.attrs.get("source", "em"))
            LAST_SOURCES[symbol] = source
            if source != "em":
                logger.warning("sync_daily %s 使用备用数据源 %s（东财 push2 不可用）", symbol, source)
            logger.info(
                "sync_daily %s: %d 行（%s ~ %s，source=%s）",
                symbol,
                rows,
                start_iso,
                end_iso,
                source,
            )
        except Exception as exc:  # noqa: BLE001 - 单 symbol 失败不中断整体
            stats[symbol] = 0
            errors[symbol] = str(exc)
            logger.warning("sync_daily %s 失败：%s", symbol, exc)
    if symbols and not fetched_any and errors:
        detail = "; ".join(f"{s}: {e}" for s, e in errors.items())
        raise DataFetchError(f"全部 {len(errors)} 个 symbol 日线抓取失败（{detail}）")
    return stats


# ---------------------------------------------------------------------------
# 分钟线
# ---------------------------------------------------------------------------
_MINUTE_RENAME = {
    "时间": "ts",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
}


def fetch_minute_raw(
    symbol: str,
    period: int,
    start: str,
    end: str,
    adjust: str = "",
    cfg: dict | None = None,
) -> pd.DataFrame:
    """拉单只股票分钟线原始 DataFrame（网络调用，测试替身入口）。"""
    ak = _akshare()
    return _call_akshare(
        ak.stock_zh_a_hist_min_em,
        symbol=str(symbol).strip().zfill(6),
        period=str(period),
        start_date=str(start),
        end_date=str(end),
        adjust=adjust,
        cfg=cfg,
    )


def convert_minute(
    raw_df: pd.DataFrame | None,
    symbol: str | None = None,
    period: int = 5,
) -> pd.DataFrame:
    """AKShare 分钟原始表 → 契约 §3.2 的 ``minute_bars`` DataFrame（纯函数）。

    东财口径：``时间`` 列、成交量单位「手」。
    """
    if raw_df is None or len(raw_df) == 0:
        return models.empty_minute()
    if not isinstance(raw_df, pd.DataFrame):
        raise TypeError(f"convert_minute 需要 DataFrame，收到 {type(raw_df)!r}")

    df = raw_df.copy()
    df = df.rename(columns={k: v for k, v in _MINUTE_RENAME.items() if k in df.columns})
    if "ts" not in df.columns:
        raise ValueError("AKShare 分钟原始表缺少『时间』列")

    if symbol is None and "股票代码" in df.columns and len(df):
        symbol = str(df["股票代码"].iloc[0])
    if symbol is None:
        raise ValueError("convert_minute 无法确定 symbol（请传参）")
    df["symbol"] = str(symbol).strip().zfill(6)
    df["period"] = int(period)

    for col in ("open", "high", "low", "close", "amount"):
        if col not in df.columns:
            df[col] = float("nan")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0) * 100.0
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")

    return normalize_minute(df[list(MINUTE_COLUMNS)])


def fetch_minute_sina_raw(
    symbol: str,
    period: int,
    cfg: dict | None = None,
) -> pd.DataFrame:
    """新浪分钟线原始表（网络调用，测试替身入口）。

    返回列 ``day/open/high/low/close/volume(股)/amount``，通常只含最近数个交易日。
    """
    ak = _akshare()
    return _call_akshare(
        ak.stock_zh_a_hist_min_sina,
        symbol=_sina_symbol(symbol),
        period=str(period),
        adjust="",
        cfg=cfg,
    )


def convert_minute_sina(
    raw_df: pd.DataFrame | None,
    symbol: str | None = None,
    period: int = 5,
) -> pd.DataFrame:
    """新浪分钟原始表 → 契约 §3.2（纯函数）。成交量已是股，不再 ×100。"""
    if raw_df is None or len(raw_df) == 0:
        return models.empty_minute()
    if not isinstance(raw_df, pd.DataFrame):
        raise TypeError(f"convert_minute_sina 需要 DataFrame，收到 {type(raw_df)!r}")

    df = raw_df.copy().rename(columns={"day": "ts"})
    if "ts" not in df.columns:
        raise ValueError("新浪分钟原始表缺少 day 列")
    if symbol is None:
        raise ValueError("convert_minute_sina 需要 symbol")
    df["symbol"] = str(symbol).strip().zfill(6)
    df["period"] = int(period)
    for col in ("open", "high", "low", "close", "amount"):
        if col not in df.columns:
            df[col] = float("nan")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).round().astype("int64")
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    return normalize_minute(df[list(MINUTE_COLUMNS)])


def fetch_minute(
    symbol: str, period: int, start: str, end: str, cfg: dict | None = None
) -> pd.DataFrame:
    """拉分钟线并转换；东财不可用时自动降级新浪接口（``data.minute_source`` 可强制）。"""
    source_pref = preferred_source(cfg, "minute")
    em_error: Exception | None = None
    if source_pref in ("auto", "em"):
        try:
            frame = convert_minute(
                fetch_minute_raw(symbol, period, start, end, cfg=cfg),
                symbol=symbol,
                period=period,
            )
            if len(frame):
                frame.attrs["source"] = "em"
                return frame
            em_error = DataFetchError(f"东财接口未返回 {symbol} {period} 分钟线")
        except DataFetchError as exc:
            em_error = exc
        if source_pref == "em":
            raise em_error if em_error is not None else DataFetchError("东财分钟线获取失败")
        logger.warning(
            "%s %d 分钟线东财不可用（%s 限流/中断），自动降级 AKShare 新浪接口",
            symbol,
            period,
            _LIMIT_HINT,
        )
    try:
        frame = convert_minute_sina(fetch_minute_sina_raw(symbol, period, cfg=cfg), symbol=symbol, period=period)
    except Exception as exc:  # noqa: BLE001
        raise DataFetchError(f"{symbol} 分钟线获取失败（东财：{em_error}；新浪：{exc}）") from exc
    # 新浪只返回最近窗口，按请求区间裁剪
    try:
        start_ts = pd.Timestamp(str(start)).strftime("%Y-%m-%d %H:%M")
        end_ts = pd.Timestamp(str(end)).strftime("%Y-%m-%d %H:%M")
        frame = frame[(frame["ts"] >= start_ts) & (frame["ts"] <= end_ts)].reset_index(drop=True)
    except (TypeError, ValueError):
        pass
    if len(frame) == 0:
        raise DataFetchError(f"新浪接口在 {start} ~ {end} 无 {symbol} 分钟线")
    frame.attrs["source"] = "sina"
    return frame


def sync_minute(symbols: list[str], period: int, cfg: dict) -> dict[str, int]:
    """同步最近窗口的分钟线到 SQLite（幂等），返回 ``{symbol: 写入行数}``。"""
    cfg = cfg or {}
    db = _db_path(cfg)
    init_db(db)
    period = int(period)
    if period not in MINUTE_WINDOW_DAYS:
        raise ValueError(f"period 必须是 {sorted(MINUTE_WINDOW_DAYS)}，收到 {period}")
    window = MINUTE_WINDOW_DAYS[period]
    today = date.today()
    start = (today - timedelta(days=window)).strftime("%Y-%m-%d 09:30:00")
    end = f"{today.isoformat()} 15:00:00"
    stats: dict[str, int] = {}
    fetched_any = False
    errors: dict[str, str] = {}
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().zfill(6)
        try:
            frame = fetch_minute(symbol, period, start, end, cfg=cfg)
            rows = upsert_minute(frame, db) if len(frame) else 0
            stats[symbol] = rows
            fetched_any = True
            source = str(frame.attrs.get("source", "em"))
            LAST_SOURCES[symbol] = source
            logger.info("sync_minute %s/%dm: %d 行（source=%s）", symbol, period, rows, source)
        except Exception as exc:  # noqa: BLE001
            stats[symbol] = 0
            errors[symbol] = str(exc)
            logger.warning("sync_minute %s/%dm 失败：%s", symbol, period, exc)
    if symbols and not fetched_any and errors:
        detail = "; ".join(f"{s}: {e}" for s, e in errors.items())
        raise DataFetchError(f"全部 {len(errors)} 个 symbol 分钟线抓取失败（{detail}）")
    return stats
