"""本地存储层（契约 §3、§9）：**Parquet 存行情 + DuckDB 做查询/状态库**。

存储布局
--------
- 行情（只追加/幂等 UPSERT，不进 DuckDB）：

  - ``data/parquet/daily_bars/symbol=<code>/daily_bars.parquet``（契约 §3.1）
  - ``data/parquet/minute_bars/symbol=<code>/period=<p>/minute_bars.parquet``（§3.2）

- 状态（``signals``，以及 paper 模块自行管理的 ``paper_*`` 表）：

  - ``data/db/quant.duckdb``（配置键 ``data.db``）

Parquet 根目录默认由 DuckDB 路径推导：``.../db/quant.duckdb`` → ``.../parquet``；
其它位置（如测试临时库 ``tmp/quant.db``）→ ``tmp/parquet``。也可用环境变量
``QUANT_PARQUET_DIR`` 显式覆盖，便于迁移脚本/自定义配置。

对外签名（契约 §9，实现方不得改名）::

    init_db(db_path) -> None
    upsert_daily(df, db_path) -> int
    upsert_minute(df, db_path) -> int
    read_daily(symbol, db_path, start=None, end=None) -> pd.DataFrame
    read_daily_multi(symbols, db_path, start=None, end=None) -> pd.DataFrame
    read_minute(symbol, period, db_path, start=None, end=None) -> pd.DataFrame
    write_signal(sig, db_path) -> None
    read_signals(db_path, limit=200) -> list[dict]
    read_latest_signals(db_path, symbols) -> list[dict]

返回结构（列名/列顺序/dtype/排序、信号 dict 字段）与旧 SQLite 实现保持一致。
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import AbstractContextManager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd

from quant.data import db as duckdb_conn
from quant.data import parquet_store
from quant.data.models import (
    DAILY_COLUMNS,
    DIRECTIONS,
    HORIZONS,
    MINUTE_COLUMNS,
    SIGNAL_COLUMNS,
    SIGNAL_SOURCES,
    SIGNAL_STATUSES,
    empty_daily,
    empty_minute,
    normalize_daily,
    normalize_minute,
    now_iso,
    to_compact_asof,
)

logger = logging.getLogger(__name__)

#: Parquet 根目录环境变量（迁移脚本 / 自定义 data.parquet_dir 时用）
PARQUET_DIR_ENV = "QUANT_PARQUET_DIR"

# ---------------------------------------------------------------------------
# DuckDB 状态库 DDL（契约 §3.3；行情表在 Parquet 里，不建 DuckDB 表）
# ---------------------------------------------------------------------------
#: DuckDB 2.x 里 ``asof`` 是保留字（AS OF），建表/查询/写入统一加双引号。
def _q(column: str) -> str:
    return f'"{column}"'


_SIGNAL_COLUMNS_SQL = ", ".join(_q(c) for c in SIGNAL_COLUMNS)

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS signals (
    signal_id       TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    {_q("asof")}            TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    price           DOUBLE,
    direction       TEXT,
    prob_up         DOUBLE,
    prob_flat       DOUBLE,
    prob_down       DOUBLE,
    confidence      DOUBLE,
    horizon         TEXT,
    expires_at      TEXT,
    model_version   TEXT,
    top_features    TEXT,
    source          TEXT,
    status          TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_asof ON signals(symbol, {_q("asof")});
CREATE INDEX IF NOT EXISTS idx_signals_trade_date ON signals(trade_date);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
"""

# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def _configured_parquet_root(db_path: Path) -> Path | None:
    """若 ``db_path`` 正是 config 里的 ``data.db``，优先用 ``data.parquet_dir``。"""
    try:
        from quant.config import cfg_path, load_config  # noqa: PLC0415 - 避免模块级循环导入

        cfg = load_config()
        configured = (cfg.get("data") or {}).get("parquet_dir")
        if not configured:
            return None
        if Path(cfg_path(cfg, "data.db")).resolve() != db_path.resolve():
            return None
        return Path(cfg_path(cfg, "data.parquet_dir"))
    except Exception:  # noqa: BLE001 - 配置缺失/路径异常时退回约定推导
        return None


def parquet_root(db_path: str | Path) -> Path:
    """Parquet 根目录：环境变量 > config ``data.parquet_dir`` > 由 db 路径约定推导。

    - ``QUANT_PARQUET_DIR``（迁移脚本/多环境显式指定）优先；
    - 当 ``db_path`` 就是 config 里的 ``data.db`` 时，用 ``data.parquet_dir``
      （默认 ``data/parquet``）；
    - 否则按约定：``.../db/quant.duckdb`` → ``.../parquet``，其它 ``db.parent/parquet``
      （临时库测试即落 ``tmp/parquet``，互不干扰）。
    """
    env = os.environ.get(PARQUET_DIR_ENV)
    if env:
        return Path(env)
    db = Path(db_path)
    configured = _configured_parquet_root(db)
    if configured is not None:
        return configured
    if db.parent.name in ("db", "database"):
        return db.parent.parent / "parquet"
    return db.parent / "parquet"


