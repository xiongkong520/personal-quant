"""SQLite 存储层：建表、幂等 UPSERT、读取（契约 §3、§9）。

数据库路径默认 ``data/db/quant.db``，由 CLI 通过 ``config.cfg_path(cfg, "data.db")``
解析后传入。所有写操作幂等：重复抓同一段行情不会产生重复行。

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
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd

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

# ---------------------------------------------------------------------------
# DDL（契约 §3.1 / §3.2 / §3.3）
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol        TEXT    NOT NULL,
    trade_date    TEXT    NOT NULL,
    open          REAL,
    high          REAL,
    low           REAL,
    close         REAL,
    pre_close     REAL,
    volume        INTEGER,
    amount        REAL,
    pct_chg       REAL,
    turnover_rate REAL    DEFAULT 0,
    adj_factor    REAL    DEFAULT 1.0,
    adj_close     REAL,
    limit_up      REAL,
    limit_down    REAL,
    is_suspended  INTEGER DEFAULT 0,
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_trade_date ON daily_bars(trade_date);
CREATE INDEX IF NOT EXISTS idx_daily_symbol ON daily_bars(symbol);

CREATE TABLE IF NOT EXISTS minute_bars (
    symbol TEXT    NOT NULL,
    period INTEGER NOT NULL,
    ts     TEXT    NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL,
    volume INTEGER,
    amount REAL,
    PRIMARY KEY (symbol, period, ts)
);
CREATE INDEX IF NOT EXISTS idx_minute_ts ON minute_bars(ts);

CREATE TABLE IF NOT EXISTS signals (
    signal_id       TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    asof            TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    price           REAL,
    direction       TEXT,
    prob_up         REAL,
    prob_flat       REAL,
    prob_down       REAL,
    confidence      REAL,
    horizon         TEXT,
    expires_at      TEXT,
    model_version   TEXT,
    top_features    TEXT,
    source          TEXT,
    status          TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_asof ON signals(symbol, asof);
CREATE INDEX IF NOT EXISTS idx_signals_trade_date ON signals(trade_date);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
"""

_TABLES = ("daily_bars", "minute_bars", "signals")


