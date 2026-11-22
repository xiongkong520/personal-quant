"""市场状态引擎：大盘状态机 + 行情主线 + 仓位建议（契约 §15 / task-7）。

产出 ``data/reports/regime.json``（``schema=1``），供本地 API / Cloudflare / 前端展示。
全部结论只用于研究学习，**不构成投资建议**。

设计要点
--------
1. 数据源分层（每个接口失败都可降级，单点故障不崩溃）：
   - 指数趋势：腾讯 ``index_zh_daily_tx``（含成交额，首选）→ 新浪 ``index_zh_daily_sina``
     （仅成交量，降级口径）兜底；
   - 市场宽度/情绪：复用 :mod:`quant.data.market` 的 ``stock_market_activity_legu`` +
     ``compute_sentiment``；
   - 板块行情：同花顺（``stock_fund_flow_industry_ths`` / ``stock_fund_flow_concept_ths``，
     ``即时`` + ``5日排行`` 各一次即可拿到全部行业/概念的当日、5 日涨幅与净流入，共 4 次请求）
     为首选；东方财富（``stock_board_industry_name_em`` / ``stock_fund_flow_sector_rank_em``）
     为兜底。东财 push2 限流异常（``Push2RateLimitError``）**不重试**，直接降级，避免延长限流。
2. 规则可解释：
   ``状态分 = 0.45*趋势分 + 0.40*情绪分 + 0.15*量能分``，再叠加冰点守卫（情绪<20 且宽度<15%
   等）；主线综合分 = 当日涨幅百分位 34% + 5 日涨幅百分位 33% + 净流入百分位 33%。
3. 全部分析函数离线可测：:func:`build_regime_snapshot` 支持注入 ``data=`` 合成数据，
   测试不联网。

字段单位约定
------------
- ``trend.ret5 / trend.ret20``：百分比（``-1.54`` 表示 -1.54%）；
- ``leaders[].pct_1d / pct_5d``：百分比；
- ``leaders[].net_inflow / net_inflow_5d``：亿元；
- ``leaders[].score``：0-100 的板块综合分。
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

from quant.data.market import compute_sentiment, fetch_index_daily, fetch_market_activity

TZ = timezone(timedelta(hours=8))

#: 大盘状态 → 中文标签
STATE_LABELS: dict[str, str] = {
    "offense": "进攻",
    "lean_bull": "偏多",
    "range": "震荡",
    "lean_bear": "偏空",
    "ice_watch": "冰点布局",
}

#: 状态 → (建议仓位区间, 仓位标签, 一句话理由, 触发条件)
STATE_POSITION: dict[str, tuple[str, str, str, list[str]]] = {
    "offense": (
        "6-8成",
        "高仓位",
        "趋势与情绪共振向上，可按趋势行情参与，回撤容忍度适度提高",
        [
            "加仓条件：沪深300 站稳 20 日线且情绪分维持在 60 以上",
            "减仓条件：跌破 20 日线或单日跌停家数超过 30 家",
        ],
    ),
    "lean_bull": (
        "4-6成",
        "中高仓位",
        "偏多结构下跟随主线板块，避免追高，保留机动仓位",
        [
            "加仓条件：主线板块 5 日涨幅与净流入同时位居前列",
            "减仓条件：状态分回落至 50 以下或情绪分跌破 40",
        ],
    ),
    "range": (
        "2-4成",
        "中性仓位",
        "震荡市以低吸高抛为主，快进快出，不做趋势性加仓",
        [
            "加仓条件：指数放量站上 20 日线且情绪分回升至 55 以上",
            "减仓条件：反弹缩量或跌停家数明显多于涨停家数",
        ],
    ),
    "lean_bear": (
        "0-2成",
        "低仓位",
        "均线空头排列且情绪偏冷，防守为主，等待企稳信号再提升仓位",
        [
            "加仓条件：涨跌宽度连续 3 日回升且指数止跌",
            "风控条件：指数再创新低或跌停家数超过 100 家时保持观望",
        ],
    ),
    "ice_watch": (
        "0-2成",
        "试错仓位",
        "冰点区左侧布局窗口，只用小仓位试错修复，跌停潮未收敛前不加仓",
        [
            "试错条件：情绪分<20 且涨跌宽度<15% 时可小仓位试冰点修复（≤2成）",
            "加仓条件：宽度回升至 30% 以上且涨停家数多于跌停家数",
            "风控条件：指数再创新低或跌停家数超过 100 家时保持观望",
        ],
    ),
}

#: 研究用途免责声明（对外展示时必须携带）
DISCLAIMER = "研究用途，不构成投资建议"

#: 指数代码 / 名称（趋势判断基准）
INDEX_CODE = "sh000300"
INDEX_NAME = "沪深300"

#: 主线板块数量
LEADER_TOP_N = 5

#: 主线综合分权重：当日涨幅 / 5 日涨幅 / 资金净流入
LEADER_WEIGHTS: dict[str, float] = {"pct_1d": 0.34, "pct_5d": 0.33, "net_inflow": 0.33}

#: 板块接口最小请求间隔（秒），东财/同花顺均可能限流
SECTOR_SLEEP = 0.35

#: 板块历史（连续强势天数）请求间隔（秒）
HIST_SLEEP = 0.3

#: 概念板块黑名单：这些是横截面标签/同花顺自编指数，不是行情主线
CONCEPT_NAME_BLACKLIST: frozenset[str] = frozenset(
    {
        "融资融券",
        "沪股通",
        "深股通",
        "转债标的",
        "机构重仓",
        "基金重仓",
        "社保重仓",
        "QFII重仓",
        "券商重仓",
        "保险重仓",
        "信托重仓",
        "破净股",
        "高送转",
        "次新股",
        "低价股",
        "微盘股",
        "ST板块",
        "举牌",
        "参股新三板",
        "股权转让",
        "MSCI中国",
        "标普道琼斯A股",
        "富时罗素概念",
        "预盈预增",
        "预亏预减",
        "昨日涨停",
        "昨日连板",
        "昨日触板",
        "同花顺漂亮100",
        "同花顺中特估100",
    }
)
CONCEPT_PREFIX_BLACKLIST: tuple[str, ...] = ("同花顺",)

#: 情绪兜底（数据缺失时按中性处理）
NEUTRAL_EMOTION: dict[str, Any] = {
    "score": 50.0,
    "label": "中性",
    "state": "neutral",
    "breadth": 0.5,
    "limit_up": 0,
    "limit_down": 0,
    "limit_ratio": 0.5,
    "active_pct": 0.0,
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _num(value: Any, default: float | None = None) -> float | None:
    """把接口返回的数值（可能带 ``%`` / 千分位 / ``-`` 占位）解析为 float。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float, np.integer, np.floating)):
        out = float(value)
        return default if math.isnan(out) or math.isinf(out) else out
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in {"", "-", "--", "None", "nan", "NaN", "null"}:
        return default
    try:
        out = float(text)
    except ValueError:
        return default
    return default if math.isnan(out) or math.isinf(out) else out


