"""存储层离线单测：Parquet 行情 + DuckDB 状态库、幂等 UPSERT、读取、信号读写（契约 §3、§9）。"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from quant.data import fetch, models, store

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "data"


def _daily_frame() -> pd.DataFrame:
    raw = pd.read_csv(FIXTURES / "ak_daily_raw_600519.csv")
    qfq = pd.read_csv(FIXTURES / "ak_daily_qfq_600519.csv")
    return fetch.convert_daily(qfq, raw, symbol="600519", limit_fn=models.limit_prices)


def _minute_frame() -> pd.DataFrame:
    raw = pd.read_csv(FIXTURES / "ak_minute_5_600519.csv")
    return fetch.convert_minute(raw, symbol="600519", period=5)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """DuckDB 状态库文件（行情 Parquet 由其位置推导到 ``tmp_path/parquet``）。"""
    path = tmp_path / "quant.duckdb"
    store.init_db(path)
    return path


def _parquet_daily_file(db: Path, symbol: str = "600519") -> Path:
    return store.parquet_root(db) / "daily_bars" / f"symbol={symbol}" / "daily_bars.parquet"


def _parquet_minute_file(db: Path, symbol: str = "600519", period: int = 5) -> Path:
    return (
        store.parquet_root(db)
        / "minute_bars"
        / f"symbol={symbol}"
        / f"period={period}"
        / "minute_bars.parquet"
    )


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------
def test_init_db_creates_state_tables_parquet_dirs_and_indexes(db: Path):
    # 状态库：DuckDB signals 表 + 索引
    assert "signals" in store.get_table_names(db)
    with store.connect(db) as con:
        indexes = {
            row["index_name"]
            for row in con.execute(
                "SELECT index_name FROM duckdb_indexes() WHERE table_name='signals'"
            ).fetch_df().to_dict("records")
        }
    assert {
        "idx_signals_symbol_asof",
        "idx_signals_trade_date",
        "idx_signals_status",
    } <= indexes
    # 行情目录（Parquet 根 + 两张逻辑行情表目录）
    root = store.parquet_root(db)
    assert (root / "daily_bars").is_dir()
    assert (root / "minute_bars").is_dir()


def test_init_db_is_idempotent(db: Path):
    store.init_db(db)
    store.init_db(db)
    assert "signals" in store.get_table_names(db)


def test_parquet_layout_and_duckdb_read_parquet(db: Path):
    """行情落到契约目录，且能用 DuckDB read_parquet 直接查询。"""
    store.upsert_daily(_daily_frame(), db)
    path = _parquet_daily_file(db)
    assert path.is_file(), "日线应写成 daily_bars/symbol=<code>/daily_bars.parquet"
    assert str(path).endswith("symbol=600519/daily_bars.parquet")

    con = duckdb.connect()
    try:
        # 目录名是 hive 风格的 symbol=600519；文件本身已带 symbol 列，显式关闭
        # hive_partitioning，避免 DuckDB 把目录名推断成整数分区列。
        rows = con.execute(
            "SELECT symbol, trade_date, close FROM read_parquet(?, hive_partitioning=false)"
            " ORDER BY trade_date",
            [str(path)],
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 4
    assert rows[0][0] == "600519" and rows[0][1] == "2024-01-02"

    store.upsert_minute(_minute_frame(), db)
    mpath = _parquet_minute_file(db)
    assert mpath.is_file(), "分钟线应写成 minute_bars/symbol=<code>/period=<p>/minute_bars.parquet"
    assert store.get_storage_tables(db) == ["daily_bars", "minute_bars", "signals"]


def test_parquet_upsert_keeps_other_dates_and_replaces_new_values(db: Path):
    """增量 upsert 只覆盖重叠主键，不丢旧行（保留新值）。"""
    frame = _daily_frame()
    store.upsert_daily(frame.iloc[:2], db)
    changed = frame.iloc[1:3].copy()
    changed.loc[:, "close"] = 1234.5
    store.upsert_daily(changed, db)
    out = store.read_daily("600519", db)
    assert len(out) == 3
    by_date = out.set_index("trade_date")["close"]
    assert by_date["2024-01-02"] == 1700.0  # 旧行未被改动
    assert by_date["2024-01-03"] == 1234.5  # 重叠行保留新值
    assert by_date["2024-01-04"] == 1234.5


# ---------------------------------------------------------------------------
# 日线写入/读取
# ---------------------------------------------------------------------------
def test_upsert_daily_roundtrip(db: Path):
    frame = _daily_frame()
    assert store.upsert_daily(frame, db) == 4
    out = store.read_daily("600519", db)
    assert len(out) == 4
    assert list(out.columns) == list(models.DAILY_COLUMNS)
    assert out["trade_date"].tolist() == ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    row = out.iloc[0]
    assert row["symbol"] == "600519"
    assert row["close"] == 1700.0
    assert row["pre_close"] == 1687.5
    assert row["volume"] == 2345600
    assert row["adj_close"] == 1530.0
    assert row["limit_up"] == 1856.25
    assert row["limit_down"] == 1518.75
    assert row["is_suspended"] == 0
    assert out.iloc[3]["is_suspended"] == 1


def test_upsert_daily_is_idempotent(db: Path):
    frame = _daily_frame()
    store.upsert_daily(frame, db)
    store.upsert_daily(frame, db)
    store.upsert_daily(frame, db)
    assert len(store.read_daily("600519", db)) == 4


def test_upsert_daily_updates_existing_row(db: Path):
    frame = _daily_frame()
    store.upsert_daily(frame, db)
    changed = frame.copy()
    changed.loc[0, "close"] = 999.0
    changed.loc[0, "adj_close"] = 899.1
    store.upsert_daily(changed, db)
    out = store.read_daily("600519", db)
    assert len(out) == 4
    assert out.iloc[0]["close"] == 999.0
    assert out.iloc[0]["adj_close"] == pytest.approx(899.1)


def test_read_daily_date_range(db: Path):
    store.upsert_daily(_daily_frame(), db)
    out = store.read_daily("600519", db, start="2024-01-03", end="2024-01-04")
    assert out["trade_date"].tolist() == ["2024-01-03", "2024-01-04"]
    assert store.read_daily("600519", db, start="2025-01-01").empty


def test_read_daily_missing_symbol_returns_typed_empty(db: Path):
    out = store.read_daily("000001", db)
    assert out.empty
    assert list(out.columns) == list(models.DAILY_COLUMNS)


def test_read_daily_multi(db: Path):
    a = _daily_frame()
    b = a.copy()
    b["symbol"] = "000001"
    b["close"] = b["close"] + 10
    store.upsert_daily(pd.concat([a, b], ignore_index=True), db)
    out = store.read_daily_multi(["600519", "000001"], db)
    assert len(out) == 8
    assert out["symbol"].tolist()[:4] == ["000001"] * 4  # 按 symbol 升序
    assert out["symbol"].tolist()[4:] == ["600519"] * 4
    filtered = store.read_daily_multi(["000001"], db, start="2024-01-02", end="2024-01-02")
    assert filtered["symbol"].tolist() == ["000001"]
    assert store.read_daily_multi([], db).empty


# ---------------------------------------------------------------------------
# 分钟线写入/读取
# ---------------------------------------------------------------------------
def test_minute_roundtrip_and_idempotency(db: Path):
    frame = _minute_frame()
    assert store.upsert_minute(frame, db) == 3
    assert store.upsert_minute(frame, db) == 3
    out = store.read_minute("600519", 5, db)
    assert len(out) == 3
    assert list(out.columns) == list(models.MINUTE_COLUMNS)
    assert out["ts"].tolist() == [
        "2024-01-02 09:35",
        "2024-01-02 09:40",
        "2024-01-02 09:45",
    ]
    assert out["volume"].tolist() == [120000, 80000, 150000]
    assert out["period"].tolist() == [5, 5, 5]


def test_read_minute_time_range_and_period_filter(db: Path):
    store.upsert_minute(_minute_frame(), db)
    out = store.read_minute("600519", 5, db, start="2024-01-02 09:35", end="2024-01-02 09:40")
    assert len(out) == 2
    assert store.read_minute("600519", 15, db).empty


# ---------------------------------------------------------------------------
# 信号
# ---------------------------------------------------------------------------
def _signal(symbol: str = "600519", asof: str = "2026-11-21T14:35:00+08:00", direction: str = "up", **kwargs):
    sig = {
        "symbol": symbol,
        "asof": asof,
        "trade_date": asof[:10],
        "price": 1680.5,
        "direction": direction,
        "prob": {"up": 0.42, "flat": 0.31, "down": 0.27},
        "model_version": "v0.1.0",
        "top_features": [{"name": "ret_5d", "value": 0.031, "contrib": 0.08}],
    }
    sig.update(kwargs)
    return sig


def test_write_and_read_signal_roundtrip(db: Path):
    sig = _signal()
    store.write_signal(sig, db)
    out = store.read_signals(db)
    assert len(out) == 1
    got = out[0]
    assert got["signal_id"] == "600519-2026-11-21-20261121T143500"
    assert got["direction"] == "up"
    assert got["confidence"] == 0.42
    assert got["prob_up"] + got["prob_flat"] + got["prob_down"] == pytest.approx(1.0)
    assert got["top_features"][0]["name"] == "ret_5d"
    assert got["status"] == "ok"
    assert got["source"] == "local"
    assert got["horizon"] == "1d"
    assert got["expires_at"].endswith("+08:00")


def test_write_signal_idempotent_by_signal_id(db: Path):
    store.write_signal(_signal(), db)
    store.write_signal(_signal(confidence=0.42, top_features=[]), db)
    out = store.read_signals(db)
    assert len(out) == 1
    assert out[0]["top_features"] == []


def test_read_signals_limit_and_order(db: Path):
    store.write_signal(_signal(asof="2026-11-20T14:35:00+08:00"), db)
    store.write_signal(_signal(asof="2026-11-21T14:35:00+08:00"), db)
    store.write_signal(_signal(symbol="000001", asof="2026-11-21T10:00:00+08:00"), db)
    out = store.read_signals(db, limit=2)
    assert len(out) == 2
    assert [s["asof"] for s in out] == [
        "2026-11-21T14:35:00+08:00",
        "2026-11-21T10:00:00+08:00",
    ]
    assert store.read_signals(db, limit=0) == []


def test_read_latest_signals_per_symbol(db: Path):
    store.write_signal(_signal(asof="2026-11-20T14:35:00+08:00"), db)
    store.write_signal(_signal(asof="2026-11-21T14:35:00+08:00"), db)
    store.write_signal(
        _signal(symbol="000001", asof="2026-11-21T10:00:00+08:00", direction="flat"), db
    )
    latest = store.read_latest_signals(db, ["600519", "000001", "300750"])
    assert len(latest) == 2
    by_symbol = {s["symbol"]: s for s in latest}
    assert set(by_symbol) == {"600519", "000001"}
    assert by_symbol["600519"]["asof"] == "2026-11-21T14:35:00+08:00"
    assert by_symbol["000001"]["asof"] == "2026-11-21T10:00:00+08:00"
    assert store.read_latest_signals(db, []) == []


def test_write_signal_validates_direction(db: Path):
    with pytest.raises(ValueError, match="direction"):
        store.write_signal(_signal(direction="sideways"), db)


def test_write_signal_validates_model_version(db: Path):
    sig = _signal()
    sig.pop("model_version")
    with pytest.raises(ValueError, match="model_version"):
        store.write_signal(sig, db)


def test_write_signal_truncates_top_features(db: Path):
    features = [{"name": f"f{i}", "value": i, "contrib": 0.1} for i in range(8)]
    store.write_signal(_signal(top_features=features), db)
    got = store.read_signals(db)[0]
    assert len(got["top_features"]) == 5


def test_write_signal_accepts_json_string_features(db: Path):
    store.write_signal(_signal(top_features=json.dumps([{"name": "ret_1d"}])), db)
    got = store.read_signals(db)[0]
    assert got["top_features"] == [{"name": "ret_1d"}]


def test_read_signals_empty_db(db: Path):
    assert store.read_signals(db) == []
    assert store.read_latest_signals(db, ["600519"]) == []


def test_write_signal_confidence_follows_max_prob(db: Path):
    store.write_signal(_signal(confidence=0.99), db)
    got = store.read_signals(db)[0]
    assert got["confidence"] == 0.42  # 契约：confidence = max(prob)，保留 4 位


# ---------------------------------------------------------------------------
# DuckDB 连接层：只读语义 / 锁冲突重试（data/db.py）
# ---------------------------------------------------------------------------
def test_readonly_connect_rejects_missing_file(tmp_path: Path):
    from quant.data import db as dbmod

    with pytest.raises(FileNotFoundError):
        with dbmod.connect(tmp_path / "missing.duckdb", read_only=True):
            pass


def test_connect_retries_on_lock_conflict(tmp_path: Path, monkeypatch):
    from quant.data import db as dbmod

    path = tmp_path / "quant.duckdb"
    calls = {"n": 0}
    real_open = dbmod._open

    def flaky_open(p, read_only):
        calls["n"] += 1
        if calls["n"] < 3:  # 前两次模拟「其它进程持锁」
            raise duckdb.IOException(
                f'Could not set lock on file "{p}": Conflicting lock is held'
            )
        return real_open(p, read_only)

    monkeypatch.setattr(dbmod, "_open", flaky_open)
    with dbmod.connect(path, attempts=3) as con:
        assert con.execute("SELECT 1").fetchall() == [(1,)]
    assert calls["n"] == 3
