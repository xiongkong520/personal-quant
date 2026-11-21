"""离线合成日线 fixture（model-dev 自测用，不联网）。

用途
----
1. 生成与契约 §3.1 字段完全一致的合成日线 CSV（``synthetic_daily.csv``），
   供特征 / 模型 / 回测测试使用；
2. 提供 ``write_daily_db(df, db_path)`` 把合成日线写成本地 SQLite 库，
   这样在真实 ``data/db/quant.db`` 就绪前也能把 train / backtest / signal
   全链路跑通。

设计要点
--------
- 收益过程带一阶自相关（momentum）+ 市场因子 + 特质噪声，让特征有可学信号；
- 收益截断在涨跌停幅度内，并注入少量"一字涨停 / 一字跌停"事件，
  用于验证回测的涨跌停拒单逻辑；
- 注入停牌日（``is_suspended=1``，成交量为 0）用于验证顺延逻辑；
- ``adj_factor`` 固定 1.0（另提供 ``make_dividend_frame`` 单独验证前复权口径）；
- 全部由 ``numpy.random.default_rng(seed)`` 确定，可复现。
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

FIXTURE_DIR = Path(__file__).resolve().parent
CSV_PATH = FIXTURE_DIR / "synthetic_daily.csv"

SYMBOLS: tuple[str, ...] = ("600519", "000001", "300750")
BASE_PRICE = {"600519": 1680.0, "000001": 12.5, "300750": 185.0}
FLOAT_SHARES = {"600519": 1.256e9, "000001": 1.94e10, "300750": 1.6e9}

DAILY_COLUMNS = [
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "pct_chg",
    "turnover_rate",
    "adj_factor",
    "adj_close",
    "limit_up",
    "limit_down",
    "is_suspended",
]


def limit_pct_for(symbol: str) -> float:
    """按契约 §6 的板块规则给出涨跌停比例（fixture 简化用）。"""
    if symbol.startswith(("300", "301", "688")):
        return 0.20
    if symbol.startswith(("8", "4")):
        return 0.30
    return 0.10


def _round_price(x: float) -> float:
    """四舍五入到分（A 股报价规则，不用 Python 银行家舍入）。"""
    return math.floor(float(x) * 100 + 0.5) / 100


def make_daily(
    symbols: tuple[str, ...] | list[str] = SYMBOLS,
    start: str = "2022-01-04",
    end: str = "2026-11-20",
    seed: int = 42,
) -> pd.DataFrame:
    """生成多只股票的合成日线（行 = symbol × 交易日）。"""
    rng = np.random.default_rng(seed)
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]
    n = len(dates)
    if n < 10:
        raise ValueError("交易日太少，无法生成合成日线")

    # 市场因子（所有股票共享，造成截面相关）
    mkt_ret = rng.normal(0.0002, 0.0085, n)
    frames: list[pd.DataFrame] = []

    for k, sym in enumerate(symbols):
        pct = limit_pct_for(sym)
        beta = 0.85 + 0.25 * k
        eps = rng.normal(0.0, 0.0115, n)
        ret = np.zeros(n)
        for i in range(1, n):
            # 一阶自相关 + 市场因子 + 特质噪声 + 微小漂移 → 特征可学
            ret[i] = (
                0.0002 * (1 if k % 2 == 0 else -1)
                + 0.28 * ret[i - 1]
                + beta * 0.55 * mkt_ret[i]
                + eps[i]
            )
        cap = pct - 0.01
        ret = np.clip(ret, -cap, cap)
        gap = rng.normal(0.0, 0.0035, n)
        gap[0] = 0.0

        # 注入一字板事件：open=high=low=close=涨/跌停价
        for idx, sign in ((900, +1), (1050, -1)):
            if 0 < idx < n:
                ret[idx] = sign * pct
                gap[idx] = sign * pct

        # 由 close-to-close 收益串出价格序列
        close = np.empty(n)
        pre_close = np.empty(n)
        close[0] = BASE_PRICE.get(sym, 50.0)
        pre_close[0] = _round_price(close[0] / (1.0 + ret[0]))
        for i in range(1, n):
            pre_close[i] = close[i - 1]
            close[i] = pre_close[i] * (1.0 + ret[i])
        open_ = pre_close * (1.0 + gap)
        high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.0025, n)))
        low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.0025, n)))

        limit_up = np.array([_round_price(p * (1.0 + pct)) for p in pre_close])
        limit_down = np.array([_round_price(p * (1.0 - pct)) for p in pre_close])

        open_, high, low, close = (
            np.round(v, 2) for v in (open_, high, low, close)
        )
        pre_close = np.round(pre_close, 2)
        # 保证 OHLC 自洽
        high = np.maximum.reduce([high, open_, close])
        low = np.minimum.reduce([low, open_, close])

        base_vol = FLOAT_SHARES.get(sym, 5e8) * 0.0025
        volume = base_vol * np.exp(rng.normal(0.0, 0.35, n)) * (1.0 + 6.0 * np.abs(ret))
        volume = np.maximum(volume, 100.0)

        # 一字板当天成交清淡
        for idx in (900, 1050):
            if 0 < idx < n:
                volume[idx] = base_vol * 0.15
        volume = volume.astype("int64")

        frame = pd.DataFrame(
            {
                "symbol": sym,
                "trade_date": dates,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "pre_close": pre_close,
                "volume": volume,
                "amount": np.round(volume * close, 2),
                "pct_chg": np.round((close / pre_close - 1.0) * 100.0, 2),
                "turnover_rate": np.round(volume / FLOAT_SHARES.get(sym, 5e8) * 100.0, 4),
                "adj_factor": 1.0,
                "adj_close": close,
                "limit_up": limit_up,
                "limit_down": limit_down,
                "is_suspended": 0,
            }
        )

        # 停牌日：价格冻结、无成交（真实库里停牌日通常不写行，这里保留行以测顺延逻辑）
        suspend_offsets = {"600519": (400,), "000001": (650,), "300750": (800,)}.get(sym, ())
        for idx in suspend_offsets:
            if 0 < idx < n:
                frame.loc[idx, ["open", "high", "low", "close"]] = frame.loc[idx, "pre_close"]
                frame.loc[idx, "volume"] = 0
                frame.loc[idx, "amount"] = 0.0
                frame.loc[idx, "pct_chg"] = 0.0
                frame.loc[idx, "turnover_rate"] = 0.0
                frame.loc[idx, "is_suspended"] = 1
                # 下一交易日前收 = 停牌日收盘（价格冻结）
                if idx + 1 < n:
                    frame.loc[idx + 1, "pre_close"] = frame.loc[idx, "close"]
                    frame.loc[idx + 1, "limit_up"] = _round_price(
                        frame.loc[idx, "close"] * (1.0 + pct)
                    )
                    frame.loc[idx + 1, "limit_down"] = _round_price(
                        frame.loc[idx, "close"] * (1.0 - pct)
                    )

        frames.append(frame)

    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    return out[DAILY_COLUMNS]


def make_dividend_frame() -> pd.DataFrame:
    """构造含分红除权的小样本，用来验证"标签/收益基于前复权价"的口径。

    第 4 行（0 基）是除权日：不复权价从 10.3 跳到 10.0（-2.91%），
    但 adj_factor 从 0.97 变为 1.0，前复权口径下只是 +0.09%（flat）。
    """
    dates = pd.bdate_range("2024-06-03", periods=8).strftime("%Y-%m-%d").tolist()
    close = [10.0, 10.1, 10.2, 10.3, 10.0, 10.2, 10.3, 10.4]
    adj_factor = [0.97, 0.97, 0.97, 0.97, 1.0, 1.0, 1.0, 1.0]
    rows = []
    prev = _round_price(close[0] / 1.01)
    for i, d in enumerate(dates):
        c = float(close[i])
        rows.append(
            {
                "symbol": "600000",
                "trade_date": d,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "pre_close": prev,
                "volume": 1_000_000,
                "amount": c * 1_000_000,
                "pct_chg": round((c / prev - 1.0) * 100.0, 2),
                "turnover_rate": 0.1,
                "adj_factor": adj_factor[i],
                "adj_close": round(c * adj_factor[i], 6),
                "limit_up": _round_price(prev * 1.1),
                "limit_down": _round_price(prev * 0.9),
                "is_suspended": 0,
            }
        )
        prev = c
    return pd.DataFrame(rows, columns=DAILY_COLUMNS)


def load_fixture_csv(path: Path | None = None) -> pd.DataFrame:
    """读取已提交的合成日线 CSV（类型与契约一致）。"""
    df = pd.read_csv(path or CSV_PATH, dtype={"symbol": str, "trade_date": str})
    return df[DAILY_COLUMNS]


def write_daily_db(df: pd.DataFrame, db_path: str | Path) -> Path:
    """把日线 DataFrame 写成契约 §3.1 的 SQLite ``daily_bars`` 表。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    cols = ", ".join(DAILY_COLUMNS)
    placeholders = ", ".join("?" for _ in DAILY_COLUMNS)
    ddl = f"""
    CREATE TABLE daily_bars (
        symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, pre_close REAL,
        volume INTEGER, amount REAL, pct_chg REAL, turnover_rate REAL,
        adj_factor REAL DEFAULT 1.0, adj_close REAL,
        limit_up REAL, limit_down REAL, is_suspended INTEGER DEFAULT 0,
        PRIMARY KEY (symbol, trade_date)
    );
    """
    records = [
        tuple(None if pd.isna(v) else (int(v) if isinstance(v, (np.integer,)) else float(v) if isinstance(v, (np.floating,)) else v)
              for v in row)
        for row in df[DAILY_COLUMNS].itertuples(index=False, name=None)
    ]
    with sqlite3.connect(db_path) as con:
        con.execute(ddl)
        con.executemany(
            f"INSERT INTO daily_bars ({cols}) VALUES ({placeholders})", records
        )
        con.commit()
    return db_path


GENERATED_DIR = FIXTURE_DIR / ".generated"
SYNTH_DB_PATH = GENERATED_DIR / "quant_synth.db"


def make_synth_db(df: pd.DataFrame | None = None, path: Path | None = None) -> Path:
    """把合成日线写成 SQLite 库，供 ``synth_config.yaml`` 的 CLI 全链路自测使用。"""
    frame = make_daily() if df is None else df
    target = Path(path) if path is not None else SYNTH_DB_PATH
    write_daily_db(frame, target)
    return target


def main() -> None:
    """生成并保存合成日线 CSV + 合成 SQLite 库（开发时手动跑一次即可）。"""
    df = make_daily()
    df.to_csv(CSV_PATH, index=False)
    db = make_synth_db(df)
    print(f"wrote {CSV_PATH} rows={len(df)} symbols={df['symbol'].nunique()}")
    print(f"wrote {db}")
    print(df.head(3).to_string(index=False))


if __name__ == "__main__":
    main()