def _round(value: Any, digits: int = 2) -> float | None:
    num = _num(value)
    return None if num is None else round(num, digits)


def _is_rate_limit_error(exc: BaseException) -> bool:
    """东财 push2 限流：1.20.x 会抛 Push2RateLimitError，明确要求不要反复调用。"""
    name = type(exc).__name__
    text = str(exc)
    return "Push2RateLimit" in name or "被临时限流" in text or "429" in text


def _call_with_retry(
    fn: Callable[[], Any], *, attempts: int = 2, wait: float = 0.8, label: str = ""
) -> Any:
    """失败重试 1 次（默认）；限流异常不重试，直接抛出由上层降级。"""
    last: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 数据源异常统一兜底
            if _is_rate_limit_error(exc):
                raise
            last = exc
            if i + 1 < max(1, attempts):
                time.sleep(wait)
    if last is not None:
        raise last
    raise RuntimeError(f"call failed: {label or fn!r}")


# ---------------------------------------------------------------------------
# 趋势（指数均线/动量/成交额）
# ---------------------------------------------------------------------------
def _column(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in df.columns:
            return name
    return None


def normalize_index_daily(daily: pd.DataFrame | None) -> pd.DataFrame | None:
    """统一指数日线为 ``date / close / amount``（amount 可为空），按日期升序。"""
    if daily is None or len(daily) == 0:
        return None
    df = pd.DataFrame(daily).copy()
    date_col = _column(df, ("date", "日期"))
    close_col = _column(df, ("close", "收盘", "收盘价"))
    amount_col = _column(df, ("amount", "成交额"))
    if close_col is None:
        return None
    if date_col is None:
        df["date"] = [f"row{i}" for i in range(len(df))]
        date_col = "date"
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d"),
            "close": pd.to_numeric(df[close_col], errors="coerce"),
        }
    )
    out["amount"] = (
        pd.to_numeric(df[amount_col], errors="coerce") if amount_col else np.nan
    )
    out = out.dropna(subset=["date", "close"]).drop_duplicates(subset=["date"], keep="last")
    return out.sort_values("date").reset_index(drop=True)


def analyze_trend(daily: pd.DataFrame | None, index_name: str = INDEX_NAME) -> dict[str, Any]:
    """指数趋势指标：均线、均线排列、5/20 日动量、成交额相对 20 日均值。

    返回契约 §15 ``trend`` 字段（缺失值用 ``None``），另附 ``close / amount /
    amount_ratio / trade_date`` 便于报告解释。
    """
    trend: dict[str, Any] = {
        "index": index_name,
        "ma5": None,
        "ma20": None,
        "ma60": None,
        "ma_state": "震荡",
        "ret5": None,
        "ret20": None,
        "close": None,
        "amount": None,
        "amount_ratio": None,
        "vol_ratio": None,
        "trade_date": None,
    }
    normalized = normalize_index_daily(daily)
    if normalized is None or len(normalized) < 2:
        return trend

    close = normalized["close"].astype(float)
    trend["trade_date"] = str(normalized["date"].iloc[-1])
    trend["close"] = _round(close.iloc[-1])

    for window, key in ((5, "ma5"), (20, "ma20"), (60, "ma60")):
        if len(close) >= window:
            trend[key] = _round(close.rolling(window).mean().iloc[-1])

    if len(close) >= 6:
        trend["ret5"] = _round((close.iloc[-1] / close.iloc[-6] - 1.0) * 100.0)
    if len(close) >= 21:
        trend["ret20"] = _round((close.iloc[-1] / close.iloc[-21] - 1.0) * 100.0)

    ma5, ma20, ma60 = trend["ma5"], trend["ma20"], trend["ma60"]
    if ma5 is not None and ma20 is not None and ma60 is not None:
        above_ma20 = trend["close"] is not None and trend["close"] > ma20
        if ma5 > ma20 > ma60 and above_ma20:
            trend["ma_state"] = "多头排列"
        elif ma5 < ma20 < ma60 and not above_ma20:
            trend["ma_state"] = "空头排列"
        else:
            trend["ma_state"] = "震荡"

    amount = normalized["amount"].astype(float).dropna()
    if len(amount) >= 21:
        base = float(amount.iloc[-21:-1].mean())
        if base > 0:
            trend["amount"] = _round(amount.iloc[-1] / 1e8)  # 亿元
            trend["amount_ratio"] = _round(amount.iloc[-1] / base, 4)
    return trend


