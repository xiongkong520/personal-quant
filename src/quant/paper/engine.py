"""模拟盘引擎（契约 §3.4 / §6 / §9）。

核心行为（全部为「模拟」，不接券商、不下真实单）：
- 账户与状态存本地 SQLite 的 ``paper_*`` 表（契约 §3.4）；
- 信号来源为 ``signals`` 表里 ``status='ok'`` 且执行时点未过期的信号；
  T 日收盘后信号 → T+1 交易日 09:30 以开盘价 ± 滑点成交（开盘触及涨/跌停则拒单）；
- 手数、费用、滑点、T+1、涨跌停等规则全部调用 ``quant.paper.rules``（与回测共用同一实现）；
- 现金不允许为负、不允许超卖；所有被拒订单落库存 ``reject_reason``；
- 停牌或不存在的行情不成交，订单保持 pending 顺延到下一可交易日。

对外入口（契约 §9）：``init_account`` / ``advance`` / ``snapshot``。
``fill_pending_orders`` 为单测/回测调试用的低层入口。

advance 的「虚拟交易日」推进规则：
- 有历史净值时从最后一天的下一个交易日起推进；
- 首次推进时：优先从「最早信号对应的 T+1」开始回放；否则从最新行情日开始；
- 行情不够 ``days`` 天时，用工作日顺延补齐（未来无行情日仅按最后收盘价盯市，不产生成交）。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from quant.config import cfg_path
from quant.paper import rules

TZ = timezone(timedelta(hours=8))
ACCOUNT_ID = "default"
CURVE_LIMIT = 500   # snapshot /api/equity 默认最多返回的净值点数
ORDER_LIMIT = 100   # snapshot 返回的最近订单/成交条数
PENDING = "pending"
FILLED = "filled"
REJECTED = "rejected"
EXPIRED = "expired"

_DDL = """
CREATE TABLE IF NOT EXISTS paper_account (
    id TEXT PRIMARY KEY,
    cash REAL NOT NULL DEFAULT 0,
    created_at TEXT,
    initial_cash REAL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    symbol TEXT PRIMARY KEY,
    qty INTEGER NOT NULL DEFAULT 0,
    available_qty INTEGER NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0,
    last_price REAL NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    symbol TEXT,
    side TEXT,
    qty INTEGER,
    target_price REAL,
    status TEXT,
    reject_reason TEXT,
    created_at TEXT,
    filled_at TEXT,
    filled_price REAL,
    fee REAL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id TEXT PRIMARY KEY,
    order_id TEXT,
    symbol TEXT,
    side TEXT,
    qty INTEGER,
    price REAL,
    amount REAL,
    fee REAL,
    realized_pnl REAL,
    trade_date TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS paper_equity_curve (
    ts TEXT PRIMARY KEY,
    cash REAL,
    market_value REAL,
    equity REAL
);
"""


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def db_path(cfg: dict[str, Any]) -> Path:
    """从 cfg 取 SQLite 路径（契约 §3：data.db）。"""
    try:
        return Path(cfg_path(cfg, "data.db"))
    except (KeyError, TypeError) as exc:  # 兼容只传 data.db 的测试配置
        db = (cfg or {}).get("data", {}).get("db") if isinstance(cfg, dict) else None
        if db:
            return Path(str(db))
        raise ValueError("cfg 缺少 data.db 配置") from exc


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _connect(db: str | Path) -> sqlite3.Connection:
    path = Path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if _table_exists(conn, table) and column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def ensure_paper_tables(conn: sqlite3.Connection) -> None:
    """幂等建 paper_* 表；对旧库做「只增列」迁移。"""
    conn.executescript(_DDL)
    _add_column_if_missing(conn, "paper_account", "initial_cash", "REAL")
    _add_column_if_missing(conn, "paper_trades", "realized_pnl", "REAL")
    conn.commit()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_iso(text: Any) -> datetime | None:
    """解析契约占用的 ISO 时间（带时区；无时区按 +08:00；支持 Z 后缀）。"""
    if not text:
        return None
    raw = str(text).strip()
    try:
        if raw.endswith("Z"):
            dt = datetime.fromisoformat(raw[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def _exec_dt(trade_date: str) -> datetime:
    """虚拟交易日 09:30 开盘时点。"""
    day = date.fromisoformat(trade_date)
    return datetime(day.year, day.month, day.day, 9, 30, tzinfo=TZ)


def _initial_cash(cfg: dict[str, Any]) -> float:
    paper = (cfg or {}).get("paper") or {}
    return max(_as_float(paper.get("initial_cash"), 1_000_000.0), 0.0)


def _max_position_pct(cfg: dict[str, Any]) -> float:
    paper = (cfg or {}).get("paper") or {}
    pct = _as_float(paper.get("max_position_pct"), rules.MAX_POSITION_PCT)
    return min(max(pct, 0.0), 1.0)


def _limit_check(cfg: dict[str, Any]) -> bool:
    paper = (cfg or {}).get("paper") or {}
    return bool(paper.get("limit_check", True))


def _tick_round(cfg: dict[str, Any]) -> bool:
    paper = (cfg or {}).get("paper") or {}
    return bool(paper.get("tick_round", True))


def _weekdays_from(start: str, count: int) -> list[str]:
    """含 start 的往后数 count 个工作日（周一~周五，虚拟日历兜底）。"""
    out: list[str] = []
    try:
        day = date.fromisoformat(start)
    except ValueError:
        return out
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _weekdays_after(start: str, count: int) -> list[str]:
    """严格晚于 start 的 count 个工作日。"""
    try:
        day = date.fromisoformat(start) + timedelta(days=1)
    except ValueError:
        return []
    return _weekdays_from(day.isoformat(), count)


# ---------------------------------------------------------------------------
# 账户 / 持仓 / 行情 / 信号读取
# ---------------------------------------------------------------------------
def _load_account(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM paper_account WHERE id=?", (ACCOUNT_ID,)).fetchone()
    return dict(row) if row else None


def _insert_account(conn: sqlite3.Connection, cash: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO paper_account(id, cash, created_at, initial_cash) VALUES (?,?,?,?)",
        (ACCOUNT_ID, rules.round_cents(cash), _now_iso(), rules.round_cents(cash)),
    )


def _cash(conn: sqlite3.Connection) -> float:
    account = _load_account(conn)
    return rules.round_cents(_as_float(account.get("cash")) if account else 0.0)


def _set_cash(conn: sqlite3.Connection, cash: float) -> None:
    conn.execute(
        "UPDATE paper_account SET cash=? WHERE id=?", (rules.round_cents(cash), ACCOUNT_ID)
    )


def _get_position(conn: sqlite3.Connection, symbol: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM paper_positions WHERE symbol=?", (symbol,)).fetchone()
    return dict(row) if row else None


def _all_positions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM paper_positions ORDER BY symbol").fetchall()
    return [dict(row) for row in rows]


def _read_bar(conn: sqlite3.Connection, symbol: str, trade_date: str) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            "SELECT * FROM daily_bars WHERE symbol=? AND trade_date=?", (symbol, trade_date)
        ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def _bar_dates(conn: sqlite3.Connection, symbols: list[str]) -> list[str]:
    """deployed 日线里的全部交易日（升序）；无表/无数据返回 []。"""
    if not _table_exists(conn, "daily_bars"):
        return []
    try:
        if symbols:
            marks = ",".join("?" for _ in symbols)
            rows = conn.execute(
                f"SELECT DISTINCT trade_date FROM daily_bars WHERE symbol IN ({marks}) ORDER BY trade_date",
                tuple(symbols),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM daily_bars ORDER BY trade_date"
            ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0]) for row in rows if row[0]]


def _read_all_signals(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    """status='ok' 的信号按 trade_date 分组；无 signals 表返回 {}。"""
    if not _table_exists(conn, "signals"):
        return {}
    try:
        rows = conn.execute("SELECT * FROM signals WHERE status='ok'").fetchall()
    except sqlite3.Error:
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        symbol = str(item.get("symbol") or "")
        if symbols and symbol not in symbols:
            continue
        trade_date = str(item.get("trade_date") or "")
        if not trade_date:
            continue
        grouped.setdefault(trade_date, []).append(item)
    return grouped


def _read_signal_dates(conn: sqlite3.Connection, symbols: list[str]) -> list[str]:
    if not _table_exists(conn, "signals"):
        return []
    try:
        if symbols:
            marks = ",".join("?" for _ in symbols)
            rows = conn.execute(
                f"SELECT DISTINCT trade_date FROM signals WHERE status='ok' AND symbol IN ({marks}) ORDER BY trade_date",
                tuple(symbols),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM signals WHERE status='ok' ORDER BY trade_date"
            ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0]) for row in rows if row[0]]


def _last_equity_ts(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT ts FROM paper_equity_curve ORDER BY ts DESC LIMIT 1").fetchone()
    return str(row[0]) if row and row[0] else None


def _limit_prices_for_bar(symbol: str, bar: dict[str, Any]) -> tuple[float, float]:
    """优先用日线里的涨跌停列，缺失时按 pre_close + 板块规则现算。"""
    limit_up = _as_float(bar.get("limit_up"))
    limit_down = _as_float(bar.get("limit_down"))
    if limit_up > 0 and limit_down > 0:
        return limit_up, limit_down
    pre_close = _as_float(bar.get("pre_close"))
    if pre_close <= 0:
        pre_close = _as_float(bar.get("close"))
    if pre_close <= 0:
        return 0.0, 0.0
    is_st = bool(_as_int(bar.get("is_st")))
    return rules.limit_prices(pre_close, symbol, is_st)


def _current_equity(conn: sqlite3.Connection) -> float:
    """用现金 + 持仓最新价估算账户权益（用于买入仓位上限）。"""
    cash = _cash(conn)
    market_value = 0.0
    for pos in _all_positions(conn):
        price = _as_float(pos.get("last_price")) or _as_float(pos.get("avg_cost"))
        market_value += _as_int(pos.get("qty")) * price
    return rules.round_cents(cash + market_value)


def _buy_capacity(
    conn: sqlite3.Connection, cfg: dict[str, Any], symbol: str, price: float
) -> tuple[int, str | None]:
    """按「单票仓位上限 + 现金（含费用）」计算可买整手数。"""
    lot = rules.lot_size(cfg)
    if price <= 0:
        return 0, rules.REJECT_NO_PRICE
    cash = _cash(conn)
    equity = _current_equity(conn)
    pos = _get_position(conn, symbol)
    current_value = _as_int(pos.get("qty")) * price if pos else 0.0
    cap_value = equity * _max_position_pct(cfg)
    cap_qty = rules.round_lot((cap_value - current_value) / price, lot)
    cash_qty = rules.max_affordable_qty(cash, price, cfg)
    qty = min(cap_qty, cash_qty)
    if qty > 0:
        return qty, None
    if cap_qty <= 0:
        return 0, rules.REJECT_POSITION_LIMIT
    if cash_qty <= 0:
        return 0, rules.REJECT_INSUFFICIENT_CASH
    return 0, rules.REJECT_LOT_SIZE


def _estimate_fill_price(
    conn: sqlite3.Connection, symbol: str, side: str, cfg: dict[str, Any]
) -> float | None:
    """下单时估算成交价（用于决定股数）；真实成交在 fill 阶段重算。"""
    # 无明确执行日时用最新一根日线的收盘价估算
    try:
        row = conn.execute(
            "SELECT close FROM daily_bars WHERE symbol=? ORDER BY trade_date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if not row or _as_float(row[0]) <= 0:
        return None
    return rules.slippage_price(_as_float(row[0]), side, cfg, tick_round=_tick_round(cfg))


# ---------------------------------------------------------------------------
# 建 / 重置账户
# ---------------------------------------------------------------------------
def init_account(cash: float, db_path: Path | str) -> None:
    """初始化（或重置）默认模拟账户：清空持仓/订单/成交/净值曲线，写入初始现金。"""
    initial = rules.round_cents(_as_float(cash))
    if initial < 0:
        raise ValueError("初始现金不能为负")
    conn = _connect(db_path)
    try:
        ensure_paper_tables(conn)
        with conn:
            conn.execute("DELETE FROM paper_positions")
            conn.execute("DELETE FROM paper_orders")
            conn.execute("DELETE FROM paper_trades")
            conn.execute("DELETE FROM paper_equity_curve")
        _insert_account(conn, initial)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 交易日推进
# ---------------------------------------------------------------------------
def _initial_start(conn: sqlite3.Connection, cfg: dict[str, Any], bar_dates: list[str]) -> str:
    """首次推进的起始日：优先回放最早信号对应的 T+1，否则从最新行情日。"""
    symbols = list(cfg.get("symbols") or [])
    signal_dates = _read_signal_dates(conn, symbols)
    if bar_dates:
        if signal_dates:
            first = min(signal_dates)
            later = [d for d in bar_dates if d > first]
            if later:
                return later[0]
        return bar_dates[-1]
    fallback = ((cfg.get("backtest") or {}).get("start")) or "2025-01-01"
    return str(fallback)


def _plan_dates(
    conn: sqlite3.Connection, cfg: dict[str, Any], days: int, bar_dates: list[str]
) -> list[str]:
    cursor = _last_equity_ts(conn)
    if cursor is None:
        start = _initial_start(conn, cfg, bar_dates)
        pool = [d for d in bar_dates if d >= start]
        if len(pool) >= days:
            return pool[:days]
        if bar_dates:
            ref = max([start] + pool + [bar_dates[-1]])
            return pool + _weekdays_after(ref, days - len(pool))
        return _weekdays_from(start, days)

    pool = [d for d in bar_dates if d > cursor]
    if len(pool) >= days:
        return pool[:days]
    ref = max([cursor] + pool + bar_dates[-1:])
    return pool + _weekdays_after(ref, days - len(pool))


def _prev_date(bar_dates: list[str], trade_date: str) -> str | None:
    earlier = [d for d in bar_dates if d < trade_date]
    return earlier[-1] if earlier else None


def _exec_open(conn: sqlite3.Connection) -> None:
    """交易日开盘结算 T+1：前一交易日起持有的股份全部可卖。"""
    conn.execute("UPDATE paper_positions SET available_qty = qty")


def _signal_sort_key(sig: dict[str, Any]) -> str:
    return str(sig.get("asof") or sig.get("created_at") or "")


def _signal_expired(sig: dict[str, Any], trade_date: str) -> bool:
    expires = _parse_iso(sig.get("expires_at"))
    if expires is None:
        return False  # 契约要求有 expires_at；缺失时保守放行，由上层数据校验兜底
    return expires < _exec_dt(trade_date)


def _order_id_for(signal_row: dict[str, Any], side: str) -> str:
    signal_id = str(signal_row.get("signal_id") or "").strip()
    if signal_id:
        return f"{signal_id}:{side}"
    return f"{signal_row.get('symbol')}-{signal_row.get('trade_date')}-{side}"


def _order_exists(conn: sqlite3.Connection, order_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM paper_orders WHERE order_id=?", (order_id,)).fetchone()
    return row is not None


def _has_pending_order(conn: sqlite3.Connection, symbol: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_orders WHERE symbol=? AND status=? LIMIT 1", (symbol, PENDING)
    ).fetchone()
    return row is not None


def _create_orders_from_signals(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    trade_date: str,
    prev_date: str | None,
    signal_index: dict[str, list[dict[str, Any]]],
) -> int:
    """把上一交易日（T）的信号转成 T+1 开盘执行的下单。"""
    if not prev_date:
        return 0
    candidates = signal_index.get(prev_date) or []
    if not candidates:
        return 0
    # 同 symbol 多条信号取 asof 最新的一条，避免重复下单
    latest: dict[str, dict[str, Any]] = {}
    for sig in candidates:
        symbol = str(sig.get("symbol") or "")
        if not symbol:
            continue
        key = _signal_sort_key(sig)
        if symbol not in latest or key > _signal_sort_key(latest[symbol]):
            latest[symbol] = sig

    created = 0
    for symbol in sorted(latest):
        sig = latest[symbol]
        direction = str(sig.get("direction") or "").strip().lower()
        if direction not in ("up", "down"):
            continue  # flat：不新开仓、不强制平仓
        if _signal_expired(sig, trade_date):
            continue  # 过期信号不参与模拟盘
        if _has_pending_order(conn, symbol):
            continue  # 已有顺延订单，等它先成交/被拒
        side = "buy" if direction == "up" else "sell"
        order_id = _order_id_for(sig, side)
        if _order_exists(conn, order_id):
            continue

        bar = _read_bar(conn, symbol, trade_date)
        open_price = _as_float(bar.get("open")) if bar else 0.0
        if open_price > 0:
            est_price = rules.slippage_price(
                open_price, side, cfg, tick_round=_tick_round(cfg)
            )
        else:
            est_price = _as_float(sig.get("price")) or _estimate_fill_price(conn, symbol, side, cfg) or 0.0

        reason: str | None = None
        if side == "buy":
            qty, reason = _buy_capacity(conn, cfg, symbol, est_price)
        else:
            pos = _get_position(conn, symbol)
            if not pos or _as_int(pos.get("qty")) <= 0:
                qty, reason = 0, rules.REJECT_NO_POSITION
            else:
                qty = rules.sellable_qty(pos)
                if qty <= 0:
                    qty, reason = 0, rules.REJECT_T_PLUS_ONE

        status = PENDING if qty > 0 else REJECTED
        conn.execute(
            "INSERT OR IGNORE INTO paper_orders"
            "(order_id, symbol, side, qty, target_price, status, reject_reason, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                order_id,
                symbol,
                side,
                int(qty),
                rules.round_cents(_as_float(sig.get("price"))) if sig.get("price") is not None else None,
                status,
                reason,
                str(sig.get("asof") or sig.get("created_at") or _now_iso()),
            ),
        )
        created += 1
    return created


def _reject_order(conn: sqlite3.Connection, order: dict[str, Any], reason: str) -> None:
    conn.execute(
        "UPDATE paper_orders SET status=?, reject_reason=? WHERE order_id=?",
        (REJECTED, reason, order.get("order_id")),
    )


def _record_fill(
    conn: sqlite3.Connection,
    order: dict[str, Any],
    *,
    qty: int,
    price: float,
    fee: float,
    trade_date: str,
    realized_pnl: float | None = None,
) -> None:
    filled_at = f"{trade_date}T09:30:00+08:00"
    amount = rules.round_cents(qty * price)
    conn.execute(
        "UPDATE paper_orders SET status=?, qty=?, filled_at=?, filled_price=?, fee=?, reject_reason=NULL"
        " WHERE order_id=?",
        (FILLED, int(qty), filled_at, price, rules.round_cents(fee), order.get("order_id")),
    )
    conn.execute(
        "INSERT OR REPLACE INTO paper_trades"
        "(trade_id, order_id, symbol, side, qty, price, amount, fee, realized_pnl, trade_date, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"{order.get('order_id')}#trade",
            order.get("order_id"),
            order.get("symbol"),
            order.get("side"),
            int(qty),
            price,
            amount,
            rules.round_cents(fee),
            None if realized_pnl is None else rules.round_cents(realized_pnl),
            trade_date,
            filled_at,
        ),
    )


def _fill_buy(
    conn: sqlite3.Connection, order: dict[str, Any], price: float, trade_date: str, cfg: dict[str, Any]
) -> tuple[bool, str | None]:
    lot = rules.lot_size(cfg)
    cash = _cash(conn)
    qty_cap, reason = _buy_capacity(conn, cfg, order.get("symbol"), price)
    if qty_cap <= 0:
        return False, reason or rules.REJECT_INSUFFICIENT_CASH
    ordered_qty = _as_int(order.get("qty"))
    qty = min(ordered_qty, qty_cap) if ordered_qty > 0 else qty_cap
    qty = rules.round_lot(qty, lot)
    amount = 0.0
    fee = 0.0
    while qty > 0:
        amount = rules.round_cents(qty * price)
        fee = rules.fees("buy", amount, cfg)["total"]
        if rules.round_cents(amount + fee) <= cash + 1e-9:
            break
        qty -= lot
    if qty <= 0:
        return False, rules.REJECT_INSUFFICIENT_CASH

    symbol = str(order.get("symbol"))
    pos = _get_position(conn, symbol)
    old_qty = _as_int(pos.get("qty")) if pos else 0
    old_available = _as_int(pos.get("available_qty")) if pos else 0
    old_cost = _as_float(pos.get("avg_cost")) if pos else 0.0
    new_qty = old_qty + qty
    avg_cost = round((old_qty * old_cost + amount + fee) / new_qty, 4) if new_qty else 0.0
    _set_cash(conn, cash - rules.round_cents(amount + fee))
    conn.execute(
        "INSERT OR REPLACE INTO paper_positions"
        "(symbol, qty, available_qty, avg_cost, last_price, updated_at) VALUES (?,?,?,?,?,?)",
        (symbol, new_qty, old_available, avg_cost, price, trade_date),
    )
    _record_fill(conn, order, qty=qty, price=price, fee=fee, trade_date=trade_date, realized_pnl=None)
    return True, None


def _fill_sell(
    conn: sqlite3.Connection, order: dict[str, Any], price: float, trade_date: str, cfg: dict[str, Any]
) -> tuple[bool, str | None]:
    symbol = str(order.get("symbol"))
    pos = _get_position(conn, symbol)
    if not pos or _as_int(pos.get("qty")) <= 0:
        return False, rules.REJECT_NO_POSITION
    available = _as_int(pos.get("available_qty"))
    if available <= 0:
        return False, rules.REJECT_T_PLUS_ONE  # T+1：当日买入不可当日卖
    ordered_qty = _as_int(order.get("qty"))
    qty = rules.sellable_qty(pos, ordered_qty if ordered_qty > 0 else None)
    if qty <= 0:
        return False, rules.REJECT_T_PLUS_ONE

    cash = _cash(conn)
    amount = rules.round_cents(qty * price)
    fee = rules.fees("sell", amount, cfg)["total"]
    proceeds = rules.round_cents(amount - fee)
    avg_cost = _as_float(pos.get("avg_cost"))
    realized_pnl = rules.round_cents(amount - fee - qty * avg_cost)

    _set_cash(conn, cash + proceeds)
    left_qty = _as_int(pos.get("qty")) - qty
    left_available = available - qty
    if left_qty <= 0:
        conn.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol,))
    else:
        conn.execute(
            "UPDATE paper_positions SET qty=?, available_qty=?, last_price=?, updated_at=? WHERE symbol=?",
            (left_qty, max(left_available, 0), price, trade_date, symbol),
        )
    _record_fill(
        conn, order, qty=qty, price=price, fee=fee, trade_date=trade_date, realized_pnl=realized_pnl
    )
    return True, None


def _execute_order(
    conn: sqlite3.Connection, order: dict[str, Any], trade_date: str, cfg: dict[str, Any]
) -> tuple[bool, str | None]:
    symbol = str(order.get("symbol"))
    side = str(order.get("side") or "")
    bar = _read_bar(conn, symbol, trade_date)
    if bar is None or _as_int(bar.get("is_suspended")):
        return False, "__postpone__"  # 停牌/无行情：顺延，不改状态
    open_price = _as_float(bar.get("open"))
    if open_price <= 0:
        return False, rules.REJECT_NO_PRICE
    limit_up, limit_down = _limit_prices_for_bar(symbol, bar)

    if _limit_check(cfg):
        if side == "buy" and rules.is_limit_up_at_open(open_price, limit_up):
            return False, rules.REJECT_LIMIT_UP
        if side == "sell" and rules.is_limit_down_at_open(open_price, limit_down):
            return False, rules.REJECT_LIMIT_DOWN

    price = rules.slippage_price(
        open_price,
        side,
        cfg,
        limit_up=limit_up or None,
        limit_down=limit_down or None,
        tick_round=_tick_round(cfg),
    )
    if price <= 0:
        return False, rules.REJECT_NO_PRICE
    if side == "buy":
        return _fill_buy(conn, order, price, trade_date, cfg)
    return _fill_sell(conn, order, price, trade_date, cfg)


def _fill_pending(
    conn: sqlite3.Connection, trade_date: str, cfg: dict[str, Any]
) -> tuple[int, int, int]:
    """成交所有 pending 订单；返回 (filled, rejected, postponed)。卖出先于买入处理。"""
    rows = conn.execute(
        "SELECT * FROM paper_orders WHERE status=?"
        " ORDER BY CASE side WHEN 'sell' THEN 0 ELSE 1 END, created_at, order_id",
        (PENDING,),
    ).fetchall()
    filled = rejected = postponed = 0
    for row in rows:
        order = dict(row)
        ok, reason = _execute_order(conn, order, trade_date, cfg)
        if ok:
            filled += 1
        elif reason == "__postpone__":
            postponed += 1
        else:
            _reject_order(conn, order, reason or "unknown")
            rejected += 1
    return filled, rejected, postponed


def _mark_to_market(conn: sqlite3.Connection, trade_date: str) -> dict[str, float]:
    """按当日收盘价更新持仓 last_price，并追加净值曲线一行。"""
    cash = _cash(conn)
    market_value = 0.0
    for pos in _all_positions(conn):
        symbol = str(pos.get("symbol"))
        price = _as_float(pos.get("last_price")) or _as_float(pos.get("avg_cost"))
        bar = _read_bar(conn, symbol, trade_date)
        if bar is not None and not _as_int(bar.get("is_suspended")):
            close = _as_float(bar.get("close"))
            if close > 0:
                price = close
        if price <= 0:
            price = _as_float(pos.get("avg_cost"))
        qty = _as_int(pos.get("qty"))
        market_value += qty * price
        conn.execute(
            "UPDATE paper_positions SET last_price=?, updated_at=? WHERE symbol=?",
            (price, trade_date, symbol),
        )
    market_value = rules.round_cents(market_value)
    equity = rules.round_cents(cash + market_value)
    conn.execute(
        "INSERT OR REPLACE INTO paper_equity_curve(ts, cash, market_value, equity) VALUES (?,?,?,?)",
        (trade_date, cash, market_value, equity),
    )
    return {"cash": cash, "market_value": market_value, "equity": equity}


def _run_days(
    conn: sqlite3.Connection, days: int, cfg: dict[str, Any]
) -> dict[str, Any]:
    symbols = [str(s) for s in (cfg.get("symbols") or [])]
    stats: dict[str, Any] = {
        "days_requested": days,
        "days_processed": 0,
        "from": None,
        "to": None,
        "orders_created": 0,
        "filled": 0,
        "rejected": 0,
        "pending": 0,
    }
    if days <= 0:
        return stats

    bar_dates = _bar_dates(conn, symbols)
    plan = _plan_dates(conn, cfg, days, bar_dates)
    if not plan:
        return stats

    signal_index = _read_all_signals(conn, symbols)
    prev: str | None = _prev_date(bar_dates, plan[0])
    if prev is None:
        earlier = [d for d in signal_index if d < plan[0]]
        if earlier:
            prev = max(earlier)

    for trade_date in plan:
        _exec_open(conn)
        stats["orders_created"] += _create_orders_from_signals(
            conn, cfg, trade_date, prev, signal_index
        )
        filled, rejected, _postponed = _fill_pending(conn, trade_date, cfg)
        stats["filled"] += filled
        stats["rejected"] += rejected
        _mark_to_market(conn, trade_date)
        conn.commit()
        prev = trade_date
        stats["days_processed"] += 1

    stats["from"] = plan[0]
    stats["to"] = plan[-1]
    stats["pending"] = _count(conn, "paper_orders", f"status='{PENDING}'")
    return stats


def _count(conn: sqlite3.Connection, table: str, where: str = "1=1") -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# 对外：advance / snapshot
# ---------------------------------------------------------------------------
def advance(days: int, cfg: dict[str, Any]) -> dict[str, Any]:
    """推进模拟盘 ``days`` 个交易日并返回最新快照（含 ``run`` 本次执行统计）。"""
    days = _as_int(days)
    if days < 0:
        raise ValueError("days 不能为负")
    db = db_path(cfg)
    conn = _connect(db)
    try:
        ensure_paper_tables(conn)
        if _load_account(conn) is None:
            _insert_account(conn, _initial_cash(cfg))
            conn.commit()
        run_stats = _run_days(conn, days, cfg)
    finally:
        conn.close()
    snap = snapshot(cfg)
    snap["run"] = run_stats
    return snap


def _load_curve(conn: sqlite3.Connection, limit: int | None = CURVE_LIMIT) -> list[dict[str, Any]]:
    if not _table_exists(conn, "paper_equity_curve"):
        return []
    if limit and limit > 0:
        rows = conn.execute(
            "SELECT * FROM paper_equity_curve ORDER BY ts DESC LIMIT ?", (int(limit),)
        ).fetchall()
        rows = list(reversed(rows))
    else:
        rows = conn.execute("SELECT * FROM paper_equity_curve ORDER BY ts").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        out.append(
            {
                "ts": str(item.get("ts")),
                "cash": rules.round_cents(_as_float(item.get("cash"))),
                "market_value": rules.round_cents(_as_float(item.get("market_value"))),
                "equity": rules.round_cents(_as_float(item.get("equity"))),
            }
        )
    return out


def _trade_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """交易胜率（交易口径）：已平仓盈利笔数 / 平仓笔数。

    「平仓」按成交口径统计每一笔卖出成交（清仓或减仓都算一次平仓），
    单笔实现盈亏 = 卖出成交额 - 卖出费用 - 卖出股数 × 持仓均价（均价已含买入费用）。
    """
    if not _table_exists(conn, "paper_trades"):
        return {
            "win_rate": None,
            "closed_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "flat_trades": 0,
            "realized_pnl": 0.0,
        }
    try:
        rows = conn.execute("SELECT * FROM paper_trades WHERE side='sell'").fetchall()
    except sqlite3.Error:
        rows = []
    closed = wins = losses = flats = 0
    realized = 0.0
    for row in rows:
        item = dict(row)
        pnl = _as_float(item.get("realized_pnl"))
        closed += 1
        realized += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        else:
            flats += 1
    return {
        "win_rate": round(wins / closed, 4) if closed else None,
        "closed_trades": closed,
        "winning_trades": wins,
        "losing_trades": losses,
        "flat_trades": flats,
        "realized_pnl": rules.round_cents(realized),
    }


def snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    """返回账户快照：现金/持仓市值/权益/今日盈亏/累计收益/交易胜率/最近订单/净值曲线。"""
    db = db_path(cfg)
    conn = _connect(db)
    try:
        ensure_paper_tables(conn)
        account = _load_account(conn)
        if account is None:
            _insert_account(conn, _initial_cash(cfg))
            conn.commit()
            account = _load_account(conn)

        curve = _load_curve(conn)
        positions = []
        for pos in _all_positions(conn):
            qty = _as_int(pos.get("qty"))
            last_price = _as_float(pos.get("last_price")) or _as_float(pos.get("avg_cost"))
            avg_cost = _as_float(pos.get("avg_cost"))
            positions.append(
                {
                    "symbol": str(pos.get("symbol")),
                    "qty": qty,
                    "available_qty": _as_int(pos.get("available_qty")),
                    "avg_cost": round(avg_cost, 4),
                    "last_price": rules.round_cents(last_price),
                    "market_value": rules.round_cents(qty * last_price),
                    "unrealized_pnl": rules.round_cents(qty * (last_price - avg_cost)),
                    "updated_at": pos.get("updated_at"),
                }
            )

        cash = rules.round_cents(_as_float(account.get("cash")))
        initial_cash = _as_float(account.get("initial_cash"))
        if initial_cash <= 0:
            initial_cash = rules.round_cents(curve[0]["equity"]) if curve else cash
        if curve:
            market_value = curve[-1]["market_value"]
            equity = curve[-1]["equity"]
        else:
            market_value = rules.round_cents(sum(p["market_value"] for p in positions))
            equity = rules.round_cents(cash + market_value)
        prev_equity = curve[-2]["equity"] if len(curve) >= 2 else initial_cash
        today_pnl = rules.round_cents(equity - prev_equity)
        total_return = round(equity / initial_cash - 1.0, 6) if initial_cash > 0 else 0.0
        trade_stats = _trade_stats(conn)

        orders: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        if _table_exists(conn, "paper_orders"):
            orders = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM paper_orders ORDER BY COALESCE(filled_at, created_at, '') DESC, order_id DESC LIMIT ?",
                    (ORDER_LIMIT,),
                ).fetchall()
            ]
        if _table_exists(conn, "paper_trades"):
            trades = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM paper_trades ORDER BY COALESCE(trade_date,'') DESC, trade_id DESC LIMIT ?",
                    (ORDER_LIMIT,),
                ).fetchall()
            ]

        return {
            "schema": 1,
            "account": {
                "id": str(account.get("id") or ACCOUNT_ID),
                "cash": cash,
                "initial_cash": rules.round_cents(initial_cash),
                "created_at": account.get("created_at"),
            },
            "as_of": curve[-1]["ts"] if curve else None,
            "cash": cash,
            "equity": equity,
            "market_value": market_value,
            "today_pnl": today_pnl,
            "total_return": total_return,
            # 交易胜率（已平仓盈利笔数 / 平仓笔数）；无平仓交易时 win_rate 为 null
            "win_rate": trade_stats["win_rate"],
            "closed_trades": trade_stats["closed_trades"],
            "winning_trades": trade_stats["winning_trades"],
            "losing_trades": trade_stats["losing_trades"],
            "flat_trades": trade_stats["flat_trades"],
            "realized_pnl": trade_stats["realized_pnl"],
            "positions": positions,
            "curve": curve,
            "orders": orders,
            "trades": trades,
            "counts": {
                "positions": len(positions),
                "orders": _count(conn, "paper_orders"),
                "pending_orders": _count(conn, "paper_orders", f"status='{PENDING}'"),
                "trades": _count(conn, "paper_trades"),
            },
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 低层入口（单测 / 回测调试用）
# ---------------------------------------------------------------------------
def fill_pending_orders(db_path: str | Path, trade_date: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """仅对指定交易日成交当前 pending 订单（不结算 T+1、不推进日期）。

    正常流程由 ``advance`` 调用；单独暴露用于验证 T+1 / 拒单等边界。
    """
    conn = _connect(db_path)
    try:
        ensure_paper_tables(conn)
        filled, rejected, postponed = _fill_pending(conn, trade_date, cfg)
        conn.commit()
    finally:
        conn.close()
    return {"filled": filled, "rejected": rejected, "postponed": postponed}


__all__ = [
    "ACCOUNT_ID",
    "advance",
    "db_path",
    "ensure_paper_tables",
    "fill_pending_orders",
    "init_account",
    "snapshot",
]
