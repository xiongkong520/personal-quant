"""Parquet 行情存储：原子写、增量合并、DuckDB ``read_parquet`` 查询（契约 §3.1 / §3.2）。

目录约定（由 ``store.parquet_root(db_path)`` 推导，见 ``quant.data.store``）::

    <parquet_dir>/daily_bars/symbol=<code>/daily_bars.parquet
    <parquet_dir>/minute_bars/symbol=<code>/period=<p>/minute_bars.parquet

要点
----
- **增量 upsert**：读旧文件 → concat 新行 → 按主键去重（``keep="last"`` 保留新值）
  → 排序 → 写临时文件 → ``os.replace`` 原子替换（进程内再加 ``flock`` 写锁）；
- **查询走 DuckDB**：``read_parquet([...], union_by_name=true)``，多票用文件列表，
  目录 hunk 约定为 hive 风格（``symbol=`` / ``period=``），但文件内已带 ``symbol`` /
  ``period`` 列（契约 §3.1/§3.2），所以默认关闭 hive_partitioning 直接用文件列；
- 文件不存在 = 无数据，返回带正确列/dtype 的空 DataFrame（调用方无需特判）；
- 行情只读不删：``SYMBOLS`` 级文件相互独立，抓单票失败不影响其它票。
"""
from __future__ import annotations

import fcntl
import logging
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import duckdb
import pandas as pd

from quant.data.models import (
    DAILY_COLUMNS,
    DAILY_INT_COLUMNS,
    DAILY_STR_COLUMNS,
    MINUTE_COLUMNS,
    MINUTE_INT_COLUMNS,
    MINUTE_STR_COLUMNS,
    empty_daily,
    empty_minute,
    normalize_daily,
    normalize_minute,
)

logger = logging.getLogger(__name__)

DAILY_DIR = "daily_bars"
MINUTE_DIR = "minute_bars"
DAILY_FILENAME = "daily_bars.parquet"
MINUTE_FILENAME = "minute_bars.parquet"

_DAILY_KEYS: tuple[str, ...] = ("symbol", "trade_date")
_MINUTE_KEYS: tuple[str, ...] = ("symbol", "period", "ts")


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def daily_dir(root: str | Path) -> Path:
    return Path(root) / DAILY_DIR


def minute_dir(root: str | Path) -> Path:
    return Path(root) / MINUTE_DIR


def daily_path(root: str | Path, symbol: str) -> Path:
    """单票日线 Parquet 路径：``daily_bars/symbol=<code>/daily_bars.parquet``。"""
    sym = str(symbol).strip().zfill(6)
    return daily_dir(root) / f"symbol={sym}" / DAILY_FILENAME


def minute_path(root: str | Path, symbol: str, period: int | str) -> Path:
    """单票分钟线 Parquet 路径：``minute_bars/symbol=<code>/period=<p>/minute_bars.parquet``。"""
    sym = str(symbol).strip().zfill(6)
    return minute_dir(root) / f"symbol={sym}" / f"period={int(period)}" / MINUTE_FILENAME


def ensure_dirs(root: str | Path) -> Path:
    """建 ``daily_bars/`` 与 ``minute_bars/`` 目录（可重复调用）。"""
    base = Path(root)
    daily_dir(base).mkdir(parents=True, exist_ok=True)
    minute_dir(base).mkdir(parents=True, exist_ok=True)
    return base


def daily_glob(root: str | Path) -> str:
    """DuckDB 多票日线 glob（含 ``symbol=`` 分区目录）。"""
    return str(daily_dir(root) / "*" / DAILY_FILENAME)


def minute_glob(root: str | Path) -> str:
    """DuckDB 多票分钟线 glob（含 ``symbol=`` / ``period=`` 分区目录）。"""
    return str(minute_dir(root) / "*" / "*" / MINUTE_FILENAME)


def daily_symbols(root: str | Path) -> list[str]:
    """已落地的日线 symbol 列表（升序）。"""
    base = daily_dir(root)
    if not base.is_dir():
        return []
    out = []
    for child in sorted(base.glob("symbol=*")):
        if (child / DAILY_FILENAME).is_file():
            out.append(child.name.split("=", 1)[1])
    return out


def minute_partitions(root: str | Path) -> list[tuple[str, int]]:
    """已落地的分钟线 ``(symbol, period)`` 列表。"""
    base = minute_dir(root)
    if not base.is_dir():
        return []
    out: list[tuple[str, int]] = []
    for child in sorted(base.glob("symbol=*")):
        for sub in sorted(child.glob("period=*")):
            if (sub / MINUTE_FILENAME).is_file():
                try:
                    out.append((child.name.split("=", 1)[1], int(sub.name.split("=", 1)[1])))
                except ValueError:  # pragma: no cover - 异常目录名忽略
                    continue
    return out