def score_trend(trend: dict[str, Any]) -> float:
    """趋势分 0-100：均线排列 ±20，收盘相对 MA20 ±10，5 日动量 ±15，20 日动量 ±10。"""
    ma5, ma20, ma60 = trend.get("ma5"), trend.get("ma20"), trend.get("ma60")
    close, ret5, ret20 = trend.get("close"), trend.get("ret5"), trend.get("ret20")
    if ma5 is None or ma20 is None or ma60 is None:
        return 50.0

    score = 50.0
    if ma5 > ma20 > ma60:
        score += 20.0
    elif ma5 > ma20:
        score += 10.0
    elif ma5 < ma20 < ma60:
        score -= 20.0
    else:
        score -= 10.0

    if close is not None and ma20 > 0:
        score += _clamp((close / ma20 - 1.0) * 100.0 * 2.0, -10.0, 10.0)
    if ret5 is not None:
        score += _clamp(float(ret5) * 2.5, -15.0, 15.0)
    if ret20 is not None:
        score += _clamp(float(ret20) * 1.0, -10.0, 10.0)
    return round(_clamp(score, 0.0, 100.0), 1)


def score_volume(trend: dict[str, Any]) -> float:
    """量能分 0-100：成交额（缺失则成交量）相对 20 日均值。

    趋势偏强时放量为正贡献；趋势偏弱时放量为负贡献（放量下跌）。
    """
    ratio = trend.get("amount_ratio")
    if ratio is None:
        ratio = trend.get("vol_ratio")
    if ratio is None:
        return 50.0
    ratio = float(ratio)
    trend_score = score_trend(trend)
    delta = (ratio - 1.0) * 60.0 if trend_score >= 50.0 else (1.0 - ratio) * 60.0
    return round(_clamp(50.0 + delta, 0.0, 100.0), 1)


# ---------------------------------------------------------------------------
# 情绪
# ---------------------------------------------------------------------------
def emotion_from_activity(activity: dict[str, Any] | None) -> dict[str, Any]:
    """把乐咕涨跌家数转换成契约 ``emotion`` 字段（缺数据按中性兜底）。"""
    if not activity:
        return dict(NEUTRAL_EMOTION)
    payload = {
        "up": int(_num(activity.get("up"), 0) or 0),
        "down": int(_num(activity.get("down"), 0) or 0),
        "flat": int(_num(activity.get("flat"), 0) or 0),
        "suspended": int(_num(activity.get("suspended"), 0) or 0),
        "limit_up": int(_num(activity.get("limit_up"), 0) or 0),
        "limit_down": int(_num(activity.get("limit_down"), 0) or 0),
        "active_pct": float(_num(activity.get("active_pct"), 0.0) or 0.0),
        "asof": str(activity.get("asof", "")),
    }
    if payload["up"] + payload["down"] <= 0:
        return dict(NEUTRAL_EMOTION)
    sentiment = compute_sentiment(payload)
    return {
        "score": _round(sentiment["score"], 1),
        "label": sentiment["label"],
        "state": sentiment.get("state", "neutral"),
        "breadth": _round(sentiment["breadth"], 4),
        "limit_up": payload["limit_up"],
        "limit_down": payload["limit_down"],
        "limit_ratio": _round(sentiment["limit_ratio"], 4),
        "active_pct": _round(sentiment["active_pct"], 2),
    }


def _is_ice_market(emotion: dict[str, Any]) -> bool:
    """冰点守卫：极低宽度 + 跌停显著多于涨停（含情绪分极低）。"""
    score = float(emotion.get("score") or 50.0)
    breadth = float(emotion.get("breadth") or 0.5)
    limit_up = float(emotion.get("limit_up") or 0)
    limit_down = float(emotion.get("limit_down") or 0)
    if emotion.get("label") == "冰点" or emotion.get("state") == "ice":
        return True
    if score < 20.0 and breadth < 0.15:
        return True
    return breadth < 0.20 and limit_down > 1.5 * limit_up and score < 30.0


# ---------------------------------------------------------------------------
# 大盘状态机
# ---------------------------------------------------------------------------
def _state_for(score: float, trend: dict[str, Any], emotion: dict[str, Any]) -> str:
    if _is_ice_market(emotion):
        return "ice_watch"
    if score >= 70.0 and trend.get("ma_state") == "多头排列" and float(emotion.get("score") or 0) >= 55.0:
        return "offense"
    if score >= 55.0:
        return "lean_bull"
    if score >= 42.0:
        return "range"
    return "lean_bear"


def _fmt_num(value: Any, digits: int = 1) -> str:
    num = _num(value)
    return "N/A" if num is None else f"{num:.{digits}f}"


