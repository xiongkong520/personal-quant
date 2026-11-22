"""市场状态引擎测试（契约 §15 / task-7）。

全部离线：合成指数日线 / 涨跌家数 / 板块行情，不联网。
覆盖：
- 指数趋势指标（均线排列、5/20 日动量、成交额相对 20 日均值）；
- 大盘状态机（进攻 / 偏多 / 震荡 / 偏空 / 冰点布局）与状态分单调性；
- 主线板块综合排序（百分位加权、缺失字段归一化、黑名单过滤、连续强势天数）；
- 仓位建议映射与免责声明；
- build_regime_snapshot 字段齐全、失败降级 partial（板块失败/数据全缺/历史失败不崩溃）；
- 取数降级链路（腾讯→新浪、同花顺→东财）用 monkeypatch 模拟，不访问网络；
- scripts/regime_report.py 摘要函数离线可用。
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import quant.data.regime as regime

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "regime_report.py"


# ---------------------------------------------------------------------------
# 合成数据
# ---------------------------------------------------------------------------
def index_daily(kind: str = "up", n: int = 120) -> pd.DataFrame:
    """构造腾讯口径指数日线（date/open/close/high/low/volume/amount）。"""
    dates = pd.bdate_range("2026-05-01", periods=n).strftime("%Y-%m-%d")
    if kind == "up":
        close = 3000.0 * (1.005 ** np.arange(n))
        amount = 3.0e11 * (1.002 ** np.arange(n))
        amount[-1] *= 1.5  # 末日放量
    elif kind == "down":
        close = 3600.0 * (0.995 ** np.arange(n))
        amount = 3.0e11 * (0.998 ** np.arange(n))
        amount[-1] *= 1.5  # 放量下跌
    elif kind == "flat":
        close = np.full(n, 3000.0)
        amount = np.full(n, 3.0e11)
    else:  # pragma: no cover - 防御
        raise ValueError(kind)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "close": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "volume": amount / 10.0,
            "amount": amount,
        }
    )


def activity_hot() -> dict:
    return {
        "up": 4200,
        "down": 800,
        "flat": 100,
        "suspended": 10,
        "limit_up": 110,
        "limit_down": 8,
        "active_pct": 18.0,
        "asof": "2026-11-20 15:00:00",
    }


def activity_ice() -> dict:
    return {
        "up": 290,
        "down": 4900,
        "flat": 20,
        "suspended": 8,
        "limit_up": 30,
        "limit_down": 68,
        "active_pct": 5.69,
        "asof": "2026-11-20 15:00:00",
    }


def activity_neutral() -> dict:
    return {
        "up": 2500,
        "down": 2500,
        "flat": 100,
        "suspended": 10,
        "limit_up": 50,
        "limit_down": 50,
        "active_pct": 10.0,
        "asof": "2026-11-20 15:00:00",
    }


def boards_sample() -> list[dict]:
    """单调强弱构造：半导体（三强）> AI PC（1日强/5日次强）> 银行 > 煤炭 > 白酒。"""
    return [
        {"name": "半导体", "kind": "industry", "pct_1d": 3.2, "pct_5d": 8.5, "net_inflow": 25.0},
        {"name": "AI PC", "kind": "concept", "pct_1d": 4.1, "pct_5d": 6.0, "net_inflow": 12.0},
        {"name": "银行", "kind": "industry", "pct_1d": 1.2, "pct_5d": 3.1, "net_inflow": 18.0},
        {"name": "煤炭", "kind": "industry", "pct_1d": 2.0, "pct_5d": 1.0, "net_inflow": 2.0},
        {"name": "白酒", "kind": "industry", "pct_1d": -0.5, "pct_5d": -2.0, "net_inflow": -5.0},
        {"name": "地产", "kind": "industry", "pct_1d": -1.5, "pct_5d": -4.0, "net_inflow": -8.0},
    ]


def board_hist(closes: list[float]) -> pd.DataFrame:
    dates = pd.bdate_range("2026-11-01", periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"日期": dates, "收盘价": closes})


def synthetic_payload(index_kind: str = "down", activity: dict | None = None) -> dict:
    hist = board_hist([100, 101, 102, 103, 104, 105])
    return {
        "index_daily": index_daily(index_kind),
        "activity": activity if activity is not None else activity_ice(),
        "boards": boards_sample(),
        "hist_fetcher": lambda kind, name: hist,
        "errors": [],
        "warnings": [],
        "sources": {"index": "synthetic", "emotion": "synthetic", "sectors": "synthetic"},
    }


# ---------------------------------------------------------------------------
# 指数归一化 / 趋势
# ---------------------------------------------------------------------------
def test_normalize_index_daily_sina_columns_without_amount() -> None:
    df = pd.DataFrame(
        {
            "date": ["2026-11-19", "2026-11-20", "2026-11-20"],
            "open": [1.0, 2.0, 3.0],
            "high": [1.0, 2.0, 3.0],
            "low": [1.0, 2.0, 3.0],
            "close": [10.0, 11.0, 11.5],
            "volume": [100, 200, 300],
        }
    )
    out = regime.normalize_index_daily(df)
    assert out is not None
    assert out["date"].tolist() == ["2026-11-19", "2026-11-20"]
    assert out["close"].iloc[-1] == 11.5  # 同日去重保留最后
    assert out["amount"].isna().all()


def test_normalize_index_daily_invalid_returns_none() -> None:
    assert regime.normalize_index_daily(None) is None
    assert regime.normalize_index_daily(pd.DataFrame()) is None
    assert regime.normalize_index_daily(pd.DataFrame({"foo": [1, 2]})) is None


def test_analyze_trend_up() -> None:
    trend = regime.analyze_trend(index_daily("up"))
    assert trend["index"] == regime.INDEX_NAME
    assert trend["ma5"] > trend["ma20"] > trend["ma60"] > 0
    assert trend["ma_state"] == "多头排列"
    assert trend["ret5"] > 0 and trend["ret20"] > 0
    assert trend["close"] == pytest.approx(float(index_daily("up")["close"].iloc[-1]), abs=0.01)
    assert trend["amount_ratio"] > 1.0  # 末日放量
    assert trend["trade_date"] == "2026-11-20" or trend["trade_date"] is not None


def test_analyze_trend_down() -> None:
    trend = regime.analyze_trend(index_daily("down"))
    assert trend["ma5"] < trend["ma20"] < trend["ma60"]
    assert trend["ma_state"] == "空头排列"
    assert trend["ret5"] < 0 and trend["ret20"] < 0


def test_analyze_trend_insufficient_history() -> None:
    trend = regime.analyze_trend(index_daily("up", n=30))
    assert trend["ma60"] is None
    assert trend["ma5"] is not None and trend["ma20"] is not None
    assert trend["ma_state"] == "震荡"
    assert trend["ret5"] is not None and trend["ret20"] is not None

    short = regime.analyze_trend(index_daily("up", n=5))
    assert short["ma20"] is None and short["ma60"] is None
    assert short["ret5"] is None and short["ret20"] is None


def test_analyze_trend_none_safe() -> None:
    trend = regime.analyze_trend(None)
    assert trend["index"] == regime.INDEX_NAME
    assert trend["ma5"] is None and trend["ma_state"] == "震荡"
    assert trend["ret5"] is None and trend["close"] is None


def test_score_trend_bounds_and_ordering() -> None:
    up = regime.score_trend(regime.analyze_trend(index_daily("up")))
    down = regime.score_trend(regime.analyze_trend(index_daily("down")))
    flat = regime.score_trend(regime.analyze_trend(index_daily("flat")))
    assert 0.0 <= down < flat < up <= 100.0
    assert regime.score_trend(regime.analyze_trend(None)) == 50.0


def test_score_volume_direction_aware() -> None:
    up = regime.analyze_trend(index_daily("up"))
    down = regime.analyze_trend(index_daily("down"))
    assert regime.score_volume(up) > 50.0  # 强势放量
    assert regime.score_volume(down) < 50.0  # 放量下跌
    assert regime.score_volume(regime.analyze_trend(None)) == 50.0


# ---------------------------------------------------------------------------
# 情绪
# ---------------------------------------------------------------------------
def test_emotion_from_activity_hot_and_ice() -> None:
    hot = regime.emotion_from_activity(activity_hot())
    assert hot["label"] in {"偏热", "过热"}
    assert hot["score"] >= 60
    assert hot["limit_up"] == 110 and hot["limit_down"] == 8
    assert 0.0 <= hot["breadth"] <= 1.0

    ice = regime.emotion_from_activity(activity_ice())
    assert ice["label"] == "冰点"
    assert ice["score"] < 30
    assert ice["limit_up"] == 30 and ice["limit_down"] == 68


@pytest.mark.parametrize("payload", [None, {}, {"up": 0, "down": 0}])
def test_emotion_from_activity_missing_is_neutral(payload) -> None:
    emotion = regime.emotion_from_activity(payload)
    assert emotion["score"] == 50.0
    assert emotion["label"] == "中性"


# ---------------------------------------------------------------------------
# 大盘状态机
# ---------------------------------------------------------------------------
def _state(index_kind: str, activity: dict) -> dict:
    trend = regime.analyze_trend(index_daily(index_kind))
    emotion = regime.emotion_from_activity(activity)
    return regime.decide_market_state(trend, emotion)


def test_state_offense() -> None:
    state = _state("up", activity_hot())
    assert state["state"] == "offense"
    assert state["label"] == "进攻"
    assert state["score"] >= 70
    assert 3 <= len(state["reasons"]) <= 5


def test_state_ice_watch() -> None:
    state = _state("down", activity_ice())
    assert state["state"] == "ice_watch"
    assert state["label"] == "冰点布局"
    assert state["score"] < 40
    assert 3 <= len(state["reasons"]) <= 5
    assert any("冰点" in reason for reason in state["reasons"])


def test_state_range_or_lean_bear_for_flat_market() -> None:
    state = _state("flat", activity_neutral())
    # 平盘指数 + 中性情绪：应落在震荡/偏空/偏多之间，且分数居中
    assert state["state"] in {"range", "lean_bear", "lean_bull"}
    assert 40 <= state["score"] <= 60
    assert state["label"] == regime.STATE_LABELS[state["state"]]


def test_state_score_monotonic() -> None:
    offense = _state("up", activity_hot())["score"]
    neutral = _state("flat", activity_neutral())["score"]
    ice = _state("down", activity_ice())["score"]
    assert offense > neutral > ice


def test_state_labels_cover_all_states() -> None:
    assert set(regime.STATE_LABELS) == set(regime.STATE_POSITION)
    assert regime.STATE_LABELS == {
        "offense": "进攻",
        "lean_bull": "偏多",
        "range": "震荡",
        "lean_bear": "偏空",
        "ice_watch": "冰点布局",
    }


# ---------------------------------------------------------------------------
# 主线板块
# ---------------------------------------------------------------------------
def test_rank_leaders_order_and_fields() -> None:
    leaders = regime.rank_leaders(boards_sample())
    assert len(leaders) == regime.LEADER_TOP_N
    scores = [leader["score"] for leader in leaders]
    assert scores == sorted(scores, reverse=True)
    first = leaders[0]
    assert first["name"] in {"半导体", "AI PC"}
    for leader in leaders:
        assert 0.0 <= leader["score"] <= 100.0
        assert leader["tag"] in {"领涨", "活跃"}
        assert leader["kind"] in {"industry", "concept"}
        assert isinstance(leader["days"], int)
        assert leader["pct_1d"] is not None


def test_rank_leaders_tag_uses_1d_percentile() -> None:
    leaders = regime.rank_leaders(boards_sample(), top_n=10)
    top_1d = max(leaders, key=lambda item: item["pct_1d"])
    assert top_1d["tag"] == "领涨"
    assert any(leader["tag"] == "活跃" for leader in leaders)


def test_rank_leaders_missing_components_renormalized() -> None:
    boards = [
        {"name": "A", "kind": "industry", "pct_1d": 5.0},
        {"name": "B", "kind": "industry", "pct_1d": 1.0, "pct_5d": 2.0, "net_inflow": 3.0},
        {"name": "C", "kind": "industry", "pct_5d": 9.0, "net_inflow": 10.0},
        {"name": "D", "kind": "industry"},
    ]
    leaders = regime.rank_leaders(boards)
    names = [leader["name"] for leader in leaders]
    assert "D" not in names  # 三项全缺失不参与
    for leader in leaders:
        assert not math.isnan(leader["score"])
        assert 0.0 <= leader["score"] <= 100.0
    json.dumps(leaders)  # 必须可 JSON 序列化


def test_rank_leaders_filters_cross_section_concepts() -> None:
    boards = [
        {"name": "半导体", "kind": "industry", "pct_1d": 1.0, "pct_5d": 2.0, "net_inflow": 3.0},
        {"name": "融资融券", "kind": "concept", "pct_1d": 9.0, "pct_5d": 9.0, "net_inflow": 9.0},
        {"name": "同花顺漂亮100", "kind": "concept", "pct_1d": 9.0, "pct_5d": 9.0, "net_inflow": 9.0},
        {"name": "减速器", "kind": "concept", "pct_1d": 2.0, "pct_5d": 3.0, "net_inflow": 4.0},
    ]
    names = [leader["name"] for leader in regime.rank_leaders(boards)]
    assert "半导体" in names and "减速器" in names
    assert "融资融券" not in names and "同花顺漂亮100" not in names


@pytest.mark.parametrize("boards", [[], None])
def test_rank_leaders_empty(boards) -> None:
    assert regime.rank_leaders(boards) == []


def test_rank_leaders_days_map() -> None:
    days_map = {"industry:半导体": 3, "concept:AI PC": 2}
    leaders = regime.rank_leaders(boards_sample(), days_map=days_map)
    by_name = {leader["name"]: leader for leader in leaders}
    assert by_name["半导体"]["days"] == 3
    assert by_name["AI PC"]["days"] == 2
    assert by_name["银行"]["days"] == 0


# ---------------------------------------------------------------------------
# 连续强势天数 / 板块历史归一化
# ---------------------------------------------------------------------------
def test_consecutive_up_days() -> None:
    assert regime.consecutive_up_days(board_hist([100, 101, 102, 103, 104, 105])) == 5
    assert regime.consecutive_up_days(board_hist([100, 102, 101, 103, 102])) == 0
    assert regime.consecutive_up_days(board_hist([100, 100, 100])) == 0
    assert regime.consecutive_up_days(None) == 0
    assert regime.consecutive_up_days(board_hist([100, 101])) == 1
    assert regime.consecutive_up_days(board_hist([100, 101, 102, 103]), max_days=2) == 2


def test_normalize_board_history_ths_and_em() -> None:
    ths = pd.DataFrame({"日期": ["2026-11-19", "2026-11-20"], "收盘价": [1.0, 2.0]})
    em = pd.DataFrame({"日期": ["2026-11-19", "2026-11-20"], "收盘": [3.0, 4.0]})
    for frame, expected in ((ths, 2.0), (em, 4.0)):
        out = regime.normalize_board_history(frame)
        assert out is not None and out["close"].iloc[-1] == expected
    assert regime.normalize_board_history(pd.DataFrame({"foo": [1]})) is None
    assert regime.normalize_board_history(None) is None


# ---------------------------------------------------------------------------
# 仓位建议
# ---------------------------------------------------------------------------
def test_position_advice_all_states() -> None:
    expected_range = {
        "offense": "6-8成",
        "lean_bull": "4-6成",
        "range": "2-4成",
        "lean_bear": "0-2成",
        "ice_watch": "0-2成",
    }
    emotion = regime.emotion_from_activity(activity_ice())
    for state, range_text in expected_range.items():
        advice = regime.position_advice({"state": state, "score": 30.0}, emotion)
        assert advice["range"] == range_text
        assert advice["label"]
        assert advice["rationale"]
        assert advice["triggers"]
        assert advice["disclaimer"] == regime.DISCLAIMER
        assert "研究用途" in advice["disclaimer"] and "不构成投资建议" in advice["disclaimer"]


def test_position_advice_unknown_state_falls_back() -> None:
    advice = regime.position_advice({"state": "???"}, regime.emotion_from_activity(None))
    assert advice["range"] == "2-4成"
    assert advice["disclaimer"] == regime.DISCLAIMER


# ---------------------------------------------------------------------------
# build_regime_snapshot：字段齐全 / 降级
# ---------------------------------------------------------------------------
REQUIRED_TOP_KEYS = {
    "schema",
    "asof",
    "status",
    "market_state",
    "trend",
    "emotion",
    "leaders",
    "position",
    "data_quality",
}
REQUIRED_TREND_KEYS = {"index", "ma5", "ma20", "ma60", "ma_state", "ret5", "ret20"}
REQUIRED_EMOTION_KEYS = {"score", "label", "breadth", "limit_up", "limit_down"}


def test_build_snapshot_offline_complete() -> None:
    snapshot = regime.build_regime_snapshot(data=synthetic_payload())
    assert REQUIRED_TOP_KEYS <= set(snapshot)
    assert snapshot["schema"] == 1
    assert snapshot["status"] == "ok"
    assert snapshot["asof"].endswith("+08:00")
    assert snapshot["trade_date"]

    state = snapshot["market_state"]
    assert state["state"] in regime.STATE_LABELS
    assert state["label"] == regime.STATE_LABELS[state["state"]]
    assert 0.0 <= state["score"] <= 100.0
    assert 3 <= len(state["reasons"]) <= 5
    assert set(state["score_parts"]) == {"trend", "emotion", "volume"}
    for value in state["score_parts"].values():
        assert 0.0 <= value <= 100.0

    assert REQUIRED_TREND_KEYS <= set(snapshot["trend"])
    assert REQUIRED_EMOTION_KEYS <= set(snapshot["emotion"])
    assert snapshot["emotion"]["limit_up"] == 30
    assert snapshot["emotion"]["limit_down"] == 68

    leaders = snapshot["leaders"]
    assert 1 <= len(leaders) <= regime.LEADER_TOP_N
    scores = [leader["score"] for leader in leaders]
    assert scores == sorted(scores, reverse=True)
    for leader in leaders:
        assert {"name", "pct_1d", "pct_5d", "net_inflow", "score", "days", "tag"} <= set(leader)
        assert isinstance(leader["days"], int)

    position = snapshot["position"]
    assert {"range", "label", "rationale", "disclaimer"} <= set(position)
    assert position["disclaimer"] == regime.DISCLAIMER
    assert "不构成投资建议" in position["disclaimer"]
    assert snapshot["disclaimer"] == regime.DISCLAIMER

    # JSON 可序列化且往返一致（供落盘/前端消费）
    dumped = json.dumps(snapshot, ensure_ascii=False)
    assert json.loads(dumped) == snapshot


def test_build_snapshot_hist_failure_only_warns() -> None:
    payload = synthetic_payload()

    def boom(kind: str, name: str) -> pd.DataFrame:
        raise RuntimeError("hist boom")

    payload["hist_fetcher"] = boom
    snapshot = regime.build_regime_snapshot(data=payload)
    assert snapshot["status"] == "ok"
    assert all(leader["days"] == 0 for leader in snapshot["leaders"])
    assert any("board_hist" in warning for warning in snapshot["data_quality"]["warnings"])


def test_build_snapshot_partial_when_sectors_fail() -> None:
    payload = synthetic_payload()
    payload.update({"boards": [], "errors": ["sectors_ths: RuntimeError", "sectors_em: Push2RateLimitError"]})
    snapshot = regime.build_regime_snapshot(data=payload)
    assert snapshot["status"] == "partial"
    assert snapshot["data_quality"]["status"] == "partial"
    assert snapshot["leaders"] == []
    assert any("sectors" in error for error in snapshot["data_quality"]["errors"])
    # 板块缺失时仍输出大盘状态与仓位建议
    assert snapshot["market_state"]["label"]
    assert snapshot["position"]["disclaimer"] == regime.DISCLAIMER
    assert any("板块" in warning for warning in snapshot["data_quality"]["warnings"])


def test_build_snapshot_partial_when_all_missing() -> None:
    snapshot = regime.build_regime_snapshot(
        data={
            "index_daily": None,
            "activity": None,
            "boards": [],
            "hist_fetcher": None,
            "errors": ["index_tx: RuntimeError", "activity: RuntimeError"],
            "warnings": [],
            "sources": {},
        }
    )
    assert snapshot["status"] == "partial"
    assert REQUIRED_TOP_KEYS <= set(snapshot)
    assert snapshot["market_state"]["state"] in regime.STATE_LABELS
    assert 3 <= len(snapshot["market_state"]["reasons"]) <= 5
    assert snapshot["trend"]["ma5"] is None
    assert snapshot["emotion"]["score"] == 50.0
    assert snapshot["data_quality"]["errors"]
    json.dumps(snapshot)


def test_build_snapshot_days_from_history() -> None:
    snapshot = regime.build_regime_snapshot(data=synthetic_payload())
    assert snapshot["leaders"], "需要至少一个主线板块"
    assert all(leader["days"] == 5 for leader in snapshot["leaders"])  # 合成历史连续 5 天上涨


# ---------------------------------------------------------------------------
# 取数降级链路（monkeypatch，不联网）
# ---------------------------------------------------------------------------
def test_collect_market_data_falls_back_to_sina_and_em(monkeypatch) -> None:
    monkeypatch.setattr(regime, "fetch_index_daily_tx", lambda: (_ for _ in ()).throw(RuntimeError("tx down")))
    monkeypatch.setattr(regime, "fetch_index_daily", lambda symbol: index_daily("flat"))
    monkeypatch.setattr(regime, "fetch_market_activity", lambda: activity_ice())
    monkeypatch.setattr(
        regime, "fetch_boards_ths", lambda: (_ for _ in ()).throw(RuntimeError("ths down"))
    )
    monkeypatch.setattr(regime, "fetch_boards_em", lambda: (boards_sample(), ["em:warning"]))
    monkeypatch.setattr(regime, "SECTOR_SLEEP", 0.0)

    data = regime.collect_market_data()
    assert data["index_daily"] is not None
    assert data["activity"]["limit_up"] == 30
    assert data["boards"] == boards_sample()
    assert "sina" in data["sources"]["index"]
    assert "eastmoney" in data["sources"]["sectors"]
    assert any("腾讯" in warning for warning in data["warnings"])
    assert any("index_tx" in error for error in data["errors"])

    snapshot = regime.build_regime_snapshot(data=data)
    assert snapshot["status"] == "ok"


def test_collect_market_data_all_down_does_not_raise(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("down")

    for name in ("fetch_index_daily_tx", "fetch_index_daily", "fetch_market_activity",
                 "fetch_boards_ths", "fetch_boards_em"):
        monkeypatch.setattr(regime, name, boom)
    monkeypatch.setattr(regime, "SECTOR_SLEEP", 0.0)

    data = regime.collect_market_data()
    assert data["index_daily"] is None
    assert data["activity"] is None
    assert data["boards"] == []
    assert len(data["errors"]) >= 3
    snapshot = regime.build_regime_snapshot(data=data)
    assert snapshot["status"] == "partial"
    json.dumps(snapshot)


# ---------------------------------------------------------------------------
# 同花顺 / 东财板块返回归一化
# ---------------------------------------------------------------------------
def test_normalize_ths_boards_merge_and_filter() -> None:
    now = pd.DataFrame(
        {
            "行业": ["保险", "银行", "融资融券"],
            "行业-涨跌幅": [0.39, 0.21, 3.0],
            "净额": [12.52, 43.12, 9.9],
            "公司家数": [5, 84, 300],
            "领涨股": ["中国太保", "工商银行", "某某"],
        }
    )
    stage = pd.DataFrame(
        {
            "行业": ["保险", "银行"],
            "阶段涨跌幅": ["2.74%", "2.55%"],
            "净额": [4.84, 61.79],
        }
    )
    boards = regime._normalize_ths_boards(now, stage, "industry")
    by_name = {board["name"]: board for board in boards}
    assert by_name["保险"]["pct_1d"] == pytest.approx(0.39)
    assert by_name["保险"]["pct_5d"] == pytest.approx(2.74)  # 百分比字符串解析
    assert by_name["保险"]["net_inflow"] == pytest.approx(12.52)
    assert by_name["保险"]["net_inflow_5d"] == pytest.approx(4.84)

    concepts = regime._normalize_ths_boards(now, stage, "concept")
    names = [board["name"] for board in concepts]
    assert "融资融券" not in names  # 横截面标签被过滤
    assert "保险" in names


def test_normalize_em_boards_indicator_5d() -> None:
    names = pd.DataFrame({"板块名称": ["半导体"], "涨跌幅": [3.2]})
    rank = pd.DataFrame(
        {
            "名称": ["半导体"],
            "5日涨跌幅": [8.5],
            "5日主力净流入-净额": [2.5e9],
        }
    )
    boards = regime._normalize_em_boards(names, rank, "industry", indicator="5日")
    assert boards[0]["pct_5d"] == pytest.approx(8.5)
    assert boards[0]["net_inflow_5d"] == pytest.approx(25.0)
    assert boards[0]["net_inflow"] is None


def test_normalize_em_boards_units_and_columns() -> None:
    names = pd.DataFrame(
        {"板块名称": ["半导体", "银行"], "涨跌幅": [3.2, 0.5]}
    )
    rank = pd.DataFrame(
        {
            "名称": ["半导体", "银行"],
            "今日涨跌幅": [3.2, 0.5],
            "今日主力净流入-净额": [2.5e9, -1.2e9],  # 东财单位：元 → 亿元
        }
    )
    boards = regime._normalize_em_boards(names, rank, "industry")
    by_name = {board["name"]: board for board in boards}
    assert by_name["半导体"]["pct_1d"] == pytest.approx(3.2)
    assert by_name["半导体"]["net_inflow"] == pytest.approx(25.0)
    assert by_name["银行"]["net_inflow"] == pytest.approx(-12.0)
    assert by_name["半导体"]["pct_5d"] is None


def test_fetch_boards_em_stops_after_rate_limit(monkeypatch) -> None:
    """东财限流后必须立即中止兜底链路，不再反复调用东财接口。"""
    import akshare as ak

    class Push2RateLimitError(Exception):
        pass

    calls: list[str] = []

    def boom() -> pd.DataFrame:
        calls.append("call")
        raise Push2RateLimitError("当前 IP 被临时限流")

    monkeypatch.setattr(ak, "stock_board_industry_name_em", boom)
    monkeypatch.setattr(ak, "stock_board_concept_name_em", boom)
    monkeypatch.setattr(regime, "SECTOR_SLEEP", 0.0)

    with pytest.raises(RuntimeError):
        regime.fetch_boards_em()
    assert calls == ["call"]  # 第一次限流后不再调用任何东财接口


def test_build_snapshot_fills_pct_5d_from_history() -> None:
    """东财兜底缺 5 日涨幅时，用板块历史收盘价补全展示值。"""
    payload = synthetic_payload()
    payload["boards"] = [
        {"name": "半导体", "kind": "industry", "pct_1d": 3.2, "net_inflow": 25.0},
        {"name": "银行", "kind": "industry", "pct_1d": 1.2, "net_inflow": 18.0},
    ]
    payload["hist_fetcher"] = lambda kind, name: board_hist([100, 101, 102, 103, 104, 105])
    snapshot = regime.build_regime_snapshot(data=payload)
    assert snapshot["leaders"]
    for leader in snapshot["leaders"]:
        assert leader["pct_5d"] == pytest.approx(5.0)
        assert leader["days"] == 5


# ---------------------------------------------------------------------------
# 报告脚本摘要（离线）
# ---------------------------------------------------------------------------
def test_report_script_summary_offline() -> None:
    spec = importlib.util.spec_from_file_location("regime_report_script", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    snapshot = regime.build_regime_snapshot(data=synthetic_payload())
    lines = module.summary_lines(snapshot)
    text = "\n".join(lines)
    assert "市场状态：" in text
    assert "趋势：" in text
    assert "情绪：" in text
    assert "行情主线 Top" in text
    assert "仓位建议：" in text
    assert "研究用途，不构成投资建议" in text
    assert "数据质量：ok" in text
