"""数据转换与采集层离线单测（不联网）。

覆盖契约 §3.1 / §3.2 / §11：
- 成交量「手 → 股」×100；
- ``pre_close = 收盘 - 涨跌额``（不复权）；
- ``close`` 保持不复权、``adj_close`` 来自 qfq 收盘、``adj_factor`` 推导；
- 涨跌停价按板块（10% / 20% / 30%）与四舍五入到分；
- 停牌标记；
- 分钟线时间规范化与成交量换算；
- 交易日历本地缓存、is_trade_day / next_trade_day；
- sync_daily / sync_minute 的幂等与「单 symbol 失败不中断」。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quant.data import calendar, fetch, models, store

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "data"


def _load(name: str) -> pd.DataFrame:
    return pd.read_csv(FIXTURES / name)


def _raw_daily() -> pd.DataFrame:
    return _load("ak_daily_raw_600519.csv")


def _qfq_daily() -> pd.DataFrame:
    return _load("ak_daily_qfq_600519.csv")


def _minute_raw() -> pd.DataFrame:
    return _load("ak_minute_5_600519.csv")


# ---------------------------------------------------------------------------
# 日线转换
# ---------------------------------------------------------------------------
def test_convert_daily_columns_and_dtypes():
    out = fetch.convert_daily(_qfq_daily(), _raw_daily(), symbol="600519", limit_fn=models.limit_prices)
    assert list(out.columns) == list(models.DAILY_COLUMNS)
    assert str(out["volume"].dtype) == "int64"
    assert str(out["is_suspended"].dtype) == "int64"
    assert str(out["close"].dtype) == "float64"
    assert out["symbol"].tolist() == ["600519"] * 4
    assert out["trade_date"].tolist() == ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]


def test_convert_daily_volume_hand_to_share():
    out = fetch.convert_daily(_qfq_daily(), _raw_daily(), symbol="600519", limit_fn=models.limit_prices)
    # AKShare 成交量单位是「手」，落库必须是「股」
    assert out["volume"].tolist() == [2345600, 3456700, 1234500, 0]
    assert out["amount"].tolist() == [3987520000.0, 5824539500.0, 2086613625.0, 0.0]


def test_convert_daily_pre_close_is_unadjusted():
    out = fetch.convert_daily(_qfq_daily(), _raw_daily(), symbol="600519", limit_fn=models.limit_prices)
    # pre_close = 不复权收盘 - 不复权涨跌额
    assert out["pre_close"].tolist() == [1687.5, 1700.0, 1685.0, 1690.25]
    assert out["close"].tolist() == [1700.0, 1685.0, 1690.25, 1680.0]
    assert out["pct_chg"].tolist() == [0.74, -0.88, 0.31, -0.61]


def test_convert_daily_limit_prices_board_10pct_and_half_up():
    out = fetch.convert_daily(_qfq_daily(), _raw_daily(), symbol="600519", limit_fn=models.limit_prices)
    # 主板 ±10%，四舍五入到分；1690.25×1.1=1859.275 → 1859.28，×0.9=1521.225 → 1521.23
    assert out["limit_up"].tolist() == [1856.25, 1870.0, 1853.5, 1859.28]
    assert out["limit_down"].tolist() == [1518.75, 1530.0, 1516.5, 1521.23]


def test_convert_daily_limit_prices_board_20pct():
    raw = pd.DataFrame(
        {
            "日期": ["2024-01-02"],
            "股票代码": ["300750"],
            "开盘": [180.0],
            "收盘": [183.0],
            "最高": [185.0],
            "最低": [179.0],
            "成交量": [100],
            "成交额": [1_830_000.0],
            "振幅": [3.0],
            "涨跌幅": [1.67],
            "涨跌额": [3.0],
            "换手率": [0.5],
        }
    )
    out = fetch.convert_daily(None, raw, symbol="300750", limit_fn=models.limit_prices)
    assert out["volume"].tolist() == [10000]
    assert out["pre_close"].tolist() == [180.0]
    assert out["limit_up"].tolist() == [216.0]  # 180 × 1.2
    assert out["limit_down"].tolist() == [144.0]  # 180 × 0.8


def test_convert_daily_qfq_merge_and_adj_factor():
    out = fetch.convert_daily(_qfq_daily(), _raw_daily(), symbol="600519", limit_fn=models.limit_prices)
    # adj_close 取 qfq 的「收盘」，close 保持不复权
    assert out["adj_close"].tolist() == [1530.0, 1516.5, 1521.23, 1680.0]
    assert out["adj_factor"].iloc[0] == pytest.approx(0.9)
    assert out["adj_factor"].iloc[1] == pytest.approx(0.9)
    assert out["adj_factor"].iloc[3] == pytest.approx(1.0)


def test_convert_daily_without_qfq_falls_back():
    out = fetch.convert_daily(None, _raw_daily(), symbol="600519")
    assert out["adj_close"].tolist() == out["close"].tolist()
    assert out["adj_factor"].tolist() == [1.0] * 4


def test_convert_daily_suspension_flag():
    out = fetch.convert_daily(_qfq_daily(), _raw_daily(), symbol="600519", limit_fn=models.limit_prices)
    # 成交量 0 停牌
    assert out["is_suspended"].tolist() == [0, 0, 0, 1]


def test_convert_daily_empty_input_returns_typed_empty_frame():
    out = fetch.convert_daily(pd.DataFrame(), pd.DataFrame(), symbol="600519")
    assert out.shape == (0, len(models.DAILY_COLUMNS))
    assert list(out.columns) == list(models.DAILY_COLUMNS)


# ---------------------------------------------------------------------------
# 分钟线转换
# ---------------------------------------------------------------------------
def test_convert_minute_normalizes_time_and_volume():
    out = fetch.convert_minute(_minute_raw(), "600519", 5)
    assert list(out.columns) == list(models.MINUTE_COLUMNS)
    assert out["ts"].tolist() == [
        "2024-01-02 09:35",
        "2024-01-02 09:40",
        "2024-01-02 09:45",
    ]
    assert out["volume"].tolist() == [120000, 80000, 150000]
    assert out["period"].tolist() == [5, 5, 5]
    assert out["symbol"].tolist() == ["600519"] * 3
    assert out["close"].tolist() == [1692.5, 1695.0, 1691.0]


def test_convert_minute_empty():
    out = fetch.convert_minute(pd.DataFrame(), "600519", 5)
    assert out.shape == (0, len(models.MINUTE_COLUMNS))


# ---------------------------------------------------------------------------
# normalize / 涨跌停规则
# ---------------------------------------------------------------------------
def test_normalize_daily_fills_defaults():
    raw = pd.DataFrame(
        {
            "symbol": ["600519"],
            "trade_date": ["2024-01-02"],
            "open": [10.0],
            "high": [10.2],
            "low": [9.9],
            "close": [10.1],
            "pre_close": [10.0],
            "volume": [100],
            "amount": [1000.0],
            "pct_chg": [1.0],
            "limit_up": [11.0],
            "limit_down": [9.0],
        }
    )
    out = models.normalize_daily(raw)
    assert out["turnover_rate"].tolist() == [0.0]
    assert out["adj_factor"].tolist() == [1.0]
    assert out["adj_close"].tolist() == [10.1]
    assert out["is_suspended"].tolist() == [0]


def test_limit_pct_by_board():
    assert models.limit_pct("600519") == 0.10
    assert models.limit_pct("000001") == 0.10
    assert models.limit_pct("002594") == 0.10
    assert models.limit_pct("300750") == 0.20
    assert models.limit_pct("688981") == 0.20
    assert models.limit_pct("830799") == 0.30
    assert models.limit_pct("430139") == 0.30
    assert models.limit_pct("600519", is_st=True) == 0.05


def test_round_half_up_to_cent():
    assert models.round_half_up(11.055, 2) == 11.06
    assert models.round_half_up(11.054, 2) == 11.05
    assert models.round_half_up(10.05 * 1.1, 2) == 11.06
    assert models.limit_prices(10.0, "600519") == (11.0, 9.0)
    assert models.limit_prices(10.0, "600519", is_st=True) == (10.5, 9.5)


# ---------------------------------------------------------------------------
# 交易日历（本地缓存，不联网）
# ---------------------------------------------------------------------------
CAL_DATES = [
    "2024-01-01",
    "2024-01-02",
    "2024-01-03",
    "2024-01-04",
    "2024-01-05",
    "2024-01-08",
    "2024-01-09",
    "2024-01-10",
]


def _calendar_cfg(tmp_path: Path, monkeypatch) -> dict:
    path = tmp_path / "trade_calendar.csv"
    pd.DataFrame({"trade_date": CAL_DATES}).to_csv(path, index=False)
    cfg = {"data": {"calendar_cache": str(path), "db": str(tmp_path / "quant.db")}}

    def _boom(*args, **kwargs):  # pragma: no cover - 命中即说明错误地联网
        raise AssertionError("日历缓存已覆盖请求区间，不应联网")

    monkeypatch.setattr(fetch, "fetch_calendar", _boom)
    return cfg


def test_trade_days_from_local_cache(tmp_path, monkeypatch):
    cfg = _calendar_cfg(tmp_path, monkeypatch)
    assert calendar.trade_days("2024-01-02", "2024-01-05", cfg) == [
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
    ]
    assert calendar.trade_days("2024-01-06", "2024-01-07", cfg) == []


def test_is_trade_day_and_next_trade_day(tmp_path, monkeypatch):
    cfg = _calendar_cfg(tmp_path, monkeypatch)
    assert calendar.is_trade_day("2024-01-03", cfg) is True
    assert calendar.is_trade_day("2024-01-06", cfg) is False  # 周六
    assert calendar.next_trade_day("2024-01-05", cfg) == "2024-01-08"
    assert calendar.next_trade_day("2024-01-05", cfg, inclusive=True) == "2024-01-05"
    assert calendar.next_trade_day("2024-01-10", cfg) is None  # 缓存末尾之后


def test_trade_days_refreshes_when_cache_stale(tmp_path, monkeypatch):
    cfg = _calendar_cfg(tmp_path, monkeypatch)
    extended = CAL_DATES + ["2024-01-11", "2024-01-12"]
    calls: list[bool] = []

    def fake_fetch_calendar(cfg=None):
        calls.append(True)
        return extended

    monkeypatch.setattr(fetch, "fetch_calendar", fake_fetch_calendar)
    assert calendar.trade_days("2024-01-10", "2024-01-12", cfg) == [
        "2024-01-10",
        "2024-01-11",
        "2024-01-12",
    ]
    assert calls, "缓存覆盖不足时应刷新一次"


def test_calendar_without_cache_and_offline_raises(tmp_path, monkeypatch):
    cfg = {"data": {"calendar_cache": str(tmp_path / "missing.csv"), "db": str(tmp_path / "q.db")}}

    def network_down(cfg=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(fetch, "fetch_calendar", network_down)
    with pytest.raises(calendar.CalendarError):
        calendar.trade_days("2024-01-02", "2024-01-03", cfg)


def test_refresh_unknown_range_with_empty_cache(tmp_path, monkeypatch):
    cfg = {"data": {"calendar_cache": str(tmp_path / "missing.csv"), "db": str(tmp_path / "q.db")}}
    monkeypatch.setattr(fetch, "fetch_calendar", lambda cfg=None: CAL_DATES)
    assert calendar.trade_days("2024-01-01", "2024-01-02", cfg) == CAL_DATES[:2]


# ---------------------------------------------------------------------------
# 采集编排：幂等写库、单 symbol 失败不中断
# ---------------------------------------------------------------------------
def _daily_frame() -> pd.DataFrame:
    return fetch.convert_daily(_qfq_daily(), _raw_daily(), symbol="600519", limit_fn=models.limit_prices)


def test_sync_daily_is_idempotent(tmp_path, monkeypatch):
    cfg = {"data": {"db": str(tmp_path / "quant.db")}}
    calls: list[tuple] = []

    def fake_fetch_daily(symbol, start, end, cfg=None):
        calls.append((symbol, start, end))
        return _daily_frame()

    monkeypatch.setattr(fetch, "fetch_daily", fake_fetch_daily)
    stats1 = fetch.sync_daily(["600519"], "2024-01-01", "2024-01-05", cfg)
    stats2 = fetch.sync_daily(["600519"], "2024-01-01", "2024-01-05", cfg)
    assert stats1 == {"600519": 4}
    assert stats2 == {"600519": 4}
    # 重复抓取不产生重复行
    assert len(store.read_daily("600519", tmp_path / "quant.db")) == 4
    assert calls[0] == ("600519", "2024-01-01", "2024-01-05")


def test_sync_daily_single_symbol_failure_does_not_abort(tmp_path, monkeypatch):
    cfg = {"data": {"db": str(tmp_path / "quant.db")}}

    def fake_fetch_daily(symbol, start, end, cfg=None):
        if symbol == "000001":
            raise fetch.DataFetchError("模拟东财限流")
        return _daily_frame()

    monkeypatch.setattr(fetch, "fetch_daily", fake_fetch_daily)
    stats = fetch.sync_daily(["600519", "000001"], "2024-01-01", "2024-01-05", cfg)
    assert stats == {"600519": 4, "000001": 0}


def test_sync_daily_all_failed_raises(tmp_path, monkeypatch):
    cfg = {"data": {"db": str(tmp_path / "quant.db")}}

    def always_fail(*args, **kwargs):
        raise fetch.DataFetchError("模拟限流")

    monkeypatch.setattr(fetch, "fetch_daily", always_fail)
    with pytest.raises(fetch.DataFetchError):
        fetch.sync_daily(["600519"], "2024-01-01", "2024-01-05", cfg)


def test_sync_minute_writes_rows(tmp_path, monkeypatch):
    cfg = {"data": {"db": str(tmp_path / "quant.db")}}
    monkeypatch.setattr(fetch, "fetch_minute_raw", lambda *a, **k: _minute_raw())
    stats = fetch.sync_minute(["600519"], 5, cfg)
    assert stats == {"600519": 3}
    df = store.read_minute("600519", 5, tmp_path / "quant.db")
    assert len(df) == 3
    assert df["volume"].tolist() == [120000, 80000, 150000]


def test_sync_minute_invalid_period_raises(tmp_path):
    with pytest.raises(ValueError):
        fetch.sync_minute(["600519"], 7, {"data": {"db": str(tmp_path / "q.db")}})


# ---------------------------------------------------------------------------
# 信号载荷转换（store._prepare_signal 的契约约束）
# ---------------------------------------------------------------------------
def test_prepare_signal_accepts_nested_prob_and_derives_fields():
    prepared = store._prepare_signal(
        {
            "symbol": "600519",
            "asof": "2026-11-21T14:35:00+08:00",
            "trade_date": "2026-11-21",
            "price": 1680.5,
            "direction": "up",
            "prob": {"up": 0.42, "flat": 0.31, "down": 0.27},
            "model_version": "v0.1.0",
            "top_features": [{"name": "ret_5d", "value": 0.031, "contrib": 0.08}],
        }
    )
    assert prepared["signal_id"] == "600519-2026-11-21-20261121T143500"
    assert prepared["confidence"] == 0.42
    assert prepared["source"] == "local"
    assert prepared["status"] == "ok"
    assert json.loads(prepared["top_features"])[0]["name"] == "ret_5d"
    assert prepared["created_at"].endswith("+08:00")


def test_prepare_signal_normalizes_probabilities():
    prepared = store._prepare_signal(
        {
            "symbol": "000001",
            "asof": "2026-11-21T14:35:00+08:00",
            "trade_date": "2026-11-21",
            "price": 12.0,
            "direction": "flat",
            "prob_up": 0.5,
            "prob_flat": 0.3,
            "prob_down": 0.1,
            "model_version": "v0.1.0",
        }
    )
    total = prepared["prob_up"] + prepared["prob_flat"] + prepared["prob_down"]
    assert total == pytest.approx(1.0)
    assert prepared["confidence"] == pytest.approx(0.5556)


# ---------------------------------------------------------------------------
# 备用数据源（东财 push2 被限流时自动降级到 AKShare 新浪接口）
# ---------------------------------------------------------------------------
def _sina_raw() -> pd.DataFrame:
    return _load("ak_sina_daily_raw_600519.csv")


def _sina_qfq() -> pd.DataFrame:
    return _load("ak_sina_daily_qfq_600519.csv")


def _sina_minute() -> pd.DataFrame:
    return _load("ak_sina_minute_5_600519.csv")


def test_sina_symbol_prefix_mapping():
    assert fetch._sina_symbol("600519") == "sh600519"
    assert fetch._sina_symbol("000001") == "sz000001"
    assert fetch._sina_symbol("300750") == "sz300750"
    assert fetch._sina_symbol("688981") == "sh688981"
    assert fetch._sina_symbol("830799") == "bj830799"
    assert fetch._sina_symbol("sh600519") == "sh600519"


def test_convert_daily_sina_core_fields():
    out = fetch.convert_daily_sina(
        _sina_qfq(),
        _sina_raw(),
        "600519",
        start="2024-01-02",
        end="2024-01-04",
        limit_fn=models.limit_prices,
    )
    assert list(out.columns) == list(models.DAILY_COLUMNS)
    # 新浪成交量单位已是股，不能再 ×100
    assert out["volume"].tolist() == [3215644, 2022929, 2155107]
    # pre_close 来自 lookback 段的前一交易日收盘
    assert out["pre_close"].tolist() == [1708.0, 1685.01, 1694.0]
    assert out["close"].tolist() == [1685.01, 1694.0, 1669.0]
    assert out["pct_chg"].iloc[0] == pytest.approx(-1.346, abs=1e-3)
    # 新浪 turnover 是比例，×100 转 %
    assert out["turnover_rate"].tolist() == [0.256, 0.161, 0.1716]
    assert out["adj_close"].tolist() == [1531.22, 1539.39, 1516.68]
    assert out["adj_factor"].iloc[0] == pytest.approx(1531.22 / 1685.01, abs=1e-6)
    assert out["limit_up"].tolist() == [1878.8, 1853.51, 1863.4]
    assert out["limit_down"].tolist() == [1537.2, 1516.51, 1524.6]


def test_convert_minute_sina_volume_already_shares():
    out = fetch.convert_minute_sina(_sina_minute(), "600519", 5)
    assert list(out.columns) == list(models.MINUTE_COLUMNS)
    assert out["ts"].tolist() == [
        "2026-11-20 09:35",
        "2026-11-20 09:40",
        "2026-11-20 09:45",
    ]
    assert out["volume"].tolist() == [120000, 80000, 150000]
    assert out["period"].tolist() == [5, 5, 5]


def test_fetch_daily_falls_back_to_sina(monkeypatch):
    def em_down(*args, **kwargs):
        raise fetch.DataFetchError("东财 push2 限流")

    def sina_raw(symbol, start, end, adjust="", cfg=None):
        return _sina_qfq() if adjust == "qfq" else _sina_raw()

    monkeypatch.setattr(fetch, "fetch_daily_raw", em_down)
    monkeypatch.setattr(fetch, "fetch_daily_sina_raw", sina_raw)
    frame = fetch.fetch_daily("600519", "2024-01-02", "2024-01-04")
    assert frame.attrs["source"] == "sina"
    assert frame["trade_date"].tolist() == ["2024-01-02", "2024-01-03", "2024-01-04"]


def test_fetch_daily_forced_em_does_not_fall_back(monkeypatch):
    def em_down(*args, **kwargs):
        raise fetch.DataFetchError("东财 push2 限流")

    monkeypatch.setattr(fetch, "fetch_daily_raw", em_down)
    with pytest.raises(fetch.DataFetchError, match="限流"):
        fetch.fetch_daily(
            "600519", "2024-01-02", "2024-01-04", cfg={"data": {"daily_source": "em"}}
        )


def test_fetch_daily_forced_sina_skips_em(monkeypatch):
    def em_should_not_be_called(*args, **kwargs):
        raise AssertionError("daily_source=sina 时不应调用东财接口")

    def sina_raw(symbol, start, end, adjust="", cfg=None):
        return _sina_qfq() if adjust == "qfq" else _sina_raw()

    monkeypatch.setattr(fetch, "fetch_daily_raw", em_should_not_be_called)
    monkeypatch.setattr(fetch, "fetch_daily_sina_raw", sina_raw)
    frame = fetch.fetch_daily(
        "600519", "2024-01-02", "2024-01-04", cfg={"data": {"daily_source": "sina"}}
    )
    assert frame.attrs["source"] == "sina"
    assert len(frame) == 3


def test_sync_daily_uses_fallback_when_em_blocked(tmp_path, monkeypatch):
    def em_down(*args, **kwargs):
        raise fetch.DataFetchError("东财 push2 限流")

    def sina_raw(symbol, start, end, adjust="", cfg=None):
        return _sina_qfq() if adjust == "qfq" else _sina_raw()

    monkeypatch.setattr(fetch, "fetch_daily_raw", em_down)
    monkeypatch.setattr(fetch, "fetch_daily_sina_raw", sina_raw)
    fetch.LAST_SOURCES.clear()
    cfg = {"data": {"db": str(tmp_path / "quant.db")}}
    stats = fetch.sync_daily(["600519"], "2024-01-02", "2024-01-04", cfg)
    assert stats == {"600519": 3}
    assert fetch.LAST_SOURCES["600519"] == "sina"
    assert len(store.read_daily("600519", tmp_path / "quant.db")) == 3


def test_fetch_minute_falls_back_to_sina(monkeypatch):
    def em_down(*args, **kwargs):
        raise fetch.DataFetchError("东财 push2 限流")

    monkeypatch.setattr(fetch, "fetch_minute_raw", em_down)
    monkeypatch.setattr(fetch, "fetch_minute_sina_raw", lambda *a, **k: _sina_minute())
    frame = fetch.fetch_minute(
        "600519", 5, "2026-11-20 09:30:00", "2026-11-20 15:00:00"
    )
    assert frame.attrs["source"] == "sina"
    assert len(frame) == 3
    cropped = fetch.fetch_minute(
        "600519", 5, "2026-11-20 09:40:00", "2026-11-20 15:00:00"
    )
    assert cropped["ts"].tolist() == ["2026-11-20 09:40", "2026-11-20 09:45"]


def test_sync_minute_uses_fallback_when_em_blocked(tmp_path, monkeypatch):
    def em_down(*args, **kwargs):
        raise fetch.DataFetchError("东财 push2 限流")

    monkeypatch.setattr(fetch, "fetch_minute_raw", em_down)
    monkeypatch.setattr(fetch, "fetch_minute_sina_raw", lambda *a, **k: _sina_minute())
    fetch.LAST_SOURCES.clear()
    cfg = {"data": {"db": str(tmp_path / "quant.db")}}
    stats = fetch.sync_minute(["600519"], 5, cfg)
    assert stats == {"600519": 3}
    assert fetch.LAST_SOURCES["600519"] == "sina"
    assert len(store.read_minute("600519", 5, tmp_path / "quant.db")) == 3