def _ma_reason(trend: dict[str, Any]) -> str:
    ma5, ma20, ma60 = trend.get("ma5"), trend.get("ma20"), trend.get("ma60")
    name = trend.get("index") or INDEX_NAME
    close = trend.get("close")
    if ma5 is None or ma20 is None or ma60 is None:
        return f"{name} 均线数据不足（历史不足 60 日），暂按中性趋势处理"
    state = trend.get("ma_state")
    if state == "多头排列":
        text = f"{name} 均线多头排列（MA5 {_fmt_num(ma5)} > MA20 {_fmt_num(ma20)} > MA60 {_fmt_num(ma60)}）"
    elif state == "空头排列":
        text = f"{name} 均线空头排列（MA5 {_fmt_num(ma5)} < MA20 {_fmt_num(ma20)} < MA60 {_fmt_num(ma60)}）"
    else:
        text = f"{name} 均线交织（MA5 {_fmt_num(ma5)} / MA20 {_fmt_num(ma20)} / MA60 {_fmt_num(ma60)}），方向未明"
    if close is not None and ma20 > 0:
        text += f"，收盘较 MA20 {((close / ma20) - 1.0) * 100.0:+.2f}%"
    return text


def _momentum_reason(trend: dict[str, Any]) -> str:
    ret5, ret20 = trend.get("ret5"), trend.get("ret20")
    if ret5 is None and ret20 is None:
        return "指数短期动量数据不足"
    parts = []
    if ret5 is not None:
        parts.append(f"近5日 {float(ret5):+.2f}%")
    if ret20 is not None:
        parts.append(f"近20日 {float(ret20):+.2f}%")
    anchor = ret5 if ret5 is not None else ret20
    tone = "动量偏强" if (anchor or 0) > 0 else "动量偏弱"
    return "，".join(parts) + f"（{tone}）"


def _emotion_reason(emotion: dict[str, Any]) -> str:
    score = float(emotion.get("score") or 0)
    breadth = float(emotion.get("breadth") or 0)
    return (
        f"情绪分 {score:.1f}（{emotion.get('label', '中性')}），"
        f"涨跌家数宽度 {breadth:.1%}，涨停 {int(_num(emotion.get('limit_up'), 0) or 0)} 家 / "
        f"跌停 {int(_num(emotion.get('limit_down'), 0) or 0)} 家"
    )


def _volume_reason(trend: dict[str, Any]) -> str:
    ratio = trend.get("amount_ratio")
    if ratio is not None:
        base = f"成交额 {_fmt_num(trend.get('amount'), 0)} 亿元"
        unit = "成交额"
    else:
        ratio = trend.get("vol_ratio")
        base = "成交量"
        unit = "成交量"
    if ratio is None:
        return "量能数据不足（未取得成交额）"
    ratio = float(ratio)
    tone = "放量" if ratio > 1.05 else ("缩量" if ratio < 0.95 else "平量")
    return f"{base}较 20 日均值 {ratio:.2f} 倍（{tone}，{unit}口径）"


_STATE_ACTION: dict[str, str] = {
    "offense": "趋势与情绪共振，可积极参与",
    "lean_bull": "偏多结构，跟随主线、控制追高",
    "range": "震荡格局，低吸为主、不宜趋势加仓",
    "lean_bear": "趋势与情绪偏弱，防守为主",
    "ice_watch": "冰点区左侧观察，等宽度回升再小仓位试错",
}


def decide_market_state(trend: dict[str, Any], emotion: dict[str, Any]) -> dict[str, Any]:
    """大盘状态机：输出 ``state / label / score / reasons``（3-5 条理由）。"""
    trend_score = score_trend(trend)
    volume_score = score_volume(trend)
    emotion_score = float(emotion.get("score") or 50.0)
    score = round(
        _clamp(0.45 * trend_score + 0.40 * emotion_score + 0.15 * volume_score, 0.0, 100.0), 1
    )
    state = _state_for(score, trend, emotion)

    reasons = [
        _ma_reason(trend),
        _momentum_reason(trend),
        _emotion_reason(emotion),
        _volume_reason(trend),
        f"综合状态分 {score:.1f} → {STATE_LABELS[state]}（{_STATE_ACTION[state]}）",
    ]
    if state == "ice_watch":
        reasons.insert(
            4,
            "冰点特征：宽度极低且跌停家数明显多于涨停，历史上常伴随短线修复窗口",
        )
    if len(reasons) > 5:
        # 去掉量能解释（索引 3），保留首尾，保证 3-5 条
        reasons = reasons[:3] + reasons[4:]
    return {
        "state": state,
        "label": STATE_LABELS[state],
        "score": score,
        "reasons": reasons,
        "_trend_score": trend_score,
        "_emotion_score": emotion_score,
        "_volume_score": volume_score,
    }


