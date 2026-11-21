"""A股交易规则（契约 §6；回测与模拟盘共用，入口见契约 §9）。

本模块只放纯函数：不读写数据库、不依赖当前时间/随机数，方便单测与回测复用。

价格 / 金额口径（与契约 §6 的差异仅在最后一步舍入，均按 A 股实际规则）：
- 涨跌停价四舍五入到分（Decimal ROUND_HALF_UP，不用 Python round 的银行家舍入）；
- 滑点价默认同样落到分（tick_round=True），需要契约原始公式时传 tick_round=False；
- 费用先按契约公式计算，最后一步四舍五入到分（交易所/券商按分收取）。

T+1：当日买入的股数当日不可卖，下一交易日开盘由 ``settle_t1`` 结算为可卖。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

# 契约 §6 默认参数（与 config/default.yaml 的 paper 段一致，cfg 缺省时兜底）
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_TAX_SELL = 0.0005
TRANSFER_FEE = 0.00001
SLIPPAGE_BPS = 5.0
LOT_SIZE = 100
MAX_POSITION_PCT = 0.2

# 订单被拒原因（引擎写入 paper_orders.reject_reason，回测/前端复用同一套字符串）
REJECT_LIMIT_UP = "limit_up"          # 开盘触及涨停，买入无法成交
REJECT_LIMIT_DOWN = "limit_down"      # 开盘触及跌停，卖出无法成交
REJECT_T_PLUS_ONE = "t_plus_one"      # T+1：当日买入股份不可卖
REJECT_INSUFFICIENT_CASH = "insufficient_cash"
REJECT_NO_POSITION = "no_position"
REJECT_POSITION_LIMIT = "position_limit"
REJECT_LOT_SIZE = "lot_size"          # 不足一手
REJECT_NO_PRICE = "no_price"          # 行情价格缺失/为 0

_CENT = Decimal("0.01")
_ONE = Decimal("1")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _paper_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """安全取 cfg['paper'] 段。"""
    if not isinstance(cfg, dict):
        return {}
    paper = cfg.get("paper")
    return paper if isinstance(paper, dict) else {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


def _norm_side(side: Any) -> str:
    """统一买卖方向为 'buy' / 'sell'。"""
    text = str(side or "").strip().lower()
    if text in {"sell", "s", "卖出", "卖"}:
        return "sell"
    return "buy"


def round_cents(value: float | Decimal | int) -> float:
    """四舍五入到分（A 股口径，ROUND_HALF_UP）。"""
    return float(Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP))


def _code(symbol: Any) -> str:
    """从 '600519' / '600519.SH' 里提取 6 位数字代码。"""
    digits = "".join(ch for ch in str(symbol or "") if ch.isdigit())
    return digits[:6] if digits else str(symbol or "").strip().upper()


# ---------------------------------------------------------------------------
# 涨跌停
# ---------------------------------------------------------------------------
def limit_pct(symbol: str, is_st: bool = False) -> float:
    """按板块返回涨跌幅比例。

    契约 §6：主板（600/601/603/605/000/001/002/003）±10%；ST ±5%；
    创业板 300/301、科创板 688 ±20%；北交所 8/4 开头 ±30%。
    ST 标记优先（契约把 ST 单列为 ±5%；若该股为创业板/科创板 ST，契约未细分，
    这里按字面优先取 5%，需要改口径只需调整本函数）。
    """
    if is_st:
        return 0.05
    code = _code(symbol)
    if code.startswith(("300", "301", "688")):
        return 0.20
    if code.startswith(("8", "4")):
        return 0.30
    if code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return 0.10
    return 0.10


def limit_prices(pre_close: float, symbol: str, is_st: bool = False) -> tuple[float, float]:
    """返回 (涨停价, 跌停价)，四舍五入到分。pre_close <= 0 时返回 (0.0, 0.0)。"""
    pc = _as_float(pre_close)
    if pc <= 0:
        return (0.0, 0.0)
    pct = Decimal(str(limit_pct(symbol, is_st)))
    base = Decimal(str(pc))
    up = (base * (_ONE + pct)).quantize(_CENT, rounding=ROUND_HALF_UP)
    down = (base * (_ONE - pct)).quantize(_CENT, rounding=ROUND_HALF_UP)
    return (float(up), float(down))


def is_limit_up_at_open(open_price: float, limit_up: float, eps: float = 1e-6) -> bool:
    """开盘价是否触及涨停（买入不可成交）。"""
    op, lu = _as_float(open_price), _as_float(limit_up)
    return lu > 0 and op >= lu - eps


def is_limit_down_at_open(open_price: float, limit_down: float, eps: float = 1e-6) -> bool:
    """开盘价是否触及跌停（卖出不可成交）。"""
    op, ld = _as_float(open_price), _as_float(limit_down)
    return ld > 0 and op <= ld + eps


# ---------------------------------------------------------------------------
# 费用
# ---------------------------------------------------------------------------
def fees(side: str, amount: float, cfg: dict[str, Any] | None = None) -> dict[str, float]:
    """按契约 §6 计算一笔成交的费用。

    佣金 max(成交额 × commission_rate, commission_min) 双向；
    印花税 卖出 成交额 × stamp_tax_sell；
    过户费 成交额 × transfer_fee 双向。
    每项及合计四舍五入到分（money 以元为单位）。
    """
    p = _paper_cfg(cfg)
    rate = _as_float(p.get("commission_rate"), COMMISSION_RATE)
    min_comm = _as_float(p.get("commission_min"), COMMISSION_MIN)
    stamp_rate = _as_float(p.get("stamp_tax_sell"), STAMP_TAX_SELL)
    transfer_rate = _as_float(p.get("transfer_fee"), TRANSFER_FEE)

    amt = max(_as_float(amount), 0.0)
    if amt <= 0:
        return {"commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0, "total": 0.0}

    commission = round_cents(max(amt * rate, min_comm))
    stamp = round_cents(amt * stamp_rate) if _norm_side(side) == "sell" else 0.0
    transfer = round_cents(amt * transfer_rate)
    total = round_cents(commission + stamp + transfer)
    return {
        "commission": commission,
        "stamp_tax": stamp,
        "transfer_fee": transfer,
        "total": total,
    }


# ---------------------------------------------------------------------------
# 滑点
# ---------------------------------------------------------------------------
def slippage_price(
    open_price: float,
    side: str,
    cfg: dict[str, Any] | None = None,
    limit_up: float | None = None,
    limit_down: float | None = None,
    tick_round: bool = True,
) -> float:
    """按契约 §6 计算滑点成交价。

    买入价 = min(open × (1+bps), limit_up)；卖出价 = max(open × (1-bps), limit_down)。
    tick_round=True（默认）时最后四舍五入到分；False 返回公式原始值（浮点清理到 10 位有效数字）。
    """
    p = _paper_cfg(cfg)
    bps = _as_float(p.get("slippage_bps"), SLIPPAGE_BPS)
    op = _as_float(open_price)
    if op <= 0:
        return 0.0
    # 用 Decimal 计算，避免 10×1.0005 = 10.004999… 这类二进制误差影响分位舍入
    op_dec = Decimal(str(op))
    ratio = Decimal(str(bps)) / Decimal("10000")
    if _norm_side(side) == "buy":
        price = op_dec * (_ONE + ratio)
        if limit_up is not None and _as_float(limit_up) > 0:
            price = min(price, Decimal(str(_as_float(limit_up))))
    else:
        price = op_dec * (_ONE - ratio)
        if limit_down is not None and _as_float(limit_down) > 0:
            price = max(price, Decimal(str(_as_float(limit_down))))
    price = max(price, Decimal("0.01"))  # A 股最小报价 0.01 元
    if tick_round:
        return float(price.quantize(_CENT, rounding=ROUND_HALF_UP))
    return float(price)


# ---------------------------------------------------------------------------
# 整手 / 可买数量
# ---------------------------------------------------------------------------
def lot_size(cfg: dict[str, Any] | None = None) -> int:
    """一手股数，默认 100。"""
    lot = int(_as_float(_paper_cfg(cfg).get("lot_size"), LOT_SIZE))
    return max(lot, 1)


def round_lot(qty: float, lot: int = LOT_SIZE) -> int:
    """买入数量向下取整到整手。"""
    lot = max(int(lot), 1)
    try:
        value = int(qty)
    except (TypeError, ValueError):
        return 0
    return max(value // lot * lot, 0)


def max_affordable_qty(cash: float, price: float, cfg: dict[str, Any] | None = None) -> int:
    """资金约束下可买的最大整手股数（已含买入费用）。

    - 先按 现金/价格 估上界，再逐手回退到「成交额 + 买入费用 <= 现金」；
    - 返回 0 表示一手都买不起。
    """
    lot = lot_size(cfg)
    cash_f, price_f = _as_float(cash), _as_float(price)
    if cash_f <= 0 or price_f <= 0:
        return 0
    qty = int(cash_f // price_f) // lot * lot
    while qty > 0:
        amount = round_cents(qty * price_f)
        total = round_cents(amount + fees("buy", amount, cfg)["total"])
        if total <= cash_f + 1e-9:
            return qty
        qty -= lot
    return 0


# ---------------------------------------------------------------------------
# T+1 / 可卖数量
# ---------------------------------------------------------------------------
def settle_t1(position: dict[str, Any]) -> dict[str, Any]:
    """T+1 开盘结算：把该持仓全部股数置为可卖（返回新 dict，不改入参）。"""
    out = dict(position or {})
    qty = max(int(_as_float(out.get("qty"))), 0)
    out["qty"] = qty
    out["available_qty"] = qty
    return out


def settle_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量 T+1 结算。"""
    return [settle_t1(p) for p in positions or []]