def ensure_parquet_dirs(db_path: str | Path) -> Path:
    """建 ``daily_bars/`` 与 ``minute_bars/`` 目录（``init_db``/upsert 调用）。"""
    return parquet_store.ensure_dirs(parquet_root(db_path))


# ---------------------------------------------------------------------------
# 连接与建表
# ---------------------------------------------------------------------------
def connect(
    db_path: str | Path, read_only: bool = False
) -> AbstractContextManager[duckdb.DuckDBPyConnection]:
    """``quant.data.db.connect`` 的别名（连接为原生 DuckDB 连接，支持 ? / executemany / .df()）。"""
    return duckdb_conn.connect(db_path, read_only=read_only)


def init_db(db_path: str | Path) -> None:
    """建 DuckDB ``signals`` 表/索引 + Parquet 目录；可重复调用（幂等）。"""
    path = Path(db_path)
    ensure_parquet_dirs(path)
    with duckdb_conn.connect(path) as con:
        con.execute(_SCHEMA_SQL)


def get_table_names(db_path: str | Path) -> list[str]:
    """列出 DuckDB 状态库里的表（测试/排查用；行情表在 Parquet 里）。"""
    path = Path(db_path)
    try:
        with duckdb_conn.connect(path, read_only=True) as con:
            return duckdb_conn.table_names(con)
    except (FileNotFoundError, duckdb_conn.DatabaseUnavailable, duckdb.Error, OSError):
        return []


def get_storage_tables(db_path: str | Path) -> list[str]:
    """逻辑表清单：Parquet 行情表（有落地文件时）+ DuckDB 状态表。"""
    names = set(get_table_names(db_path))
    root = parquet_root(db_path)
    if any(parquet_store.daily_dir(root).glob("symbol=*")):
        names.add("daily_bars")
    if any(parquet_store.minute_dir(root).glob("symbol=*/period=*")):
        names.add("minute_bars")
    return sorted(names)


# ---------------------------------------------------------------------------
# 幂等 UPSERT（行情 → Parquet）
# ---------------------------------------------------------------------------
def upsert_daily(df: pd.DataFrame, db_path: str | Path) -> int:
    """幂等写入日线（合并进每票 Parquet），返回本次提交的行数（= len(df)）。"""
    frame = normalize_daily(df)
    root = parquet_root(db_path)
    if frame.empty:
        parquet_store.ensure_dirs(root)
        return 0
    parquet_store.write_daily(root, frame)
    return len(frame)


def upsert_minute(df: pd.DataFrame, db_path: str | Path) -> int:
    """幂等写入分钟线（合并进 symbol×period Parquet），返回本次提交的行数。"""
    frame = normalize_minute(df)
    root = parquet_root(db_path)
    if frame.empty:
        parquet_store.ensure_dirs(root)
        return 0
    parquet_store.write_minute(root, frame)
    return len(frame)