# ---------------------------------------------------------------------------
# 连接与建表
# ---------------------------------------------------------------------------
@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """打开 SQLite 连接（自动建父目录），成功提交、异常回滚。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA synchronous=NORMAL")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db(db_path: str | Path) -> None:
    """创建 §3.1–§3.3 三张表及索引；可重复调用（IF NOT EXISTS）。"""
    with connect(db_path) as con:
        con.executescript(_SCHEMA_SQL)


def get_table_names(db_path: str | Path) -> list[str]:
    """列出库内用户表（测试/排查用）。"""
    with connect(db_path) as con:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]


# ---------------------------------------------------------------------------
# 幂等 UPSERT
# ---------------------------------------------------------------------------
def _upsert_sql(table: str, columns: tuple[str, ...], pk: tuple[str, ...]) -> str:
    cols = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c not in pk)
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
        f"ON CONFLICT({', '.join(pk)}) DO UPDATE SET {updates}"
    )


_DAILY_UPSERT = _upsert_sql("daily_bars", DAILY_COLUMNS, ("symbol", "trade_date"))
_MINUTE_UPSERT = _upsert_sql("minute_bars", MINUTE_COLUMNS, ("symbol", "period", "ts"))
_SIGNAL_UPSERT = _upsert_sql("signals", SIGNAL_COLUMNS, ("signal_id",))


def upsert_daily(df: pd.DataFrame, db_path: str | Path) -> int:
    """幂等写入日线，返回本次提交的行数（= len(df)）。"""
    init_db(db_path)
    frame = normalize_daily(df)
    rows = frame[list(DAILY_COLUMNS)].itertuples(index=False, name=None)
    payload = [tuple(_native(v) for v in row) for row in rows]
    with connect(db_path) as con:
        con.executemany(_DAILY_UPSERT, payload)
    return len(payload)


def upsert_minute(df: pd.DataFrame, db_path: str | Path) -> int:
    """幂等写入分钟线，返回本次提交的行数。"""
    init_db(db_path)
    frame = normalize_minute(df)
    rows = frame[list(MINUTE_COLUMNS)].itertuples(index=False, name=None)
    payload = [tuple(_native(v) for v in row) for row in rows]
    with connect(db_path) as con:
        con.executemany(_MINUTE_UPSERT, payload)
    return len(payload)


def _native(value: Any) -> Any:
    """pandas / numpy 标量 → sqlite3 可写原生类型。"""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    # pandas 3 的字符串列可能给 str 子类，统一转 Python str
    if isinstance(value, str):
        return str(value)
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except (ValueError, AttributeError):
            pass
    return value


# ---------------------------------------------------------------------------
# 日线 / 分钟线读取
# ---------------------------------------------------------------------------
def _date_bounds(
    start: str | None, end: str | None
) -> tuple[str | None, str | None]:
    def _norm(value: str | None) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        return pd.Timestamp(str(value).strip()).strftime("%Y-%m-%d")

    return _norm(start), _norm(end)


def _daily_where(
    symbols: Iterable[str], start: str | None, end: str | None
) -> tuple[str, list[Any]]:
    syms = [str(s).strip().zfill(6) for s in symbols if str(s).strip()]
    clauses: list[str] = []
    params: list[Any] = []
    if syms:
        clauses.append(f"symbol IN ({', '.join('?' for _ in syms)})")
        params.extend(syms)
    s_start, s_end = _date_bounds(start, end)
    if s_start:
        clauses.append("trade_date >= ?")
        params.append(s_start)
    if s_end:
        clauses.append("trade_date <= ?")
        params.append(s_end)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _coerce_daily_read(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return empty_daily()
    out = normalize_daily(df)
    return out[list(DAILY_COLUMNS)]


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
    where, params = _daily_where([symbol], start, end)
    with connect(db_path) as con:
        df = pd.read_sql_query(
            f"SELECT {', '.join(DAILY_COLUMNS)} FROM daily_bars{where}", con, params=params
        )
    return _coerce_daily_read(df)


def read_daily_multi(
    symbols: list[str],
    db_path: str | Path,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """读多只股票日线，按 ``(symbol, trade_date)`` 升序。"""
    if not symbols:
        return empty_daily()
    where, params = _daily_where(symbols, start, end)
    with connect(db_path) as con:
        df = pd.read_sql_query(
            f"SELECT {', '.join(DAILY_COLUMNS)} FROM daily_bars{where}", con, params=params
        )
    return _coerce_daily_read(df)


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
    clauses = ["symbol = ?", "period = ?"]
    params: list[Any] = [str(symbol).strip().zfill(6), int(period)]
    if start:
        clauses.append("ts >= ?")
        params.append(pd.Timestamp(str(start)).strftime("%Y-%m-%d %H:%M"))
    if end:
        clauses.append("ts <= ?")
        params.append(pd.Timestamp(str(end)).strftime("%Y-%m-%d %H:%M"))
    sql = (
        f"SELECT {', '.join(MINUTE_COLUMNS)} FROM minute_bars "
        f"WHERE {' AND '.join(clauses)} ORDER BY ts ASC"
    )
    with connect(db_path) as con:
        df = pd.read_sql_query(sql, con, params=params)
    return _coerce_minute_read(df)


# ---------------------------------------------------------------------------
# 信号写入 / 读取（契约 §3.3、§4）
# ---------------------------------------------------------------------------
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
    init_db(db_path)
    prepared = _prepare_signal(sig)
    payload = tuple(prepared[col] for col in SIGNAL_COLUMNS)
    with connect(db_path) as con:
        con.execute(_SIGNAL_UPSERT, payload)


def _row_to_signal(row: sqlite3.Row) -> dict[str, Any]:
    data = {col: row[col] for col in SIGNAL_COLUMNS}
    raw = data.get("top_features")
    if isinstance(raw, str) and raw:
        try:
            data["top_features"] = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("信号 %s 的 top_features 不是合法 JSON", data.get("signal_id"))
    elif raw is None:
        data["top_features"] = []
    return data


def read_signals(db_path: str | Path, limit: int = 200) -> list[dict]:
    """按时间倒序读信号历史（``asof`` desc），默认最近 200 条。"""
    limit = max(0, int(limit))
    if limit == 0:
        return []
    sql = (
        f"SELECT {', '.join(SIGNAL_COLUMNS)} FROM signals "
        "ORDER BY asof DESC, created_at DESC LIMIT ?"
    )
    with connect(db_path) as con:
        rows = con.execute(sql, (limit,)).fetchall()
    return [_row_to_signal(r) for r in rows]


def read_latest_signals(db_path: str | Path, symbols: list[str]) -> list[dict]:
    """每只股票取 ``asof`` 最新的一条信号（无信号的股票不出现在结果里）。"""
    syms = [str(s).strip().zfill(6) for s in symbols if str(s).strip()]
    if not syms:
        return []
    marks = ", ".join("?" for _ in syms)
    sql = f"""
        SELECT {', '.join(SIGNAL_COLUMNS)} FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY symbol ORDER BY asof DESC, created_at DESC
            ) AS _rn
            FROM signals WHERE symbol IN ({marks})
        ) WHERE _rn = 1
        ORDER BY symbol
    """
    with connect(db_path) as con:
        rows = con.execute(sql, tuple(syms)).fetchall()
    return [_row_to_signal(r) for r in rows]