def can_sell(position: dict[str, Any] | None, qty: int) -> bool:
    """是否允许卖出 qty 股（T+1：只能卖 available_qty 以内的部分）。"""
    avail = max(int(_as_float((position or {}).get("available_qty"))), 0)
    try:
        q = int(qty)
    except (TypeError, ValueError):
        return False
    return 0 < q <= avail


def sellable_qty(position: dict[str, Any] | None, requested: int | None = None) -> int:
    """可卖股数；requested 给定时取 min(requested, available)，卖出允许零股。"""
    avail = max(int(_as_float((position or {}).get("available_qty"))), 0)
    if requested is None:
        return avail
    try:
        req = int(requested)
    except (TypeError, ValueError):
        return 0
    return max(min(req, avail), 0)


__all__ = [
    "COMMISSION_RATE",
    "COMMISSION_MIN",
    "STAMP_TAX_SELL",
    "TRANSFER_FEE",
    "SLIPPAGE_BPS",
    "LOT_SIZE",
    "MAX_POSITION_PCT",
    "REJECT_LIMIT_UP",
    "REJECT_LIMIT_DOWN",
    "REJECT_T_PLUS_ONE",
    "REJECT_INSUFFICIENT_CASH",
    "REJECT_NO_POSITION",
    "REJECT_POSITION_LIMIT",
    "REJECT_LOT_SIZE",
    "REJECT_NO_PRICE",
    "round_cents",
    "limit_pct",
    "limit_prices",
    "is_limit_up_at_open",
    "is_limit_down_at_open",
    "fees",
    "slippage_price",
    "lot_size",
    "round_lot",
    "max_affordable_qty",
    "settle_t1",
    "settle_positions",
    "can_sell",
    "sellable_qty",
]
