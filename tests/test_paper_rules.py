"""契约 §6 A股交易规则单测（纯函数，离线，不联网）。

覆盖：板块比例、涨跌停价四舍五入到分、T+1、佣金/印花税/过户费精确值、
滑点边界（含涨跌停封顶/封底）、整手取整与含费资金上限、涨跌停开盘判定。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import pytest

from quant.config import load_config
from quant.paper import rules


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config()


# ---------------------------------------------------------------------------
# 板块涨跌停比例
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("symbol", "is_st", "expected"),
    [
        ("600519", False, 0.10),  # 主板
        ("601318", False, 0.10),
        ("603259", False, 0.10),
        ("605499", False, 0.10),
        ("000001", False, 0.10),
        ("001979", False, 0.10),
        ("002594", False, 0.10),
        ("003816", False, 0.10),
        ("300750", False, 0.20),  # 创业板
        ("301236", False, 0.20),
        ("688981", False, 0.20),  # 科创板
        ("830799", False, 0.30),  # 北交所 8 开头
        ("430047", False, 0.30),  # 北交所 4 开头
        ("600519", True, 0.05),   # ST
        ("600519.SH", False, 0.10),  # 带交易所后缀也能识别
    ],
)
def test_limit_pct_by_board(symbol: str, is_st: bool, expected: float) -> None:
    assert rules.limit_pct(symbol, is_st) == expected


# ---------------------------------------------------------------------------
# 涨跌停价：四舍五入到分（ROUND_HALF_UP，不是银行家舍入）
# ---------------------------------------------------------------------------
def test_limit_prices_round_half_up_to_cent() -> None:
    # 10.15 × 1.1 = 11.165 → 四舍五入 11.17；× 0.9 = 9.135 → 9.14
    up, down = rules.limit_prices(10.15, "600519")
    assert (up, down) == (11.17, 9.14)
    # 用 Decimal 独立复核（避免 binary float 的 str 表示差异）
    assert Decimal("11.165").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == Decimal("11.17")
    assert Decimal("9.135").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == Decimal("9.14")


@pytest.mark.parametrize(
    ("pre_close", "symbol", "is_st", "expected"),
    [
        (100.00, "600519", False, (110.00, 90.00)),
        (100.00, "300750", False, (120.00, 80.00)),
        (100.00, "688981", False, (120.00, 80.00)),
        (5.00, "830799", False, (6.50, 3.50)),
        (10.00, "600519", True, (10.50, 9.50)),
        (3.33, "600519", False, (3.66, 3.00)),  # 3.33×1.1=3.663→3.66；×0.9=2.997→3.00
    ],
)
def test_limit_prices_examples(pre_close, symbol, is_st, expected) -> None:
    assert rules.limit_prices(pre_close, symbol, is_st) == expected


def test_limit_prices_invalid_pre_close() -> None:
    assert rules.limit_prices(0.0, "600519") == (0.0, 0.0)
    assert rules.limit_prices(-1.0, "600519") == (0.0, 0.0)


# ---------------------------------------------------------------------------
# T+1
# ---------------------------------------------------------------------------
def test_t_plus_one_bought_today_not_sellable() -> None:
    position = {"symbol": "600519", "qty": 1000, "available_qty": 0}
    assert rules.can_sell(position, 100) is False
    assert rules.sellable_qty(position) == 0
    # 下一交易日开盘结算后才可卖
    settled = rules.settle_t1(position)
    assert settled["available_qty"] == 1000
    assert settled["qty"] == 1000
    assert rules.can_sell(settled, 1000) is True
    assert rules.can_sell(settled, 1001) is False  # 不允许超卖
    assert rules.sellable_qty(settled, 300) == 300
    assert rules.sellable_qty(settled, 5000) == 1000


def test_settle_t1_does_not_mutate_input() -> None:
    position = {"symbol": "600519", "qty": 500, "available_qty": 0}
    rules.settle_t1(position)
    assert position["available_qty"] == 0  # 原对象不变
    settled_list = rules.settle_positions([position])
    assert settled_list[0]["available_qty"] == 500


# ---------------------------------------------------------------------------
# 费用精确值（契约 §6：佣金 max(额×0.00025,5) 双向；印花税卖出 0.0005；过户费 0.00001 双向）
# ---------------------------------------------------------------------------
def test_fees_buy_exact_values(cfg: dict) -> None:
    fee = rules.fees("buy", 100000.0, cfg)
    assert fee["commission"] == 25.0        # max(100000×0.00025, 5) = 25
    assert fee["stamp_tax"] == 0.0          # 买入不收印花税
    assert fee["transfer_fee"] == 1.0       # 100000×0.00001
    assert fee["total"] == 26.0


def test_fees_sell_exact_values(cfg: dict) -> None:
    fee = rules.fees("sell", 100000.0, cfg)
    assert fee["commission"] == 25.0
    assert fee["stamp_tax"] == 50.0         # 100000×0.0005
    assert fee["transfer_fee"] == 1.0
    assert fee["total"] == 76.0


def test_fees_minimum_commission(cfg: dict) -> None:
    fee = rules.fees("buy", 1000.0, cfg)
    assert fee["commission"] == 5.0         # max(0.25, 5)
    assert fee["stamp_tax"] == 0.0
    assert fee["transfer_fee"] == 0.01      # 1000×0.00001
    assert fee["total"] == 5.01

    sell_fee = rules.fees("sell", 1000.0, cfg)
    assert sell_fee["commission"] == 5.0
    assert sell_fee["stamp_tax"] == 0.5
    assert sell_fee["transfer_fee"] == 0.01
    assert sell_fee["total"] == 5.51


def test_fees_zero_amount_and_side_aliases(cfg: dict) -> None:
    assert rules.fees("buy", 0.0, cfg)["total"] == 0.0
    assert rules.fees("SELL", 100000.0, cfg)["total"] == 76.0
    assert rules.fees("卖出", 100000.0, cfg)["stamp_tax"] == 50.0


def test_fees_uses_custom_cfg() -> None:
    cfg = {"paper": {"commission_rate": 0.0003, "commission_min": 1.0, "stamp_tax_sell": 0.001, "transfer_fee": 0.00002}}
    fee = rules.fees("sell", 10000.0, cfg)
    assert fee["commission"] == 3.0
    assert fee["stamp_tax"] == 10.0
    assert fee["transfer_fee"] == 0.2
    assert fee["total"] == 13.2


# ---------------------------------------------------------------------------
# 滑点边界
# ---------------------------------------------------------------------------
def test_slippage_buy_and_sell_default_bps(cfg: dict) -> None:
    assert rules.slippage_price(10.0, "buy", cfg) == 10.01   # 10.005 四舍五入到分
    assert rules.slippage_price(10.0, "sell", cfg) == 10.00  # 9.995 → 10.00
    # 契约原始公式（不舍入到分）
    assert rules.slippage_price(10.0, "buy", cfg, tick_round=False) == pytest.approx(10.005, abs=1e-9)
    assert rules.slippage_price(10.0, "sell", cfg, tick_round=False) == pytest.approx(9.995, abs=1e-9)


def test_slippage_clamped_by_limit_prices(cfg: dict) -> None:
    # 买入价不得超过涨停价
    assert rules.slippage_price(10.0, "buy", cfg, limit_up=9.99) == 9.99
    # 卖出价不得低于跌停价
    assert rules.slippage_price(10.0, "sell", cfg, limit_down=10.01) == 10.01
    # 未触发边界时按公式
    assert rules.slippage_price(10.0, "buy", cfg, limit_up=11.00) == 10.01
    assert rules.slippage_price(10.0, "sell", cfg, limit_down=9.00) == 10.00


def test_slippage_custom_bps_and_invalid_open() -> None:
    cfg = {"paper": {"slippage_bps": 20}}  # 20bps = 0.2%
    assert rules.slippage_price(10.0, "buy", cfg) == 10.02
    assert rules.slippage_price(10.0, "sell", cfg) == 9.98
    assert rules.slippage_price(0.0, "buy", cfg) == 0.0
    assert rules.slippage_price(-5.0, "sell", cfg) == 0.0


# ---------------------------------------------------------------------------
# 整手 / 含费资金上限
# ---------------------------------------------------------------------------
def test_round_lot() -> None:
    assert rules.round_lot(1234) == 1200
    assert rules.round_lot(1999) == 1900
    assert rules.round_lot(100) == 100
    assert rules.round_lot(99) == 0
    assert rules.round_lot(0) == 0
    assert rules.round_lot(-100) == 0
    assert rules.round_lot(250, lot=200) == 200


def test_lot_size_from_cfg(cfg: dict) -> None:
    assert rules.lot_size(cfg) == 100
    assert rules.lot_size({"paper": {"lot_size": 200}}) == 200
    assert rules.lot_size({}) == 100


def test_max_affordable_qty_includes_fees(cfg: dict) -> None:
    # 10000 元 @10.00：1000 股需 10000 + 5.00(佣金) + 0.10(过户费) = 10005.10 > 10000 → 只能 900 股
    assert rules.max_affordable_qty(10000.0, 10.0, cfg) == 900
    # 资金刚好够 1000 股含费：10000 + 5 + 0.10 = 10005.10
    assert rules.max_affordable_qty(10006.0, 10.0, cfg) == 1000
    # 连一手都买不起
    assert rules.max_affordable_qty(1000.0, 10.0, cfg) == 0
    assert rules.max_affordable_qty(0.0, 10.0, cfg) == 0
    assert rules.max_affordable_qty(100000.0, 0.0, cfg) == 0


# ---------------------------------------------------------------------------
# 涨跌停开盘判定
# ---------------------------------------------------------------------------
def test_limit_touch_at_open() -> None:
    assert rules.is_limit_up_at_open(11.00, 11.00) is True
    assert rules.is_limit_up_at_open(10.99, 11.00) is False
    assert rules.is_limit_up_at_open(10.50, 0.0) is False
    assert rules.is_limit_down_at_open(9.00, 9.00) is True
    assert rules.is_limit_down_at_open(9.01, 9.00) is False
    assert rules.is_limit_down_at_open(9.00, 0.0) is False