# ---------------------------------------------------------------------------
# 主线板块排序
# ---------------------------------------------------------------------------
def _percentiles(values: list[float | None]) -> list[float | None]:
    """百分位（越大越好）：排名第 1 → 100，最后 → 0；缺失保持 None；并列取平均名次。"""
    valid = [(i, float(v)) for i, v in enumerate(values) if v is not None]
    out: list[float | None] = [None] * len(values)
    n = len(valid)
    if n == 0:
        return out
    if n == 1:
        out[valid[0][0]] = 100.0
        return out
    ordered = sorted(valid, key=lambda item: item[1], reverse=True)
    ranks: dict[int, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[ordered[k][0]] = avg_rank
        i = j + 1
    for idx, _ in valid:
        out[idx] = round((n - ranks[idx]) / (n - 1) * 100.0, 2)
    return out


def _keep_concept(name: str) -> bool:
    if name in CONCEPT_NAME_BLACKLIST:
        return False
    return not any(name.startswith(prefix) for prefix in CONCEPT_PREFIX_BLACKLIST)


def rank_leaders(
    boards: list[dict[str, Any]],
    top_n: int = LEADER_TOP_N,
    days_map: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """板块综合分排序，取 Top N 主线。

    综合分 = 当日涨幅百分位 · 0.34 + 5 日涨幅百分位 · 0.33 + 净流入百分位 · 0.33
    （某板块缺某项数据时按剩余权重归一化，仍然可参与排序）。
    标签：当日涨幅百分位 ≥ 80 记「领涨」，否则记「活跃」。
    """
    rows = []
    for board in boards or []:
        name = str(board.get("name") or "").strip()
        if not name:
            continue
        kind = str(board.get("kind") or "industry")
        if kind == "concept" and not _keep_concept(name):
            continue
        pct_1d = _num(board.get("pct_1d"))
        pct_5d = _num(board.get("pct_5d"))
        net_inflow = _num(board.get("net_inflow"))
        if pct_1d is None and pct_5d is None and net_inflow is None:
            continue
        rows.append(
            {
                "name": name,
                "kind": kind,
                "pct_1d": pct_1d,
                "pct_5d": pct_5d,
                "net_inflow": net_inflow,
                "net_inflow_5d": _num(board.get("net_inflow_5d")),
            }
        )
    if not rows:
        return []

    p1 = _percentiles([r["pct_1d"] for r in rows])
    p5 = _percentiles([r["pct_5d"] for r in rows])
    pf = _percentiles([r["net_inflow"] for r in rows])

    for idx, row in enumerate(rows):
        components = {"pct_1d": p1[idx], "pct_5d": p5[idx], "net_inflow": pf[idx]}
        numerator = 0.0
        denominator = 0.0
        for key, pctile in components.items():
            if pctile is None:
                continue
            weight = LEADER_WEIGHTS[key]
            numerator += weight * pctile
            denominator += weight
        row["score"] = round(numerator / denominator, 1) if denominator > 0 else 0.0
        row["tag"] = "领涨" if (p1[idx] is not None and p1[idx] >= 80.0) else "活跃"
        row["days"] = int((days_map or {}).get(f"{row['kind']}:{row['name']}", 0))

    rows.sort(
        key=lambda r: (
            -r["score"],
            -(r["pct_1d"] if r["pct_1d"] is not None else -999.0),
            r["name"],
        )
    )
    leaders = []
    for row in rows[: max(0, top_n)]:
        leaders.append(
            {
                "name": row["name"],
                "kind": row["kind"],
                "pct_1d": _round(row["pct_1d"]),
                "pct_5d": _round(row["pct_5d"]),
                "net_inflow": _round(row["net_inflow"]),
                "net_inflow_5d": _round(row["net_inflow_5d"]),
                "score": row["score"],
                "days": row["days"],
                "tag": row["tag"],
            }
        )
    return leaders


def consecutive_up_days(history: pd.DataFrame | None, max_days: int = 20) -> int:
    """连续上涨天数：从最新交易日往前数，收盘价连续高于前一日即计入。"""
    closes = normalize_board_history(history)
    if closes is None or len(closes) < 2:
        return 0
    pct = closes["close"].astype(float).pct_change()
    days = 0
    for value in reversed(pct.tolist()):
        if value is not None and not math.isnan(value) and value > 0:
            days += 1
            if days >= max_days:
                break
        else:
            break
    return days


def normalize_board_history(history: pd.DataFrame | None) -> pd.DataFrame | None:
    """统一板块历史为 ``date / close``；兼容同花顺（收盘价）与东财（收盘）列名。"""
    if history is None or len(history) == 0:
        return None
    df = pd.DataFrame(history).copy()
    date_col = _column(df, ("日期", "date"))
    close_col = _column(df, ("收盘价", "收盘", "close"))
    if date_col is None or close_col is None:
        return None
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d"),
            "close": pd.to_numeric(df[close_col], errors="coerce"),
        }
    ).dropna(subset=["date", "close"])
    if out.empty:
        return None
    return out.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 仓位建议
