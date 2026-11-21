"""回测引擎测试（契约 §12）。

覆盖：
- 手算验证 accuracy / 分方向 P·R·F1 / logloss / Brier / 混淆矩阵 / 校准分桶；
- 手算验证胜率（交易口径）与 Wilson 95% 置信区间、payoff、回撤、Sharpe；
- T+1 成交时点、停牌顺延、涨跌停拒单的成交模拟；
- walk-forward 自然季度切分；
- 端到端 run_backtest：§12 字段齐全、报告文件、model.json metrics 同步。
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import quant.backtest.engine as engine
from quant.backtest.engine import (
    REQUIRED_CONTRACT_PATHS,
    build_walk_forward_windows,
    compute_direction_metrics,
    compute_trade_metrics,
    run_backtest,
    simulate_trades,
    wilson_interval,
)
from quant.model.train import train

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "model"


def _load_synth():
    spec = importlib.util.spec_from_file_location(
        "model_synth_fixture_backtest", FIXTURE_DIR / "synth.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


SYNTH = _load_synth()


# ---------------------------------------------------------------------------
# 手算：Wilson
# ---------------------------------------------------------------------------
def test_wilson_interval_known_values() -> None:
    lo, hi = wilson_interval(50, 100)
    assert lo == pytest.approx(0.4038315304, abs=1e-9)
    assert hi == pytest.approx(0.5961684696, abs=1e-9)
    lo, hi = wilson_interval(0, 10)
    assert lo == pytest.approx(0.0, abs=1e-12)
    assert hi == pytest.approx(0.2775327999, abs=1e-9)
    lo, hi = wilson_interval(10, 10)
    assert hi == pytest.approx(1.0, abs=1e-12)
    assert lo == pytest.approx(0.7224672001, abs=1e-9)
    lo, hi = wilson_interval(2, 4)
    assert lo == pytest.approx(0.1500389892, abs=1e-9)
    assert hi == pytest.approx(0.8499610108, abs=1e-9)
    assert wilson_interval(0, 0) == (None, None)


# ---------------------------------------------------------------------------
# 手算：方向指标
# ---------------------------------------------------------------------------
def test_direction_metrics_hand_computed() -> None:
    labels = ["down", "flat", "up"]
    y_true = np.array(["up", "up", "down", "flat", "up", "down"])
    y_pred = np.array(["up", "flat", "down", "flat", "up", "up"])
    probs = np.array(
        [
            [0.1, 0.2, 0.7],
            [0.2, 0.5, 0.3],
            [0.6, 0.2, 0.2],
            [0.1, 0.8, 0.1],
            [0.1, 0.1, 0.8],
            [0.3, 0.2, 0.5],
        ]
    )
    dates = np.array(
        ["2025-01-02", "2025-01-03", "2025-04-01", "2025-04-02", "2025-07-01", "2025-07-02"]
    )
    m = compute_direction_metrics(y_true, y_pred, probs, dates, labels)
    # 命中：up/up, down/down, flat/flat, up/up → 4/6
    assert m["accuracy"] == pytest.approx(4 / 6, abs=1e-6)
    assert m["n"] == 6
    # 手算 logloss = -mean(ln(0.7,0.3,0.6,0.8,0.8,0.3))
    assert m["logloss"] == pytest.approx(0.6202888798, abs=1e-6)
    # 手算 Brier = (0.14+0.78+0.24+0.06+0.06+0.78)/6
    assert m["brier"] == pytest.approx(0.3433333333, abs=1e-6)
    # 分方向
    assert m["per_class"]["up"]["precision"] == pytest.approx(2 / 3, abs=1e-6)
    assert m["per_class"]["up"]["recall"] == pytest.approx(2 / 3, abs=1e-6)
    assert m["per_class"]["down"]["precision"] == pytest.approx(1.0, abs=1e-6)
    assert m["per_class"]["down"]["recall"] == pytest.approx(1 / 2, abs=1e-6)
    assert m["per_class"]["down"]["f1"] == pytest.approx(2 / 3, abs=1e-6)
    assert m["per_class"]["flat"]["precision"] == pytest.approx(1 / 2, abs=1e-6)
    assert m["per_class"]["flat"]["recall"] == pytest.approx(1.0, abs=1e-6)
    assert m["per_class"]["up"]["support"] == 3
    # 混淆矩阵：行=真实，列=预测（down/flat/up）
    assert m["confusion"]["matrix"] == [[1, 0, 1], [0, 1, 0], [0, 1, 2]]
    # 校准分桶：<=0.4:0 笔；0.4-0.5: s1/s5（均未命中）；0.5-0.6: s2 命中；>0.6: s0/s3/s4 全命中
    buckets = {b["bucket"]: b for b in m["calibration"]}
    assert buckets["le_0.40"]["n"] == 0
    assert buckets["0.40-0.50"]["n"] == 2
    assert buckets["0.40-0.50"]["hit_rate"] == pytest.approx(0.0, abs=1e-6)
    assert buckets["0.50-0.60"]["n"] == 1
    assert buckets["0.50-0.60"]["hit_rate"] == pytest.approx(1.0, abs=1e-6)
    assert buckets["gt_0.60"]["n"] == 3
    assert buckets["gt_0.60"]["hit_rate"] == pytest.approx(1.0, abs=1e-6)
    # 分年度 / 分季度
    by_year = {row["period"]: row for row in m["by_year"]}
    assert by_year["2025"]["n"] == 6 and by_year["2025"]["accuracy"] == pytest.approx(4 / 6, abs=1e-6)
    by_quarter = {row["period"]: row for row in m["by_quarter"]}
    assert set(by_quarter) == {"2025Q1", "2025Q2", "2025Q3"}
    assert by_quarter["2025Q1"]["n"] == 2 and by_quarter["2025Q1"]["accuracy"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 手算：胜率口径
# ---------------------------------------------------------------------------
def _hand_sim() -> dict:
    returns = [0.02, -0.01, 0.0, 0.03]
    sides = ["long", "long", "short", "short"]
    trades = [
        {
            "symbol": "600000",
            "side": side,
            "direction": "up" if side == "long" else "down",
            "signal_date": f"2025-01-0{i + 1}",
            "entry_date": f"2025-01-0{i + 2}",
            "exit_date": f"2025-01-0{i + 3}",
            "return_net": r,
            "pnl": r * 1000.0,
            "fee_entry": 0.0,
            "fee_exit": 0.0,
        }
        for i, (r, side) in enumerate(zip(returns, sides))
    ]
    equity = [100000.0, 102000.0, 101000.0, 103000.0, 106000.0]
    return {
        "trades": trades,
        "equity_curve": {
            "dates": ["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10"],
            "equity": equity,
            "normalized": [v / 100000.0 for v in equity],
        },
        "skip_reasons": {"flat_signal": 3},
        "initial_cash": 100000.0,
    }


def test_trade_metrics_hand_computed() -> None:
    m = compute_trade_metrics(_hand_sim(), 100000.0)
    assert m["trades"] == {"total": 4, "win": 2, "loss": 1, "flat": 1}
    assert m["win_rate"] == pytest.approx(0.5, abs=1e-12)
    assert m["wilson95"][0] == pytest.approx(0.1500389892, abs=1e-6)
    assert m["wilson95"][1] == pytest.approx(0.8499610108, abs=1e-6)
    assert m["sample_sufficient"] is False
    assert "样本不足" in m["note"]
    assert m["avg_return_per_trade"] == pytest.approx(0.01, abs=1e-6)
    assert m["payoff_ratio"] == pytest.approx(2.5, abs=1e-6)  # 0.025 / 0.01
    assert m["total_return"] == pytest.approx(0.06, abs=1e-6)
    assert m["max_drawdown"] == pytest.approx(-0.009803921568627416, abs=1e-6)
    # sharpe 手算：日收益 [2%, -0.9804%, 1.9802%, 2.9126%]，年化 sqrt(252)
    daily = [0.02, -0.009803921568627416, 0.019801980198019823, 0.029126213592233004]
    sharpe = float(np.mean(daily) / np.std(daily, ddof=1) * math.sqrt(252))
    assert m["sharpe"] == pytest.approx(round(sharpe, 6), abs=1e-6)
    # 分方向：long 2 笔 1 胜，short 2 笔 1 胜
    assert m["win_rate_by_direction"]["long"]["n"] == 2
    assert m["win_rate_by_direction"]["long"]["win_rate"] == pytest.approx(0.5, abs=1e-6)
    assert m["win_rate_by_direction"]["short"]["n"] == 2
    assert m["win_rate_by_direction"]["short"]["win_rate"] == pytest.approx(0.5, abs=1e-6)


def test_trade_metrics_empty() -> None:
    sim = {
        "trades": [],
        "equity_curve": {"dates": ["2025-01-06"], "equity": [100000.0], "normalized": [1.0]},
        "skip_reasons": {"flat_signal": 10},
    }
    m = compute_trade_metrics(sim, 100000.0)
    assert m["win_rate"] is None
    assert m["wilson95"] == [None, None]
    assert m["trades"]["total"] == 0
    assert "无成交" in m["note"]
    assert m["total_return"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 成交模拟：T+1 / 停牌 / 涨跌停
# ---------------------------------------------------------------------------
def _round_half_up(x: float) -> float:
    return math.floor(x * 100 + 0.5) / 100


def _price_frame(
    opens: list[float],
    closes: list[float],
    suspended: list[int] | None = None,
    limit_up_on: int | None = None,
) -> pd.DataFrame:
    n = len(opens)
    suspended = suspended or [0] * n
    rows = []
    prev_close = None
    for i in range(n):
        c = float(closes[i])
        pre = float(prev_close) if prev_close is not None else float(opens[i])
        up = _round_half_up(pre * 1.1)
        down = _round_half_up(pre * 0.9)
        o = float(opens[i])
        if limit_up_on is not None and i == limit_up_on:
            o = up
            c = up
        rows.append(
            {
                "symbol": "600000",
                "trade_date": f"2024-01-{i + 2:02d}",
                "open": o,
                "high": max(o, c),
                "low": min(o, c),
                "close": c,
                "pre_close": pre,
                "volume": 1_000_000,
                "amount": c * 1_000_000,
                "pct_chg": (c / pre - 1) * 100,
                "turnover_rate": 0.1,
                "adj_factor": 1.0,
                "adj_close": c,
                "limit_up": up,
                "limit_down": down,
                "is_suspended": suspended[i],
            }
        )
        prev_close = c
    return pd.DataFrame(rows)


CFG_ZERO_FEE = {
    "paper": {
        "initial_cash": 1_000_000,
        "commission_rate": 0.0,
        "commission_min": 0.0,
        "stamp_tax_sell": 0.0,
        "transfer_fee": 0.0,
        "slippage_bps": 0.0,
        "lot_size": 100,
    }
}


@pytest.fixture()
def zero_fees(monkeypatch):
    monkeypatch.setattr(
        engine,
        "_fees",
        lambda side, amount, cfg: {
            "commission": 0.0,
            "stamp_tax": 0.0,
            "transfer_fee": 0.0,
            "total": 0.0,
        },
    )


def test_simulate_trades_t_plus_one_long_short(zero_fees) -> None:
    daily = _price_frame(
        opens=[10.0, 10.2, 10.4, 10.3, 10.2, 10.1],
        closes=[10.1, 10.3, 10.5, 10.2, 10.1, 10.0],
    )
    signals = pd.DataFrame(
        {
            "symbol": ["600000", "600000", "600000"],
            "trade_date": ["2024-01-02", "2024-01-03", "2024-01-05"],
            "direction": ["up", "flat", "down"],
        }
    )
    sim = simulate_trades(daily, signals, CFG_ZERO_FEE, ["600000"], horizon_days=1)
    trades = sim["trades"]
    assert len(trades) == 2
    long_trade, short_trade = trades
    # T 日收盘出信号 → T+1 开盘成交（绝不当日成交）
    assert long_trade["signal_date"] == "2024-01-02"
    assert long_trade["entry_date"] == "2024-01-03"
    assert long_trade["exit_date"] == "2024-01-04"
    assert long_trade["side"] == "long"
    assert long_trade["entry_price"] == pytest.approx(10.2)
    assert long_trade["exit_price"] == pytest.approx(10.4)
    assert long_trade["qty"] == 98000  # floor(1e6/10.2/100)*100
    assert long_trade["return_net"] == pytest.approx(0.2 / 10.2, abs=1e-8)
    assert short_trade["signal_date"] == "2024-01-05"
    assert short_trade["entry_date"] == "2024-01-06"  # 使用 frame 内下一行
    assert short_trade["exit_date"] == "2024-01-07"
    assert short_trade["side"] == "short"
    assert short_trade["return_net"] == pytest.approx(0.1 / 10.2, abs=1e-8)
    # 净值 = 初始 + 两笔 pnl
    assert sim["equity_curve"]["equity"][-1] == pytest.approx(
        1_000_000 + (0.2 * 98000) + (0.1 * 98000), abs=0.01
    )
    m = compute_trade_metrics(sim, 1_000_000)
    assert m["win_rate"] == pytest.approx(1.0)
    assert m["trades"]["total"] == 2


def test_simulate_trades_flat_not_traded(zero_fees) -> None:
    daily = _price_frame(
        opens=[10.0, 10.1, 10.2, 10.3],
        closes=[10.05, 10.15, 10.25, 10.35],
    )
    signals = pd.DataFrame(
        {"symbol": ["600000"], "trade_date": ["2024-01-02"], "direction": ["flat"]}
    )
    sim = simulate_trades(daily, signals, CFG_ZERO_FEE, ["600000"], horizon_days=1)
    assert sim["trades"] == []
    assert sim["skip_reasons"]["flat_signal"] == 1


def test_simulate_trades_suspended_rolls_forward(zero_fees) -> None:
    daily = _price_frame(
        opens=[10.0, 10.0, 10.2, 10.4, 10.5, 10.6],
        closes=[10.1, 10.0, 10.3, 10.3, 10.4, 10.5],
        suspended=[0, 1, 0, 0, 0, 0],
    )
    signals = pd.DataFrame(
        {"symbol": ["600000"], "trade_date": ["2024-01-02"], "direction": ["up"]}
    )
    sim = simulate_trades(daily, signals, CFG_ZERO_FEE, ["600000"], horizon_days=1)
    assert len(sim["trades"]) == 1
    trade = sim["trades"][0]
    # 次日停牌 → 顺延到再下一个交易日开盘成交
    assert trade["entry_date"] == "2024-01-04"
    assert trade["exit_date"] == "2024-01-05"
    assert any("停牌顺延" in w for w in sim["warnings"])


def test_simulate_trades_limit_up_rejects_entry(zero_fees) -> None:
    daily = _price_frame(
        opens=[10.0, 10.2, 10.4, 10.5, 10.6],
        closes=[10.1, 10.3, 10.5, 10.6, 10.7],
        limit_up_on=1,  # T+1 开盘一字涨停 → 买不进，订单 rejected
    )
    signals = pd.DataFrame(
        {"symbol": ["600000"], "trade_date": ["2024-01-02"], "direction": ["up"]}
    )
    sim = simulate_trades(daily, signals, CFG_ZERO_FEE, ["600000"], horizon_days=1)
    assert sim["trades"] == []
    assert sim["skip_reasons"]["entry_limit_reject"] == 1


def test_simulate_trades_entry_beyond_eval_skipped(zero_fees) -> None:
    daily = _price_frame(
        opens=[10.0, 10.1, 10.2, 10.3, 10.4],
        closes=[10.05, 10.15, 10.25, 10.35, 10.45],
    )
    signals = pd.DataFrame(
        {
            "symbol": ["600000", "600000"],
            "trade_date": ["2024-01-02", "2024-01-05"],
            "direction": ["up", "up"],
        }
    )
    sim = simulate_trades(
        daily, signals, CFG_ZERO_FEE, ["600000"], horizon_days=1,
        eval_start="2024-01-02", eval_end="2024-01-05",
    )
    # 第二个信号（最后一天）无法在评估窗口内完成 T+1 开仓+平仓
    assert len(sim["trades"]) == 1
    assert sim["skip_reasons"].get("entry_beyond_eval", 0) + sim["skip_reasons"].get(
        "exit_beyond_eval_or_blocked", 0
    ) >= 1


def test_simulate_trades_uses_forward_adjusted_returns(zero_fees) -> None:
    """分红除权日：不复权价 -2.91%，前复权口径约 +0.09%，交易收益不应被除权跳空污染。"""
    daily = SYNTH.make_dividend_frame()
    symbol = str(daily["symbol"].iloc[0])
    dates = daily["trade_date"].tolist()
    signals = pd.DataFrame(
        {
            "symbol": [symbol, symbol],
            "trade_date": [dates[2], dates[6]],
            "direction": ["up", "flat"],
        }
    )
    sim = simulate_trades(daily, signals, CFG_ZERO_FEE, [symbol], horizon_days=1)
    assert len(sim["trades"]) == 1
    trade = sim["trades"][0]
    # 2024-06-06 开盘 10.3（af=0.97）买入，2024-06-07 开盘 10.00（af=1.0）卖出
    assert trade["entry_date"] == dates[3]
    assert trade["exit_date"] == dates[4]
    expected_adj = (10.0 * 1.0) / (10.3 * 0.97) - 1.0
    assert trade["return_net"] == pytest.approx(expected_adj, abs=1e-6)
    assert trade["return_net"] > -0.005, "前复权口径下除权日不应出现 -2.9% 假亏损"
    raw_return = 10.0 / 10.3 - 1.0
    assert trade["return_net"] != pytest.approx(raw_return, abs=1e-4)


# ---------------------------------------------------------------------------
# walk-forward 切分
# ---------------------------------------------------------------------------
def test_walk_forward_windows_quarterly() -> None:
    windows = build_walk_forward_windows("2025-01-15", "2026-11-20", 4)
    assert [w["start"] for w in windows] == [
        "2026-01-01",
        "2026-04-01",
        "2026-07-01",
        "2026-10-01",
    ]
    assert windows[-1]["end"] == "2026-11-20"
    for w in windows:
        assert w["start"] <= w["end"]
    all_windows = build_walk_forward_windows("2025-01-15", "2026-11-20", 0)
    assert len(all_windows) == 8
    # 相邻测试窗不重叠且按时间递增
    for prev, nxt in zip(all_windows, all_windows[1:]):
        assert prev["end"] < nxt["start"]


# ---------------------------------------------------------------------------
# 端到端：run_backtest
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def backtest_artifacts(tmp_path_factory):
    base = tmp_path_factory.mktemp("backtest_e2e")
    db = SYNTH.write_daily_db(SYNTH.load_fixture_csv(), base / "quant_synth.db")
    cfg = {
        "symbols": ["600519", "000001", "300750"],
        "data": {
            "db": str(db),
            "daily_start": "2022-01-01",
            "calendar_cache": str(base / "pending_calendar.csv"),
        },
        "model": {
            "horizon_days": 1,
            "label_threshold": 0.005,
            "train_years": 2,
            "walk_forward_splits": 4,
            "random_seed": 42,
            "version": "v-backtest-test",
            "out": str(base / "model.json"),
        },
        "backtest": {
            "start": "2025-06-01",
            "end": "2026-11-20",
            "report_dir": str(base / "reports"),
        },
        "paper": {
            "initial_cash": 1000000,
            "commission_rate": 0.00025,
            "commission_min": 5,
            "stamp_tax_sell": 0.0005,
            "transfer_fee": 0.00001,
            "slippage_bps": 5,
            "lot_size": 100,
        },
    }
    train_metrics = train(cfg["symbols"], cfg)
    result = run_backtest("2025-06-01", "2026-11-20", cfg)
    return {"cfg": cfg, "base": base, "result": result, "train_metrics": train_metrics}


def _dig(node, path: str):
    for key in path.split("."):
        node = node[key]
    return node


def test_backtest_contract_fields_present(backtest_artifacts) -> None:
    result = backtest_artifacts["result"]
    for path in REQUIRED_CONTRACT_PATHS:
        _dig(result, path)  # 缺字段直接 KeyError
    json_path = Path(result["meta"]["report_json"])
    md_path = Path(result["meta"]["report_md"])
    assert json_path.exists() and md_path.exists()
    on_disk = json.loads(json_path.read_text(encoding="utf-8"))
    for path in REQUIRED_CONTRACT_PATHS:
        _dig(on_disk, path)
    assert on_disk["win_rate"]["equity_curve"]["equity"]
    assert on_disk["win_rate"]["trades_detail"]
    md = md_path.read_text(encoding="utf-8")
    for heading in (
        "方向判断指标",
        "胜率（交易口径",
        "基准对比",
        "Walk-forward 切分",
        "校准",
        "混淆矩阵",
    ):
        assert heading in md
    for benchmark in ("always_flat", "majority", "ma5_cross", "random"):
        assert benchmark in md


def test_backtest_metric_sanity(backtest_artifacts) -> None:
    result = backtest_artifacts["result"]
    direction = result["direction"]
    win = result["win_rate"]
    assert 0.0 <= direction["accuracy"] <= 1.0
    assert direction["n"] > 0
    assert direction["coverage"] is not None and 0.0 < direction["coverage"] <= 1.0
    assert direction["logloss"] > 0 and direction["brier"] > 0
    # 混淆矩阵行和 = 各真实类样本数，总和 = n
    matrix = direction["confusion"]["matrix"]
    assert len(matrix) == 3 and all(len(row) == 3 for row in matrix)
    assert sum(sum(row) for row in matrix) == direction["n"]
    # 校准分桶 n 之和 = n
    assert sum(b["n"] for b in direction["calibration"]) == direction["n"]
    # 胜率在 Wilson CI 内
    if win["win_rate"] is not None:
        assert win["wilson95"][0] <= win["win_rate"] <= win["wilson95"][1]
        assert win["trades"]["total"] == (
            win["trades"]["win"] + win["trades"]["loss"] + win["trades"]["flat"]
        )
        assert win["win_rate"] == pytest.approx(
            win["trades"]["win"] / win["trades"]["total"], abs=1e-6
        )
    eq = win["equity_curve"]
    assert len(eq["dates"]) == len(eq["equity"]) == len(eq["normalized"])
    assert eq["equity"][0] == pytest.approx(1_000_000, abs=0.01)
    # 基准 4 项齐全且各自有准确率
    for name in ("always_flat", "majority", "ma5_cross", "random"):
        assert name in result["benchmarks"]
        assert 0.0 <= result["benchmarks"][name]["accuracy"] <= 1.0
    assert len(result["benchmarks"]["comparison"]) == 4
    # walk-forward 切分：训练窗严格早于测试窗
    splits = result["walk_forward"]["splits"]
    assert len(splits) >= 1
    for split in splits:
        assert split["train_range"][1] < split["test_range"][0]
        assert split["n_train"] >= 300
        assert split["test_range"][0] <= split["test_range"][1]
    # 返回摘要不含完整 trades_detail（大字段只落 json 文件）
    assert "trades_detail" not in result["win_rate"]
    assert result["win_rate"]["trades_detail_count"] >= 0


def test_backtest_syncs_model_metrics(backtest_artifacts) -> None:
    cfg = backtest_artifacts["cfg"]
    model = json.loads(Path(cfg["model"]["out"]).read_text(encoding="utf-8"))
    metrics = model["metrics"]
    assert metrics["basis"] == "walk_forward_oos"
    assert metrics["accuracy"] == pytest.approx(
        backtest_artifacts["result"]["direction"]["accuracy"], abs=1e-9
    )
    assert metrics["n"] == backtest_artifacts["result"]["direction"]["n"]
    assert "win_rate" in metrics and "backtest_range" in metrics
    assert metrics["random_seed"] == 42


def test_backtest_missing_db_raises(tmp_path: Path) -> None:
    cfg = {
        "symbols": ["600519"],
        "data": {
            "db": str(tmp_path / "no_such.db"),
            "daily_start": "2022-01-01",
            "calendar_cache": str(tmp_path / "none.csv"),
        },
        "model": {"horizon_days": 1, "label_threshold": 0.005, "random_seed": 42},
        "backtest": {"start": "2025-01-01", "end": "2025-12-31", "report_dir": str(tmp_path)},
        "paper": {"initial_cash": 1000000},
    }
    with pytest.raises(RuntimeError):
        run_backtest("2025-01-01", "2025-12-31", cfg)