# ---------------------------------------------------------------------------
# 日线 / 分钟线读取（DuckDB read_parquet）
# ---------------------------------------------------------------------------
def _coerce_daily_read(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return empty_daily()
    return normalize_daily(df)[list(DAILY_COLUMNS)]


def _coerce_minute_read(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return empty_minute()
    return normalize_minute(df)[list(MINUTE_COLUMNS)]


def read_daily(
    symbol: str,
    db_path: str | Path,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """读单只股票日线，按 ``trade_date`` 升序；无数据返回带列的空表。"""
    frame = parquet_store.read_daily(parquet_root(db_path), symbol, start=start, end=end)
    return _coerce_daily_read(frame)


def read_daily_multi(
    symbols: list[str],
    db_path: str | Path,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """读多只股票日线，按 ``(symbol, trade_date)`` 升序。"""
    if not symbols:
        return empty_daily()
    frame = parquet_store.read_daily_multi(
        parquet_root(db_path), symbols, start=start, end=end
    )
    return _coerce_daily_read(frame)


def read_minute(
    symbol: str,
    period: int,
    db_path: str | Path,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """读单只股票某周期分钟线，按 ``ts`` 升序。

    ``start`` / ``end`` 接受 ``YYYY-MM-DD`` 或 ``YYYY-MM-DD HH:MM``。
    """
    frame = parquet_store.read_minute(
        parquet_root(db_path), symbol, period, start=start, end=end
    )
    return _coerce_minute_read(frame)


# ---------------------------------------------------------------------------
# 信号写入 / 读取（DuckDB 状态库；契约 §3.3、§4）
# ---------------------------------------------------------------------------
def _upsert_sql(table: str, columns: tuple[str, ...], pk: tuple[str, ...]) -> str:
    cols = ", ".join(_q(c) for c in columns)
    marks = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{_q(c)}=excluded.{_q(c)}" for c in columns if c not in pk
    )
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
        f"ON CONFLICT({', '.join(_q(c) for c in pk)}) DO UPDATE SET {updates}"
    )


_SIGNAL_UPSERT = _upsert_sql("signals", SIGNAL_COLUMNS, ("signal_id",))


def _signal_id_of(sig: dict[str, Any], asof: str) -> str:
    raw = sig.get("signal_id")
    if raw:
        return str(raw)
    trade_date = str(sig.get("trade_date") or asof[:10])
    return f"{sig['symbol']}-{trade_date}-{to_compact_asof(asof)}"


def _ensure_iso_tz(value: Any, default: str | None = None) -> str:
    """转成带时区的 ISO 字符串；无时区按 Asia/Shanghai 处理。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            default = now_iso()
        value = default
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Shanghai")
    return ts.isoformat(timespec="seconds")


def _probabilities(sig: dict[str, Any]) -> tuple[float, float, float]:
    nested = sig.get("prob")
    if isinstance(nested, dict):
        p = (nested.get("up"), nested.get("flat"), nested.get("down"))
    else:
        p = (sig.get("prob_up"), sig.get("prob_flat"), sig.get("prob_down"))
    try:
        probs = tuple(float(x) for x in p)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("signal 缺少 prob_up/prob_flat/prob_down（或 prob 字典）") from exc
    if any(x < 0 for x in probs):
        raise ValueError(f"概率不能为负: {probs}")
    total = sum(probs)
    if total <= 0:
        raise ValueError(f"概率之和必须为正: {probs}")
    if abs(total - 1.0) > 1e-6:
        probs = tuple(x / total for x in probs)
    return probs  # type: ignore[return-value]


def _prepare_signal(sig: dict[str, Any]) -> dict[str, Any]:
    """校验并补齐一条信号记录的 §3.3 字段。"""
    if not isinstance(sig, dict):
        raise TypeError(f"write_signal 需要 dict，收到 {type(sig)!r}")

    symbol = str(sig.get("symbol", "")).strip().zfill(6)
    if not symbol or symbol == "000000":
        raise ValueError("signal.symbol 必填")
    if len(symbol) != 6:
        raise ValueError(f"非法股票代码: {symbol!r}")

    asof = _ensure_iso_tz(sig.get("asof"))
    trade_date = str(sig.get("trade_date") or asof[:10]).strip()
    if not trade_date:
        raise ValueError("signal.trade_date 必填")

    price = sig.get("price")
    if price is None:
        raise ValueError("signal.price 必填")
    price = float(price)

    direction = str(sig.get("direction", "")).strip().lower()
    if direction not in DIRECTIONS:
        raise ValueError(f"signal.direction 必须是 {DIRECTIONS}，收到 {direction!r}")

    prob_up, prob_flat, prob_down = _probabilities(sig)
    # 契约 §3.3：confidence = max(prob)，保留 4 位；以概率为准，入参不一致时告警
    confidence = round(max(prob_up, prob_flat, prob_down), 4)
    provided_confidence = sig.get("confidence")
    if provided_confidence is not None and abs(float(provided_confidence) - confidence) > 1e-6:
        logger.warning(
            "signal.confidence=%s 与 max(prob)=%s 不一致，按契约取 max(prob)",
            provided_confidence,
            confidence,
        )

    horizon = str(sig.get("horizon") or "1d").strip()
    if horizon not in HORIZONS:
        raise ValueError(f"signal.horizon 目前只支持 {HORIZONS}，收到 {horizon!r}")

    expires_at = _ensure_iso_tz(
        sig.get("expires_at"),
        default=(pd.Timestamp(asof) + timedelta(days=1)).replace(hour=9, minute=30, second=0).isoformat(),
    )

    model_version = str(sig.get("model_version") or "").strip()
    if not model_version:
        raise ValueError("signal.model_version 必填（与 model.json 的 version 一致）")

    top_features = sig.get("top_features")
    if top_features is None:
        top_features = []
    if isinstance(top_features, str):
        try:
            parsed = json.loads(top_features)
            if not isinstance(parsed, list):
                raise ValueError
            top_features = parsed
        except (ValueError, json.JSONDecodeError):
            top_features = [top_features]
    if not isinstance(top_features, list):
        raise ValueError("signal.top_features 必须是 list（或 JSON 数组字符串）")
    if len(top_features) > 5:
        logger.warning("top_features %d 项超过契约上限 5，截断", len(top_features))
        top_features = top_features[:5]
    top_features_json = json.dumps(top_features, ensure_ascii=False)

    source = str(sig.get("source") or "local").strip().lower()
    if source not in SIGNAL_SOURCES:
        raise ValueError(f"signal.source 必须是 {SIGNAL_SOURCES}，收到 {source!r}")

    status = str(sig.get("status") or "ok").strip().lower()
    if status not in SIGNAL_STATUSES:
        raise ValueError(f"signal.status 必须是 {SIGNAL_STATUSES}，收到 {status!r}")

    created_at = _ensure_iso_tz(sig.get("created_at"), default=now_iso())

    prepared: dict[str, Any] = {
        "symbol": symbol,
        "asof": asof,
        "trade_date": trade_date,
        "price": price,
        "direction": direction,
        "prob_up": prob_up,
        "prob_flat": prob_flat,
        "prob_down": prob_down,
        "confidence": confidence,
        "horizon": horizon,
        "expires_at": expires_at,
        "model_version": model_version,
        "top_features": top_features_json,
        "source": source,
        "status": status,
        "created_at": created_at,
    }
    prepared["signal_id"] = _signal_id_of(sig | {"symbol": symbol}, asof)
    return prepared


def write_signal(sig: dict, db_path: str | Path) -> None:
    """幂等写入一条信号（同 signal_id 覆盖更新）。"""
    path = Path(db_path)
    prepared = _prepare_signal(sig)
    payload = [prepared[col] for col in SIGNAL_COLUMNS]
    init_db(path)
    with duckdb_conn.connect(path) as con:
        con.execute(_SIGNAL_UPSERT, payload)


def _row_to_signal(row: dict[str, Any]) -> dict[str, Any]:
    data = {col: row.get(col) for col in SIGNAL_COLUMNS}
    raw = data.get("top_features")
    if isinstance(raw, str) and raw:
        try:
            data["top_features"] = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("信号 %s 的 top_features 不是合法 JSON", data.get("signal_id"))
    elif raw is None:
        data["top_features"] = []
    return data


def _query_signals(
    db_path: str | Path, sql: str, params: Iterable[Any] = ()
) -> list[dict[str, Any]]:
    """只读查询 signals 表；库/表不存在或暂时被锁定时返回 []（API 容错）。"""
    path = Path(db_path)
    try:
        with duckdb_conn.connect(path, read_only=True) as con:
            if not duckdb_conn.table_exists(con, "signals"):
                return []
            return duckdb_conn.fetch_dicts(con, sql, list(params))
    except (FileNotFoundError, duckdb_conn.DatabaseUnavailable, duckdb.Error, OSError) as exc:
        logger.debug("读取 signals 失败（按空处理）: %s", exc)
        return []


def read_signals(db_path: str | Path, limit: int = 200) -> list[dict]:
    """按时间倒序读信号历史（``asof`` desc），默认最近 200 条。"""
    limit = max(0, int(limit))
    if limit == 0:
        return []
    sql = (
        f"SELECT {_SIGNAL_COLUMNS_SQL} FROM signals "
        f"ORDER BY {_q('asof')} DESC, created_at DESC LIMIT ?"
    )
    return [_row_to_signal(row) for row in _query_signals(db_path, sql, [limit])]


def read_latest_signals(db_path: str | Path, symbols: list[str]) -> list[dict]:
    """每只股票取 ``asof`` 最新的一条信号（无信号的股票不出现在结果里）。"""
    syms = [str(s).strip().zfill(6) for s in symbols if str(s).strip()]
    if not syms:
        return []
    marks = ", ".join("?" for _ in syms)
    sql = f"""
        SELECT {_SIGNAL_COLUMNS_SQL} FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY symbol ORDER BY {_q('asof')} DESC, created_at DESC
            ) AS _rn
            FROM signals WHERE symbol IN ({marks})
        ) WHERE _rn = 1
        ORDER BY symbol
    """
    return [_row_to_signal(row) for row in _query_signals(db_path, sql, syms)]


__all__ = [
    "DAILY_COLUMNS",
    "MINUTE_COLUMNS",
    "PARQUET_DIR_ENV",
    "connect",
    "ensure_parquet_dirs",
    "get_storage_tables",
    "get_table_names",
    "init_db",
    "parquet_root",
    "read_daily",
    "read_daily_multi",
    "read_latest_signals",
    "read_minute",
    "read_signals",
    "upsert_daily",
    "upsert_minute",
    "write_signal",
]