# ---------------------------------------------------------------------------
def position_advice(market_state: dict[str, Any], emotion: dict[str, Any]) -> dict[str, Any]:
    """状态 → 建议仓位区间 + 一句话理由 + 触发条件 + 免责声明。"""
    state = str(market_state.get("state") or "range")
    if state not in STATE_POSITION:
        state = "range"
    range_text, label, base, triggers = STATE_POSITION[state]
    score = float(market_state.get("score") or 0.0)
    emotion_score = float(emotion.get("score") or 50.0)
    breadth = float(emotion.get("breadth") or 0.5)
    rationale = (
        f"{base}（状态分 {score:.1f}，情绪分 {emotion_score:.1f}/"
        f"{emotion.get('label', '中性')}，涨跌宽度 {breadth:.1%}）"
    )
    return {
        "range": range_text,
        "label": label,
        "rationale": rationale,
        "triggers": list(triggers),
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# 取数：指数 / 情绪 / 板块
# ---------------------------------------------------------------------------
def fetch_index_daily_tx(symbol: str = INDEX_CODE, lookback_days: int = 400) -> pd.DataFrame:
    """腾讯指数日线（含成交额），列 date/open/close/high/low/volume/turnover/amount。"""
    import akshare as ak

    end = datetime.now(TZ) + timedelta(days=1)
    start = end - timedelta(days=lookback_days)
    return _call_with_retry(
        lambda: ak.index_zh_daily_tx(
            symbol=symbol,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        ),
        label="index_zh_daily_tx",
    )


def _normalize_ths_boards(
    now_df: pd.DataFrame | None,
    stage_df: pd.DataFrame | None,
    kind: str,
) -> list[dict[str, Any]]:
    """同花顺板块资金流 ``即时`` + ``5日排行`` → 统一板块行。"""
    if now_df is None or len(now_df) == 0:
        return []
    stage_map: dict[str, dict[str, float | None]] = {}
    if stage_df is not None and len(stage_df) > 0:
        for _, row in stage_df.iterrows():
            name = str(row.get("行业", "")).strip()
            if not name:
                continue
            stage_map[name] = {
                "pct_5d": _num(row.get("阶段涨跌幅")),
                "net_inflow_5d": _num(row.get("净额")),
            }
    boards: list[dict[str, Any]] = []
    for _, row in now_df.iterrows():
        name = str(row.get("行业", "")).strip()
        if not name or (kind == "concept" and not _keep_concept(name)):
            continue
        stage = stage_map.get(name, {})
        boards.append(
            {
                "name": name,
                "kind": kind,
                "pct_1d": _num(row.get("行业-涨跌幅")),
                "pct_5d": stage.get("pct_5d"),
                "net_inflow": _num(row.get("净额")),
                "net_inflow_5d": stage.get("net_inflow_5d"),
                "companies": _num(row.get("公司家数")),
                "lead_stock": str(row.get("领涨股", "") or ""),
            }
        )
    return boards


def fetch_boards_ths() -> tuple[list[dict[str, Any]], list[str]]:
    """同花顺板块行情（首选）：行业+概念，各取「即时」「5日排行」，共 4 次请求。"""
    import akshare as ak

    warnings: list[str] = []

    def _get(label: str, fn: Callable[[], pd.DataFrame]) -> pd.DataFrame | None:
        try:
            frame = _call_with_retry(fn, label=label)
            return frame if frame is not None and len(frame) > 0 else None
        except Exception as exc:  # noqa: BLE001 - 单接口失败降级
            warnings.append(f"ths:{label}: {type(exc).__name__}")
            return None

    industry_now = _get(
        "industry_now", lambda: ak.stock_fund_flow_industry_ths(symbol="即时")
    )
    time.sleep(SECTOR_SLEEP)
    industry_stage = _get(
        "industry_5d", lambda: ak.stock_fund_flow_industry_ths(symbol="5日排行")
    )
    time.sleep(SECTOR_SLEEP)
    concept_now = _get("concept_now", lambda: ak.stock_fund_flow_concept_ths(symbol="即时"))
    time.sleep(SECTOR_SLEEP)
    concept_stage = _get(
        "concept_5d", lambda: ak.stock_fund_flow_concept_ths(symbol="5日排行")
    )

    boards = _normalize_ths_boards(industry_now, industry_stage, "industry")
    boards += _normalize_ths_boards(concept_now, concept_stage, "concept")
    if not boards:
        raise RuntimeError("同花顺板块接口全部失败")
    return boards, warnings


def _normalize_em_boards(
    name_df: pd.DataFrame | None,
    rank_df: pd.DataFrame | None,
    kind: str,
    indicator: str = "今日",
) -> list[dict[str, Any]]:
    """东财板块行情（兜底）：板块列表给当日涨幅，资金流排行给净流入/5 日涨幅。

    东财没有「全板块 5 日涨幅」的批量接口，因此兜底路径的 ``pct_5d`` 需靠板块历史
    补全（主线候选逐个拉取）；``indicator="5日"`` 时资金流排行本身带 5 日涨幅。
    """
    if name_df is None or len(name_df) == 0:
        return []
    rank_map: dict[str, dict[str, float | None]] = {}
    if rank_df is not None and len(rank_df) > 0:
        name_key = _column(rank_df, ("名称",)) or "名称"
        pct_candidates = ("5日涨跌幅", "阶段涨跌幅") if indicator == "5日" else ("今日涨跌幅", "涨跌幅")
        pct_key = _column(rank_df, pct_candidates)
        flow_candidates = (
            ("5日主力净流入-净额", "主力净流入-净额")
            if indicator == "5日"
            else ("今日主力净流入-净额", "主力净流入-净额")
        )
        flow_key = _column(rank_df, flow_candidates)
        for _, row in rank_df.iterrows():
            name = str(row.get(name_key, "")).strip()
            if not name:
                continue
            flow = _num(row.get(flow_key)) if flow_key else None
            # 东财净额单位为元 → 统一亿元
            if flow is not None and abs(flow) > 1e5:
                flow = flow / 1e8
            rank_map[name] = {
                "pct_5d": _num(row.get(pct_key)) if pct_key else None,
                "net": flow,
            }

    name_key = _column(name_df, ("板块名称", "名称")) or "板块名称"
    pct_key = _column(name_df, ("涨跌幅", "当日涨跌幅"))
    boards: list[dict[str, Any]] = []
    for _, row in name_df.iterrows():
        name = str(row.get(name_key, "")).strip()
        if not name or (kind == "concept" and not _keep_concept(name)):
            continue
        extra = rank_map.get(name, {})
        boards.append(
            {
                "name": name,
                "kind": kind,
                "pct_1d": _num(row.get(pct_key)) if pct_key else None,
                "pct_5d": extra.get("pct_5d") if indicator == "5日" else None,
                "net_inflow": extra.get("net") if indicator != "5日" else None,
                "net_inflow_5d": extra.get("net") if indicator == "5日" else None,
                "companies": None,
                "lead_stock": "",
            }
        )
    return boards


def fetch_boards_em() -> tuple[list[dict[str, Any]], list[str]]:
    """东方财富板块行情（兜底）。push2 限流时不重试、不反复调用，直接由上层降级。"""
    import akshare as ak

    warnings: list[str] = []
    boards: list[dict[str, Any]] = []

    def _get(label: str, fn: Callable[[], pd.DataFrame]) -> pd.DataFrame | None:
        try:
            return _call_with_retry(fn, label=label)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"em:{label}: {type(exc).__name__}")
            if _is_rate_limit_error(exc):
                # 已限流：继续调用东财只会延长限流，立即中止整个兜底链路
                raise RuntimeError(f"eastmoney rate limited at {label}") from exc
            return None

    industry_names = _get("industry_names", lambda: ak.stock_board_industry_name_em())
    time.sleep(SECTOR_SLEEP)
    industry_rank = _get(
        "industry_rank", lambda: ak.stock_fund_flow_sector_rank_em(indicator="今日", sector_type="行业资金流")
    )
    boards += _normalize_em_boards(industry_names, industry_rank, "industry")

    time.sleep(SECTOR_SLEEP)
    concept_names = _get("concept_names", lambda: ak.stock_board_concept_name_em())
    time.sleep(SECTOR_SLEEP)
    concept_rank = _get(
        "concept_rank", lambda: ak.stock_fund_flow_sector_rank_em(indicator="今日", sector_type="概念资金流")
    )
    boards += _normalize_em_boards(concept_names, concept_rank, "concept")
    if not boards:
        raise RuntimeError("东方财富板块接口全部失败")
    warnings.append("东财兜底路径无全板块 5 日涨幅：排序仅用当日涨幅+净流入，5 日涨幅由板块历史补全")
    return boards, warnings