def _existing(paths: Iterable[Path]) -> list[Path]:
    return [p for p in paths if p.is_file()]


# ---------------------------------------------------------------------------
# 原子写 / 合并
# ---------------------------------------------------------------------------
@contextmanager
def _write_lock(root: str | Path) -> Iterator[None]:
    """进程/线程级写锁（``flock``），避免并发 upsert 同票互相覆盖。"""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / ".write.lock"
    with open(lock_path, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - 极端环境不支持 flock
                pass


def atomic_write_parquet(frame: pd.DataFrame, path: str | Path) -> Path:
    """写临时文件后 ``os.replace``，保证读者永远看到完整的旧/新文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        frame.to_parquet(tmp, index=False, engine="pyarrow", compression="snappy")
        os.replace(tmp, target)
    finally:
        if tmp.exists():  # pragma: no cover - 正常路径已被 replace
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


def _merge(
    old: pd.DataFrame | None,
    new: pd.DataFrame,
    *,
    columns: tuple[str, ...],
    keys: tuple[str, ...],
) -> pd.DataFrame:
    """旧 + 新 → 按主键去重（新值覆盖旧值）→ 排序。"""
    frames: list[pd.DataFrame] = []
    if old is not None and len(old) > 0:
        frames.append(old)
    if new is not None and len(new) > 0:
        frames.append(new)
    if not frames:
        return pd.DataFrame(columns=list(columns))
    merged = pd.concat(frames, ignore_index=True)
    if merged.empty:
        return pd.DataFrame(columns=list(columns))
    missing = [c for c in columns if c not in merged.columns]
    if missing:
        raise ValueError(f"Parquet 数据缺少列: {missing}")
    merged = merged[list(columns)]
    merged = merged.drop_duplicates(subset=list(keys), keep="last")
    merged = merged.sort_values(list(keys), kind="stable").reset_index(drop=True)
    return merged


def _read_existing(path: Path, columns: tuple[str, ...]) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - 坏文件视为无旧数据，用新数据重建
        logger.warning("读取旧 Parquet 失败，按无旧数据处理（%s）：%s", path, exc)
        return None
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        logger.warning("旧 Parquet 缺列 %s，按无旧数据处理: %s", missing, path)
        return None
    return frame[list(columns)]


def write_daily(root: str | Path, df: pd.DataFrame) -> int:
    """增量写日线（每票一个文件）；返回本次写入的新行数（去重前）。"""
    frame = normalize_daily(df)
    if frame.empty:
        ensure_dirs(root)
        return 0
    written = 0
    with _write_lock(root):
        for symbol, group in frame.groupby("symbol", sort=False):
            path = daily_path(root, str(symbol))
            old = _read_existing(path, DAILY_COLUMNS)
            merged = _merge(old, group, columns=DAILY_COLUMNS, keys=_DAILY_KEYS)
            atomic_write_parquet(merged, path)
            written += len(group)
    return written


def write_minute(root: str | Path, df: pd.DataFrame) -> int:
    """增量写分钟线（symbol × period 一个文件）；返回本次写入的新行数。"""
    frame = normalize_minute(df)
    if frame.empty:
        ensure_dirs(root)
        return 0
    written = 0
    with _write_lock(root):
        for (symbol, period), group in frame.groupby(["symbol", "period"], sort=False):
            path = minute_path(root, str(symbol), int(period))
            old = _read_existing(path, MINUTE_COLUMNS)
            merged = _merge(old, group, columns=MINUTE_COLUMNS, keys=_MINUTE_KEYS)
            atomic_write_parquet(merged, path)
            written += len(group)
    return written


# ---------------------------------------------------------------------------
# DuckDB 查询
# ---------------------------------------------------------------------------
def _query_parquet(
    files: Sequence[Path],
    *,
    columns: tuple[str, ...],
    where: str = "",
    params: Sequence[object] = (),
    order_by: str = "",
) -> pd.DataFrame:
    """用 DuckDB ``read_parquet`` 查询一批 Parquet 文件（``union_by_name`` 容错列差异）。"""
    if not files:
        return pd.DataFrame(columns=list(columns))
    con = duckdb.connect()
    try:
        sql = (
            f"SELECT {', '.join(columns)} FROM read_parquet(?, union_by_name=true)"
        )
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        return con.execute(sql, [[str(p) for p in files], *params]).df()
    finally:
        con.close()


def _coerce_daily_read(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or len(frame) == 0:
        return empty_daily()
    out = frame[list(DAILY_COLUMNS)].copy()
    # DuckDB → pandas 可能给 int32/float32/object，统一回契约 dtype（空值先填充）
    out["symbol"] = out["symbol"].astype(str).str.zfill(6)
    out["trade_date"] = out["trade_date"].astype(str)
    for col in DAILY_COLUMNS:
        if col in DAILY_STR_COLUMNS:
            continue
        if col in DAILY_INT_COLUMNS:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).round().astype("int64")
        else:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    return out


def _coerce_minute_read(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or len(frame) == 0:
        return empty_minute()
    out = frame[list(MINUTE_COLUMNS)].copy()
    out["symbol"] = out["symbol"].astype(str).str.zfill(6)
    out["ts"] = out["ts"].astype(str)
    for col in MINUTE_COLUMNS:
        if col in MINUTE_STR_COLUMNS:
            continue
        if col in MINUTE_INT_COLUMNS:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).round().astype("int64")
        else:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    return out


def read_daily(
    root: str | Path,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """读单票日线（``trade_date`` 升序）；无文件返回契约列空表。"""
    sym = str(symbol).strip().zfill(6)
    path = daily_path(root, sym)
    if not path.is_file():
        return empty_daily()
    clauses: list[str] = []
    params: list[object] = []
    if start:
        clauses.append("trade_date >= ?")
        params.append(pd.Timestamp(str(start)).strftime("%Y-%m-%d"))
    if end:
        clauses.append("trade_date <= ?")
        params.append(pd.Timestamp(str(end)).strftime("%Y-%m-%d"))
    frame = _query_parquet(
        [path],
        columns=DAILY_COLUMNS,
        where=" AND ".join(clauses),
        params=params,
        order_by="trade_date ASC",
    )
    return _coerce_daily_read(frame)


def read_daily_multi(
    root: str | Path,
    symbols: Iterable[str],
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """读多票日线（按 ``symbol, trade_date`` 升序）；无文件返回契约列空表。"""
    syms = [str(s).strip().zfill(6) for s in symbols if str(s).strip()]
    files = _existing(daily_path(root, sym) for sym in syms)
    if not files:
        return empty_daily()
    clauses: list[str] = []
    params: list[object] = []
    if start:
        clauses.append("trade_date >= ?")
        params.append(pd.Timestamp(str(start)).strftime("%Y-%m-%d"))
    if end:
        clauses.append("trade_date <= ?")
        params.append(pd.Timestamp(str(end)).strftime("%Y-%m-%d"))
    frame = _query_parquet(
        files,
        columns=DAILY_COLUMNS,
        where=" AND ".join(clauses),
        params=params,
        order_by="symbol ASC, trade_date ASC",
    )
    return _coerce_daily_read(frame)


def read_minute(
    root: str | Path,
    symbol: str,
    period: int,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """读单票某周期分钟线（``ts`` 升序）；无文件返回契约列空表。"""
    sym = str(symbol).strip().zfill(6)
    path = minute_path(root, sym, period)
    if not path.is_file():
        return empty_minute()
    clauses: list[str] = []
    params: list[object] = []
    if start:
        clauses.append("ts >= ?")
        params.append(pd.Timestamp(str(start)).strftime("%Y-%m-%d %H:%M"))
    if end:
        clauses.append("ts <= ?")
        params.append(pd.Timestamp(str(end)).strftime("%Y-%m-%d %H:%M"))
    frame = _query_parquet(
        [path],
        columns=MINUTE_COLUMNS,
        where=" AND ".join(clauses),
        params=params,
        order_by="ts ASC",
    )
    return _coerce_minute_read(frame)


def query_parquet_glob(
    pattern: str,
    *,
    columns: str = "*",
    where: str = "",
    params: Sequence[object] = (),
    order_by: str = "",
) -> pd.DataFrame:
    """对 glob 匹配的一批 Parquet 跑 DuckDB 查询（迁移脚本核对行数等只读场景用）。"""
    con = duckdb.connect()
    try:
        literal = str(pattern).replace("'", "''")
        sql = f"SELECT {columns} FROM read_parquet('{literal}', union_by_name=true, hive_partitioning=false)"
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        return con.execute(sql, list(params)).df()
    finally:
        con.close()


__all__ = [
    "DAILY_COLUMNS",
    "MINUTE_COLUMNS",
    "atomic_write_parquet",
    "daily_dir",
    "daily_glob",
    "daily_path",
    "daily_symbols",
    "ensure_dirs",
    "minute_dir",
    "minute_glob",
    "minute_partitions",
    "minute_path",
    "query_parquet_glob",
    "read_daily",
    "read_daily_multi",
    "read_minute",
    "write_daily",
    "write_minute",
]
