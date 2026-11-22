"""供 tests/test_paper_engine.py / tests/test_api.py 共用的离线 fixture。

按新存储布局构造契约 §3.1 行情（Parquet：``daily_bars/symbol=<code>/daily_bars.parquet``）
与 §3.3 信号（DuckDB ``signals`` 表），不联网、不依赖 data-dev。
每个场景返回 ``(bars, signals)``，用 ``create_db(path, bars, signals)`` 落到临时库；
``path`` 是 DuckDB 状态库文件，Parquet 行情由 ``quant.data.store.parquet_root`` 推导。
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from quant.data import db as duckdb_conn
from quant.data import store
from quant.data.models import DAILY_COLUMNS

PROB_BY_DIRECTION = {
    "up": (0.55, 0.25, 0.20),
    "flat": (0.30, 0.40, 0.30),
    "down": (0.20, 0.25, 0.55),
}

TOP_FEATURES = [
    {"name": "ret_5d", "value": 0.031, "contrib": 0.082},
    {"name": "ma20_dev", "value": -0.012, "contrib": -0.045},
    {"name": "rsi_14", "value": 58.2, "contrib": 0.021},
    {"name": "vol_ratio", "value": 1.35, "contrib": 0.015},
    {"name": "turnover_rate", "value": 1.02, "contrib": -0.008},
    {"name": "macd_hist", "value": 0.004, "contrib": 0.003},  # 第 6 项应被 API 截断
]


def bar(
    symbol: str,
    trade_date: str,
    *,
    pre_close: float,
    open: float,
    close: float,
    high: float | None = None,
    low: float | None = None,
    limit_up: float | None = None,
    limit_down: float | None = None,
    volume: int = 100000,
    turnover_rate: float = 1.0,
    is_suspended: int = 0,
    is_st: int = 0,
) -> dict:
    """构造一行日线（limit 缺省时由 pre_close 按主板 10% 粗算，测试里建议显式给）。"""
    if limit_up is None:
        limit_up = round(pre_close * 1.10 + 1e-9, 2)
    if limit_down is None:
        limit_down = round(pre_close * 0.90 + 1e-9, 2)
    if high is None:
        high = max(open, close)
    if low is None:
        low = min(open, close)
    amount = round(volume * close, 2)
    pct_chg = round((close / pre_close - 1.0) * 100.0, 4) if pre_close else 0.0
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "pre_close": pre_close,
        "volume": int(volume),
        "amount": amount,
        "pct_chg": pct_chg,
        "turnover_rate": turnover_rate,
        "adj_factor": 1.0,
        "adj_close": close,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "is_suspended": int(is_suspended),
        "is_st": int(is_st),
    }


def signal(
    signal_id: str,
    symbol: str,
    trade_date: str,
    *,
    asof: str,
    price: float,
    direction: str = "up",
    expires_at: str,
    status: str = "ok",
    horizon: str = "1d",
    model_version: str = "v0.1.0",
    top_features: list[dict] | None = None,
    source: str = "local",
    created_at: str | None = None,
) -> dict:
    """构造一行契约 §3.3 信号；prob 之和为 1，confidence = max(prob)。"""
    pu, pf, pd = PROB_BY_DIRECTION[direction]
    features = TOP_FEATURES if top_features is None else top_features
    features = sorted(features, key=lambda item: abs(item["contrib"]), reverse=True)[:5]
    return {
        "signal_id": signal_id,
        "symbol": symbol,
        "asof": asof,
        "trade_date": trade_date,
        "price": price,
        "direction": direction,
        "prob_up": pu,
        "prob_flat": pf,
        "prob_down": pd,
        "confidence": round(max(pu, pf, pd), 4),
        "horizon": horizon,
        "expires_at": expires_at,
        "model_version": model_version,
        "top_features": json.dumps(features, ensure_ascii=False),
        "source": source,
        "status": status,
        "created_at": created_at or asof,
    }


def create_db(path: str | Path, bars: list[dict], signals: list[dict]) -> Path:
    """建临时库：日线写 Parquet、信号写 DuckDB（覆盖同名临时库与 Parquet 目录）。"""
    db = Path(path)
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    root = store.parquet_root(db)
    shutil.rmtree(root, ignore_errors=True)
    store.init_db(db)
    if bars:
        import pandas as pd  # 延迟导入，fixture 被 CLI 脚本直接复用时也能用

        frame = pd.DataFrame(bars)
        frame = frame[[c for c in DAILY_COLUMNS if c in frame.columns]]
        store.upsert_daily(frame, db)
    if signals:
        # asof 在 DuckDB 里是保留字（AS OF），列名统一加双引号
        keys = list(signals[0].keys())
        sql = "INSERT OR REPLACE INTO signals (%s) VALUES (%s)" % (
            ",".join(f'"{k}"' for k in keys),
            ",".join("?" for _ in keys),
        )
        with duckdb_conn.connect(db) as conn:
            conn.executemany(sql, [tuple(row[k] for k in keys) for row in signals])
    return db


# ---------------------------------------------------------------------------
# 读库小工具（测试直接断言 DuckDB paper_* 表时用）
# ---------------------------------------------------------------------------
def fetch_rows(path: str | Path, sql: str, params: list | tuple = ()) -> list[dict]:
    """只读查询临时 DuckDB，返回 ``[{列名: 值}]``。"""
    with duckdb_conn.connect(path, read_only=True) as conn:
        return duckdb_conn.fetch_dicts(conn, sql, list(params))


def execute_sql(path: str | Path, sql: str, params: list | tuple = ()) -> None:
    """在临时 DuckDB 上执行一条写 SQL（测试构造边界状态用）。"""
    with duckdb_conn.connect(path) as conn:
        conn.execute(sql, list(params))


# ---------------------------------------------------------------------------
# 场景 A：3 个连续交易日，一买一卖；用于精确断言现金/持仓/净值/胜率
# ---------------------------------------------------------------------------
def scenario_a() -> tuple[list[dict], list[dict]]:
    """2026-11-16 信号 up → 11-17 开盘买入；11-17 信号 down → 11-18 开盘卖出。"""
    bars = [
        bar("600519", "2026-11-16", pre_close=10.00, open=10.00, close=10.00, limit_up=11.00, limit_down=9.00),
        bar("600519", "2026-11-17", pre_close=10.00, open=10.50, close=10.80, high=10.90, low=10.40, limit_up=11.00, limit_down=9.00),
        bar("600519", "2026-11-18", pre_close=10.80, open=10.90, close=11.20, high=11.30, low=10.70, limit_up=11.88, limit_down=9.72),
        bar("600519", "2026-11-19", pre_close=11.20, open=11.10, close=11.00, high=11.30, low=10.90, limit_up=12.32, limit_down=10.08),
    ]
    signals = [
        signal(
            "600519-2026-11-16-20261116T150500",
            "600519",
            "2026-11-16",
            asof="2026-11-16T15:05:00+08:00",
            expires_at="2026-11-17T09:30:00+08:00",
            price=10.00,
            direction="up",
        ),
        signal(
            "600519-2026-11-17-20261117T150500",
            "600519",
            "2026-11-17",
            asof="2026-11-17T15:05:00+08:00",
            expires_at="2026-11-18T09:30:00+08:00",
            price=10.80,
            direction="down",
        ),
    ]
    return bars, signals


# ---------------------------------------------------------------------------
# 场景 B：T+1 执行日开盘即涨停 → 买入被拒
# ---------------------------------------------------------------------------
def scenario_limit_up() -> tuple[list[dict], list[dict]]:
    bars = [
        bar("600519", "2026-11-16", pre_close=10.00, open=10.00, close=10.00, limit_up=11.00, limit_down=9.00),
        bar("600519", "2026-11-17", pre_close=10.00, open=11.00, close=11.00, high=11.00, low=11.00, limit_up=11.00, limit_down=9.00),
    ]
    signals = [
        signal(
            "600519-2026-11-16-20261116T150500",
            "600519",
            "2026-11-16",
            asof="2026-11-16T15:05:00+08:00",
            expires_at="2026-11-17T09:30:00+08:00",
            price=10.00,
            direction="up",
        )
    ]
    return bars, signals


# ---------------------------------------------------------------------------
# 场景 C：已持仓，T+1 执行日开盘即跌停 → 卖出被拒（持仓保留）
# ---------------------------------------------------------------------------
def scenario_limit_down() -> tuple[list[dict], list[dict]]:
    bars = [
        bar("600519", "2026-11-16", pre_close=10.00, open=10.00, close=10.00, limit_up=11.00, limit_down=9.00),
        bar("600519", "2026-11-17", pre_close=10.00, open=10.50, close=10.80, high=10.90, low=10.40, limit_up=11.00, limit_down=9.00),
        bar("600519", "2026-11-18", pre_close=10.80, open=9.72, close=9.80, high=9.90, low=9.72, limit_up=11.88, limit_down=9.72),
    ]
    signals = [
        signal(
            "600519-2026-11-16-20261116T150500",
            "600519",
            "2026-11-16",
            asof="2026-11-16T15:05:00+08:00",
            expires_at="2026-11-17T09:30:00+08:00",
            price=10.00,
            direction="up",
        ),
        signal(
            "600519-2026-11-17-20261117T150500",
            "600519",
            "2026-11-17",
            asof="2026-11-17T15:05:00+08:00",
            expires_at="2026-11-18T09:30:00+08:00",
            price=10.80,
            direction="down",
        ),
    ]
    return bars, signals


# ---------------------------------------------------------------------------
# 场景 D：T+1 执行日停牌 → 订单顺延到下一交易日成交
# ---------------------------------------------------------------------------
def scenario_suspended() -> tuple[list[dict], list[dict]]:
    bars = [
        bar("600519", "2026-11-16", pre_close=10.00, open=10.00, close=10.00, limit_up=11.00, limit_down=9.00),
        bar(
            "600519",
            "2026-11-17",
            pre_close=10.00,
            open=10.00,
            close=10.00,
            high=10.00,
            low=10.00,
            limit_up=11.00,
            limit_down=9.00,
            is_suspended=1,
        ),
        bar("600519", "2026-11-18", pre_close=10.00, open=10.50, close=10.80, high=10.90, low=10.40, limit_up=11.00, limit_down=9.00),
    ]
    signals = [
        signal(
            "600519-2026-11-16-20261116T150500",
            "600519",
            "2026-11-16",
            asof="2026-11-16T15:05:00+08:00",
            expires_at="2026-11-17T09:30:00+08:00",
            price=10.00,
            direction="up",
        )
    ]
    return bars, signals


# ---------------------------------------------------------------------------
# 场景 E：满仓上限 + 小资金 → 验证整手与含费资金上限
# ---------------------------------------------------------------------------
def scenario_lot_rounding() -> tuple[list[dict], list[dict]]:
    bars = [
        bar("600519", "2026-11-16", pre_close=10.00, open=10.00, close=10.00, limit_up=11.00, limit_down=9.00),
        bar("600519", "2026-11-17", pre_close=10.00, open=10.00, close=10.50, high=10.60, low=9.95, limit_up=11.00, limit_down=9.00),
    ]
    signals = [
        signal(
            "600519-2026-11-16-20261116T150500",
            "600519",
            "2026-11-16",
            asof="2026-11-16T15:05:00+08:00",
            expires_at="2026-11-17T09:30:00+08:00",
            price=10.00,
            direction="up",
        )
    ]
    return bars, signals


# ---------------------------------------------------------------------------
# 场景 F：买入后次日下跌卖出 → 交易胜率统计里的亏损平仓
# ---------------------------------------------------------------------------
def scenario_losing() -> tuple[list[dict], list[dict]]:
    bars = [
        bar("600519", "2026-11-16", pre_close=10.00, open=10.00, close=10.00, limit_up=11.00, limit_down=9.00),
        bar("600519", "2026-11-17", pre_close=10.00, open=10.50, close=10.80, high=10.90, low=10.40, limit_up=11.00, limit_down=9.00),
        bar("600519", "2026-11-18", pre_close=10.80, open=10.00, close=10.10, high=10.20, low=10.00, limit_up=11.88, limit_down=9.72),
    ]
    signals = [
        signal(
            "600519-2026-11-16-20261116T150500",
            "600519",
            "2026-11-16",
            asof="2026-11-16T15:05:00+08:00",
            expires_at="2026-11-17T09:30:00+08:00",
            price=10.00,
            direction="up",
        ),
        signal(
            "600519-2026-11-17-20261117T150500",
            "600519",
            "2026-11-17",
            asof="2026-11-17T15:05:00+08:00",
            expires_at="2026-11-18T09:30:00+08:00",
            price=10.80,
            direction="down",
        ),
    ]
    return bars, signals


# ---------------------------------------------------------------------------
# 配置辅助
# ---------------------------------------------------------------------------
def make_cfg(db_path: str | Path, *, initial_cash: float = 100000, symbols: tuple[str, ...] = ("600519",), **paper_overrides) -> dict:
    """用项目默认 config 深拷贝出测试配置，替换 db 路径/资金/标的。"""
    from quant.config import load_config  # 延迟导入，fixture 被 CLI 脚本复用时也能用

    cfg = copy.deepcopy(load_config())
    cfg.setdefault("data", {})["db"] = str(db_path)
    # data.dir 也指向临时目录，避免测试读到仓库里的 data/reports/signals.json
    cfg["data"]["dir"] = str(Path(db_path).parent / "data")
    cfg["symbols"] = list(symbols)
    paper = cfg.setdefault("paper", {})
    paper["initial_cash"] = float(initial_cash)
    paper.update(paper_overrides)
    return cfg