def _hist_window(lookback_days: int = 45) -> tuple[str, str]:
    end = datetime.now(TZ) + timedelta(days=1)
    start = end - timedelta(days=lookback_days)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def fetch_board_hist(kind: str, name: str) -> pd.DataFrame:
    """板块历史行情（用于连续强势天数）：同花顺首选，东财兜底。"""
    import akshare as ak

    start, end = _hist_window()
    errors: list[str] = []
    if kind == "concept":
        ths_fn = lambda: ak.stock_board_concept_index_ths(symbol=name, start_date=start, end_date=end)  # noqa: E731
        em_fn = lambda: ak.stock_board_concept_hist_em(symbol=name, period="daily", start_date=start, end_date=end, adjust="")  # noqa: E731
    else:
        ths_fn = lambda: ak.stock_board_industry_index_ths(symbol=name, start_date=start, end_date=end)  # noqa: E731
        em_fn = lambda: ak.stock_board_industry_hist_em(symbol=name, period="日k", start_date=start, end_date=end, adjust="")  # noqa: E731
    for source, fn in (("ths", ths_fn), ("em", em_fn)):
        try:
            frame = _call_with_retry(fn, label=f"board_hist:{source}:{name}")
            normalized = normalize_board_history(frame)
            if normalized is not None and len(normalized) >= 2:
                return normalized
            errors.append(f"{source}:empty")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source}:{type(exc).__name__}")
            if _is_rate_limit_error(exc):
                break
        time.sleep(SECTOR_SLEEP)
    raise RuntimeError(f"board_hist:{kind}:{name} 失败（{', '.join(errors)}）")


def collect_market_data(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """拉取指数趋势 / 情绪 / 板块主线原始数据，单接口失败记错误并降级。"""
    errors: list[str] = []
    warnings: list[str] = []
    sources: dict[str, str] = {}

    index_daily: pd.DataFrame | None = None
    try:
        index_daily = fetch_index_daily_tx()
        sources["index"] = "tencent(index_zh_daily_tx)"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"index_tx: {type(exc).__name__}")
        try:
            index_daily = fetch_index_daily(INDEX_CODE)
            sources["index"] = "sina(index_zh_daily_sina)"
            warnings.append("腾讯指数接口不可用：已降级为新浪口径（无成交额，量能改用成交量）")
        except Exception as exc2:  # noqa: BLE001
            errors.append(f"index_sina: {type(exc2).__name__}")

    time.sleep(SECTOR_SLEEP)
    activity: dict[str, Any] | None = None
    try:
        activity = fetch_market_activity()
        sources["emotion"] = "legulegu(stock_market_activity_legu)"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"activity: {type(exc).__name__}")

    time.sleep(SECTOR_SLEEP)
    boards: list[dict[str, Any]] = []
    try:
        boards, ths_warnings = fetch_boards_ths()
        warnings.extend(ths_warnings)
        sources["sectors"] = "ths(stock_fund_flow_industry_ths/stock_fund_flow_concept_ths)"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"sectors_ths: {type(exc).__name__}")
        try:
            boards, em_warnings = fetch_boards_em()
            warnings.extend(em_warnings)
            sources["sectors"] = "eastmoney(stock_board_*_name_em/stock_fund_flow_sector_rank_em)"
        except Exception as exc2:  # noqa: BLE001
            errors.append(f"sectors_em: {type(exc2).__name__}")

    return {
        "index_daily": index_daily,
        "activity": activity,
        "boards": boards,
        "hist_fetcher": fetch_board_hist,
        "errors": errors,
        "warnings": warnings,
        "sources": sources,
    }


