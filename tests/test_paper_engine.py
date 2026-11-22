"""模拟盘引擎单测（契约 §3.4 / §6 / §9）：固定行情下精确断言现金/持仓/净值/胜率。

全部离线：fixture 数据在 tests/fixtures/paper/paper_seed.py 中构造（行情 Parquet +
状态 DuckDB），不联网、不依赖 data-dev。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from quant.paper import engine

FIXTURES = Path(__file__).parent / "fixtures" / "paper"
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))

import paper_seed  # noqa: E402


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _setup(tmp_path: Path, scenario, *, initial_cash: float = 100000, **paper_overrides):
    bars, signals = scenario()
    db = paper_seed.create_db(tmp_path / "quant.db", bars, signals)
    cfg = paper_seed.make_cfg(db, initial_cash=initial_cash, **paper_overrides)
    engine.init_account(initial_cash, db)
    return db, cfg


def _rows(db: Path, sql: str, params=()) -> list[dict]:
    return paper_seed.fetch_rows(db, sql, params)


def _orders(db: Path) -> list[dict]:
    return _rows(db, "SELECT * FROM paper_orders ORDER BY created_at, order_id")


def _trades(db: Path) -> list[dict]:
    return _rows(db, "SELECT * FROM paper_trades ORDER BY trade_date, trade_id")


def _positions(db: Path) -> list[dict]:
    return _rows(db, "SELECT * FROM paper_positions ORDER BY symbol")


def _curve(db: Path) -> list[dict]:
    return _rows(db, "SELECT * FROM paper_equity_curve ORDER BY ts")


# ---------------------------------------------------------------------------
# 场景 A：推进 3 天，精确断言现金 / 持仓 / 净值 / 胜率
# ---------------------------------------------------------------------------
def test_advance_three_days_exact(tmp_path: Path) -> None:
    db, cfg = _setup(tmp_path, paper_seed.scenario_a)
    snap = engine.advance(3, cfg)

    # --- 推进的交易日 ---
    assert snap["run"]["days_requested"] == 3
    assert snap["run"]["days_processed"] == 3
    assert snap["run"]["from"] == "2026-11-17"
    assert snap["run"]["to"] == "2026-11-19"
    assert [row["ts"] for row in snap["curve"]] == ["2026-11-17", "2026-11-18", "2026-11-19"]

    # --- 2026-11-17：10.50 开盘 ×1.0005 滑点 → 10.51 成交 1900 股 ---
    # 成交额 19969.00；佣金 max(4.99225, 5)=5.00；过户费 0.20；现金 100000-19969-5.20=80025.80
    # 收盘 10.80 → 市值 20520.00，权益 100545.80
    assert snap["curve"][0]["cash"] == pytest.approx(80025.80, abs=0.01)
    assert snap["curve"][0]["market_value"] == pytest.approx(20520.00, abs=0.01)
    assert snap["curve"][0]["equity"] == pytest.approx(100545.80, abs=0.01)

    # --- 2026-11-18：10.90 开盘 ×0.9995 滑点 → 10.89 卖出 1900 股 ---
    # 成交额 20691.00；佣金 5.17 + 印花税 10.35 + 过户费 0.21 = 15.73；现金 → 100701.07
    assert snap["curve"][1]["cash"] == pytest.approx(100701.07, abs=0.01)
    assert snap["curve"][1]["market_value"] == pytest.approx(0.0, abs=0.01)
    assert snap["curve"][1]["equity"] == pytest.approx(100701.07, abs=0.01)
    assert snap["curve"][2]["equity"] == pytest.approx(100701.07, abs=0.01)

    # --- 账户快照 ---
    assert snap["cash"] == pytest.approx(100701.07, abs=0.01)
    assert snap["equity"] == pytest.approx(100701.07, abs=0.01)
    assert snap["market_value"] == pytest.approx(0.0, abs=0.01)
    assert snap["positions"] == []
    assert snap["account"]["initial_cash"] == pytest.approx(100000.0)
    assert snap["total_return"] == pytest.approx(100701.07 / 100000 - 1, abs=1e-5)
    assert snap["as_of"] == "2026-11-19"

    # --- 订单 / 成交 ---
    orders = _orders(db)
    assert [o["status"] for o in orders] == ["filled", "filled"]
    buy, sell = orders
    assert (buy["side"], buy["qty"], buy["filled_price"], buy["fee"]) == ("buy", 1900, 10.51, 5.20)
    assert (sell["side"], sell["qty"], sell["filled_price"], sell["fee"]) == ("sell", 1900, 10.89, 15.73)
    assert buy["filled_at"] == "2026-11-17T09:30:00+08:00"
    assert sell["filled_at"] == "2026-11-18T09:30:00+08:00"
    trades = _trades(db)
    assert len(trades) == 2
    assert trades[0]["amount"] == pytest.approx(19969.00, abs=0.01)
    assert trades[1]["amount"] == pytest.approx(20691.00, abs=0.01)

    # --- 交易胜率（已平仓盈利笔数 / 平仓笔数）---
    # 实现盈亏 = 20691.00 - 15.73 - 1900×10.5127 = 701.14
    assert snap["win_rate"] == pytest.approx(1.0)
    assert snap["closed_trades"] == 1
    assert snap["winning_trades"] == 1
    assert snap["losing_trades"] == 0
    assert snap["realized_pnl"] == pytest.approx(701.14, abs=0.01)
    sell_trade = [t for t in trades if t["side"] == "sell"][0]
    assert sell_trade["realized_pnl"] == pytest.approx(701.14, abs=0.01)

    # --- 现金不允许为负，持仓不允许为负 ---
    for row in snap["curve"]:
        assert row["cash"] >= 0
    for pos in _positions(db):
        assert pos["qty"] >= 0 and pos["available_qty"] >= 0


def test_advance_stepwise_respects_t_plus_one(tmp_path: Path) -> None:
    """T 日买入当天不可卖（available_qty=0），次日开盘结算后才可卖。"""
    db, cfg = _setup(tmp_path, paper_seed.scenario_a)

    snap1 = engine.advance(1, cfg)  # 2026-11-17：买入
    pos = snap1["positions"][0]
    assert pos["qty"] == 1900
    assert pos["available_qty"] == 0, "当日买入的股份 T+1 前不可卖"
    assert pos["avg_cost"] == pytest.approx(10.5127, abs=1e-4)
    assert snap1["win_rate"] is None  # 尚无平仓交易

    snap2 = engine.advance(1, cfg)  # 2026-11-18：结算后卖出
    assert snap2["positions"] == []
    assert snap2["cash"] == pytest.approx(100701.07, abs=0.01)
    assert snap2["win_rate"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 涨停买不进
# ---------------------------------------------------------------------------
def test_limit_up_open_rejects_buy(tmp_path: Path) -> None:
    db, cfg = _setup(tmp_path, paper_seed.scenario_limit_up)
    snap = engine.advance(1, cfg)

    orders = _orders(db)
    assert len(orders) == 1
    assert orders[0]["side"] == "buy"
    assert orders[0]["status"] == "rejected"
    assert orders[0]["reject_reason"] == "limit_up"
    assert snap["positions"] == []
    assert snap["cash"] == pytest.approx(100000.0, abs=0.01)
    assert snap["equity"] == pytest.approx(100000.0, abs=0.01)
    assert snap["run"]["filled"] == 0
    assert snap["run"]["rejected"] == 1


# ---------------------------------------------------------------------------
# 跌停卖不出
# ---------------------------------------------------------------------------
def test_limit_down_open_rejects_sell(tmp_path: Path) -> None:
    db, cfg = _setup(tmp_path, paper_seed.scenario_limit_down)
    snap = engine.advance(2, cfg)

    orders = _orders(db)
    assert [o["status"] for o in orders] == ["filled", "rejected"]
    assert orders[1]["side"] == "sell"
    assert orders[1]["reject_reason"] == "limit_down"
    # 持仓保留，未被卖飞
    positions = _positions(db)
    assert len(positions) == 1
    assert positions[0]["qty"] == 1900
    assert positions[0]["available_qty"] == 1900
    # 现金为买入后的余额；权益按 9.80 收盘盯市 = 80025.80 + 1900×9.80 = 98645.80
    assert snap["cash"] == pytest.approx(80025.80, abs=0.01)
    assert snap["equity"] == pytest.approx(98645.80, abs=0.01)
    assert snap["win_rate"] is None


# ---------------------------------------------------------------------------
# 停牌顺延
# ---------------------------------------------------------------------------
def test_suspended_order_deferred_to_next_tradable_day(tmp_path: Path) -> None:
    db, cfg = _setup(tmp_path, paper_seed.scenario_suspended)
    snap = engine.advance(3, cfg)

    orders = _orders(db)
    assert len(orders) == 1
    assert orders[0]["status"] == "filled"
    assert orders[0]["filled_at"].startswith("2026-11-18"), "停牌日不成交，顺延到下一交易日"
    assert orders[0]["filled_price"] == pytest.approx(10.51)
    assert snap["run"]["filled"] == 1
    assert snap["positions"][0]["qty"] == 1900
    assert snap["curve"][0]["ts"] == "2026-11-17"  # 停牌日仍有净值行（收盘盯市）
    assert snap["curve"][1]["equity"] == pytest.approx(100545.80, abs=0.01)


# ---------------------------------------------------------------------------
# 整手取整 + 含费资金上限
# ---------------------------------------------------------------------------
def test_lot_rounding_and_cash_limit(tmp_path: Path) -> None:
    db, cfg = _setup(
        tmp_path,
        paper_seed.scenario_lot_rounding,
        initial_cash=10000,
        max_position_pct=1.0,
    )
    snap = engine.advance(1, cfg)

    # 10000 元、10.01 成交价：1000 股需 10000+费用，买不起 → 900 股
    pos = snap["positions"][0]
    assert pos["qty"] == 900
    assert pos["qty"] % 100 == 0, "买入数量必须是整手"
    order = _orders(db)[0]
    assert order["qty"] == 900 and order["filled_price"] == pytest.approx(10.01)
    assert order["fee"] == pytest.approx(5.09, abs=0.01)  # 佣金 5.00 + 过户费 0.09
    assert snap["cash"] == pytest.approx(985.91, abs=0.01)
    assert snap["cash"] >= 0
    # 收盘 10.50 → 市值 9450，权益 10435.91
    assert snap["curve"][0]["equity"] == pytest.approx(10435.91, abs=0.01)


# ---------------------------------------------------------------------------
# 亏损平仓计入胜率
# ---------------------------------------------------------------------------
def test_losing_trade_makes_win_rate_zero(tmp_path: Path) -> None:
    db, cfg = _setup(tmp_path, paper_seed.scenario_losing)
    snap = engine.advance(2, cfg)

    assert snap["closed_trades"] == 1
    assert snap["winning_trades"] == 0
    assert snap["losing_trades"] == 1
    assert snap["win_rate"] == pytest.approx(0.0)
    # 10.00 开盘卖出 ×0.9995 = 9.995 → 10.00；成交额 19000；费用 5.00+9.50+0.19=14.69
    # 实现盈亏 = 19000 - 14.69 - 1900×10.5127 = -988.82；现金 80025.80+18985.31=99011.11
    assert snap["realized_pnl"] == pytest.approx(-988.82, abs=0.01)
    assert snap["cash"] == pytest.approx(99011.11, abs=0.01)
    assert snap["win_rate"] is not None and snap["win_rate"] < 0.5


# ---------------------------------------------------------------------------
# T+1 拒单（低层入口，验证当日买入无法卖出）
# ---------------------------------------------------------------------------
def test_same_day_buy_cannot_sell_rejected(tmp_path: Path) -> None:
    bars, signals = paper_seed.scenario_a()
    db = paper_seed.create_db(tmp_path / "quant.db", bars, signals)
    cfg = paper_seed.make_cfg(db, initial_cash=100000)
    engine.init_account(100000, db)

    paper_seed.execute_sql(
        db,
        "INSERT INTO paper_positions(symbol,qty,available_qty,avg_cost,last_price,updated_at)"
        " VALUES('600519',1000,0,10.0,10.0,'2026-11-17')",
    )
    paper_seed.execute_sql(
        db,
        "INSERT INTO paper_orders(order_id,symbol,side,qty,target_price,status,reject_reason,created_at)"
        " VALUES('test-sell','600519','sell',1000,10.5,'pending',NULL,'2026-11-17T09:30:00+08:00')",
    )

    result = engine.fill_pending_orders(db, "2026-11-17", cfg)
    assert result["rejected"] == 1 and result["filled"] == 0
    order = _orders(db)[0]
    assert order["status"] == "rejected"
    assert order["reject_reason"] == "t_plus_one"
    pos = _positions(db)[0]
    assert pos["qty"] == 1000, "T+1 拒单不得改变持仓"


# ---------------------------------------------------------------------------
# 不允许超卖：卖单大于可卖数量时按可卖数量成交
# ---------------------------------------------------------------------------
def test_sell_qty_clamped_to_available(tmp_path: Path) -> None:
    bars, signals = paper_seed.scenario_a()
    db = paper_seed.create_db(tmp_path / "quant.db", bars, signals)
    cfg = paper_seed.make_cfg(db, initial_cash=100000)
    engine.init_account(100000, db)

    paper_seed.execute_sql(
        db,
        "INSERT INTO paper_positions(symbol,qty,available_qty,avg_cost,last_price,updated_at)"
        " VALUES('600519',1000,1000,10.0,10.0,'2026-11-17')",
    )
    paper_seed.execute_sql(
        db,
        "INSERT INTO paper_orders(order_id,symbol,side,qty,target_price,status,reject_reason,created_at)"
        " VALUES('test-oversell','600519','sell',5000,10.5,'pending',NULL,'2026-11-17T09:30:00+08:00')",
    )

    result = engine.fill_pending_orders(db, "2026-11-17", cfg)
    assert result["filled"] == 1
    trade = _trades(db)[0]
    assert trade["qty"] == 1000, "不允许超卖：最多卖出可卖数量"
    assert _positions(db) == []
    order = _orders(db)[0]
    assert order["qty"] == 1000 and order["status"] == "filled"


# ---------------------------------------------------------------------------
# 过期信号不参与模拟盘
# ---------------------------------------------------------------------------
def test_expired_signal_skipped(tmp_path: Path) -> None:
    bars, _ = paper_seed.scenario_a()
    expired = paper_seed.signal(
        "600519-2026-11-16-20261116T150500",
        "600519",
        "2026-11-16",
        asof="2026-11-16T15:05:00+08:00",
        expires_at="2026-11-17T09:29:59+08:00",  # 执行时点 09:30 之前已过期
        price=10.00,
        direction="up",
    )
    db = paper_seed.create_db(tmp_path / "quant.db", bars, [expired])
    cfg = paper_seed.make_cfg(db, initial_cash=100000)
    engine.init_account(100000, db)

    snap = engine.advance(1, cfg)
    assert _orders(db) == [], "过期信号不得生成订单"
    assert snap["positions"] == []
    assert snap["cash"] == pytest.approx(100000.0)


# ---------------------------------------------------------------------------
# init_account 重置状态
# ---------------------------------------------------------------------------
def test_init_account_resets_state(tmp_path: Path) -> None:
    db, cfg = _setup(tmp_path, paper_seed.scenario_a)
    engine.advance(2, cfg)
    assert _orders(db)

    engine.init_account(500000, db)
    snap = engine.snapshot(cfg)
    assert snap["cash"] == pytest.approx(500000.0)
    assert snap["equity"] == pytest.approx(500000.0)
    assert snap["account"]["initial_cash"] == pytest.approx(500000.0)
    assert snap["positions"] == []
    assert snap["curve"] == []
    assert snap["as_of"] is None
    assert snap["win_rate"] is None
    assert _orders(db) == []


# ---------------------------------------------------------------------------
# snapshot 字段完整性 + 空账户
# ---------------------------------------------------------------------------
def test_snapshot_required_fields(tmp_path: Path) -> None:
    bars, signals = paper_seed.scenario_a()
    db = paper_seed.create_db(tmp_path / "quant.db", bars, signals)
    cfg = paper_seed.make_cfg(db, initial_cash=100000)
    engine.init_account(100000, db)

    snap = engine.snapshot(cfg)
    for key in (
        "cash",
        "equity",
        "market_value",
        "positions",
        "today_pnl",
        "total_return",
        "win_rate",
        "closed_trades",
        "winning_trades",
        "losing_trades",
        "realized_pnl",
        "curve",
        "orders",
        "as_of",
        "account",
    ):
        assert key in snap, f"snapshot 缺字段 {key}"
    assert snap["cash"] == pytest.approx(100000.0)
    assert snap["curve"] == []
    assert snap["win_rate"] is None


# ---------------------------------------------------------------------------
# 无行情/无信号时推进：用工作日兜底，输出平盘净值曲线
# ---------------------------------------------------------------------------
def test_advance_without_market_data_is_flat_curve(tmp_path: Path) -> None:
    db = tmp_path / "empty" / "quant.db"
    cfg = paper_seed.make_cfg(db, initial_cash=1000000)
    engine.init_account(1000000, db)

    snap = engine.advance(5, cfg)
    assert snap["run"]["days_processed"] == 5
    # 默认配置 backtest.start=2025-01-01，工作日顺延
    assert [row["ts"] for row in snap["curve"]] == [
        "2025-01-01",
        "2025-01-02",
        "2025-01-03",
        "2025-01-06",
        "2025-01-07",
    ]
    for row in snap["curve"]:
        assert row["cash"] == pytest.approx(1000000.0)
        assert row["equity"] == pytest.approx(1000000.0)
    assert snap["orders"] == []
    assert snap["positions"] == []
    assert snap["cash"] == pytest.approx(1000000.0)