# ---------------------------------------------------------------------------
# 快照组装（对外唯一入口）
# ---------------------------------------------------------------------------
def build_regime_snapshot(
    cfg: dict[str, Any] | None = None, *, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """生成市场状态快照（契约 §15）。

    Args:
        cfg: 配置（当前仅保留入口，便于后续扩展）。
        data: 离线注入用的原始数据；为 ``None`` 时走真实取数。
            可含 ``index_daily / activity / boards / hist_fetcher / errors / warnings / sources``。
            测试全部通过该参数注入合成数据，**不联网**。

    Returns:
        可直接 ``json.dump`` 的 dict；``status`` 为 ``ok`` 或 ``partial``，
        仓位建议必带 ``disclaimer``（研究用途，不构成投资建议）。
    """
    collected = data if data is not None else collect_market_data(cfg)
    errors = [str(item) for item in (collected.get("errors") or [])]
    warnings = [str(item) for item in (collected.get("warnings") or [])]
    sources = dict(collected.get("sources") or {})

    daily = collected.get("index_daily")
    activity = collected.get("activity")
    boards = collected.get("boards") or []
    hist_fetcher = collected.get("hist_fetcher")

    trend = analyze_trend(daily)
    normalized_daily = normalize_index_daily(daily)
    if daily is None:
        warnings.append("指数数据缺失：趋势按中性兜底，状态判断置信度下降")
    elif normalized_daily is not None and len(normalized_daily) < 60:
        warnings.append("指数历史不足 60 日：MA60 缺失，趋势判断置信度下降")
    if activity is None:
        if not any(str(item).startswith("activity") for item in errors):
            errors.append("activity: missing")
        warnings.append("情绪数据缺失：情绪分按中性 50 兜底")

    emotion = emotion_from_activity(activity)

    market_state = decide_market_state(trend, emotion)
    leaders = rank_leaders(boards)
    if leaders and hist_fetcher is not None:
        for leader in leaders:
            try:
                history = normalize_board_history(hist_fetcher(leader["kind"], leader["name"]))
                leader["days"] = consecutive_up_days(history)
                if leader.get("pct_5d") is None and history is not None and len(history) >= 6:
                    # 主接口缺 5 日涨幅（如东财兜底）时，用板块历史收盘价补全展示值
                    closes = history["close"].astype(float)
                    leader["pct_5d"] = _round((closes.iloc[-1] / closes.iloc[-6] - 1.0) * 100.0)
            except Exception as exc:  # noqa: BLE001 - 单板块历史失败不影响主线
                leader["days"] = 0
                warnings.append(f"board_hist:{leader['kind']}:{leader['name']}: {type(exc).__name__}")
            time.sleep(HIST_SLEEP)
    if not leaders:
        warnings.append("板块行情缺失：无法识别行情主线，建议仅参考大盘状态")

    position = position_advice(market_state, emotion)

    normalized_daily = normalize_index_daily(daily)
    core_ok = (
        normalized_daily is not None
        and len(normalized_daily) >= 60
        and emotion.get("score") is not None
        and len(leaders) > 0
    )
    status = "ok" if core_ok else "partial"
    trade_date = trend.get("trade_date")
    if trade_date is None and activity:
        trade_date = str(activity.get("asof", ""))[:10] or None

    snapshot: dict[str, Any] = {
        "schema": 1,
        "asof": _now(),
        "trade_date": trade_date,
        "status": status,
        "market_state": {
            "state": market_state["state"],
            "label": market_state["label"],
            "score": market_state["score"],
            "reasons": market_state["reasons"],
        },
        "trend": trend,
        "emotion": {
            "score": emotion.get("score"),
            "label": emotion.get("label"),
            "state": emotion.get("state"),
            "breadth": emotion.get("breadth"),
            "limit_up": int(_num(emotion.get("limit_up"), 0) or 0),
            "limit_down": int(_num(emotion.get("limit_down"), 0) or 0),
            "limit_ratio": emotion.get("limit_ratio"),
            "active_pct": emotion.get("active_pct"),
        },
        "leaders": leaders,
        "position": position,
        "data_quality": {
            "status": status,
            "errors": errors,
            "warnings": warnings,
            "sources": sources,
        },
        "disclaimer": DISCLAIMER,
    }
    # 状态分内部拆解（便于前端/排查，不改变契约必填字段）
    snapshot["market_state"]["score_parts"] = {
        "trend": market_state["_trend_score"],
        "emotion": market_state["_emotion_score"],
        "volume": market_state["_volume_score"],
    }
    return snapshot


__all__ = [
    "DISCLAIMER",
    "INDEX_CODE",
    "INDEX_NAME",
    "LEADER_TOP_N",
    "STATE_LABELS",
    "STATE_POSITION",
    "analyze_trend",
    "build_regime_snapshot",
    "collect_market_data",
    "consecutive_up_days",
    "decide_market_state",
    "emotion_from_activity",
    "fetch_board_hist",
    "fetch_boards_em",
    "fetch_boards_ths",
    "fetch_index_daily_tx",
    "normalize_board_history",
    "normalize_index_daily",
    "position_advice",
    "rank_leaders",
    "score_trend",
    "score_volume",
]
