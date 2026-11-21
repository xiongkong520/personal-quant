"""市场整体：指数行情、涨跌家数、情绪分、冰点判定、指数方向（契约 §13）。

数据源（AKShare 公开接口，全部离线可复现测试用 fixture）：
- 指数实时/日线：``index_zh_spot_sina`` / ``index_zh_daily_sina``（新浪，避开东财 push2 限流）
- 涨跌家数/涨停跌停/活跃度：``stock_market_activity_legu``（乐咕）
本模块只做取数 + 指标计算，不写库；落库/推送由 CLI 与 export_cloud 负责。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

TZ = timezone(timedelta(hours=8))

# 主要指数（新浪代码 → 中文名）
MAJOR_INDICES: list[tuple[str, str]] = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000300", "沪深300"),
]

# 情绪状态阈值（0-100）
SENTIMENT_STATES = [
    (20.0, "ice", "冰点"),
    (40.0, "cold", "偏冷"),
    (60.0, "neutral", "中性"),
    (80.0, "warm", "偏热"),
    (100.0, "hot", "过热"),
]


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _retry(fn, attempts: int = 2, wait: float = 1.0):
    """轻量重试：东财/新浪偶发限流，失败重试一次即止（避免加重限流）。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 数据源异常统一兜底
            last = exc
            if i + 1 < attempts:
                time.sleep(wait)
    raise last  # type: ignore[misc]


def fetch_market_activity() -> dict[str, Any]:
    """涨跌家数 / 涨停跌停 / 活跃度（乐咕接口，稳定）。"""
    import akshare as ak

    df = _retry(ak.stock_market_activity_legu)
    m = {str(row["item"]).strip(): row["value"] for _, row in df.iterrows()}

    def _num(key: str) -> float:
        try:
            return float(m.get(key, 0))
        except (TypeError, ValueError):
            return 0.0

    activity_raw = str(m.get("活跃度", "0%")).replace("%", "").strip()
    try:
        activity = float(activity_raw)
    except ValueError:
        activity = 0.0
    return {
        "up": int(_num("上涨")),
        "down": int(_num("下跌")),
        "flat": int(_num("平盘")),
        "suspended": int(_num("停牌")),
        "limit_up": int(_num("涨停")),
        "limit_down": int(_num("跌停")),
        "active_pct": round(activity, 2),
        "asof": str(m.get("统计日期", "")),
    }


def fetch_index_spot() -> pd.DataFrame:
    """全部指数实时快照（新浪，562 个）。"""
    import akshare as ak

    return _retry(ak.index_zh_spot_sina)


def fetch_index_daily(symbol: str) -> pd.DataFrame:
    """指数日线（新浪，长历史）。返回列 date/open/high/low/close/volume，date 为 YYYY-MM-DD。"""
    import akshare as ak

    df = _retry(lambda: ak.index_zh_daily_sina(symbol=symbol))
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------- 情绪分 ----


def compute_sentiment(activity: dict[str, Any]) -> dict[str, Any]:
    """0-100 情绪分 + 涨跌家数宽度 + 涨停跌停比 + 冰点/过热状态。

    权重：涨跌家数宽度 50%、涨停跌停比 30%、活跃度 20%（活跃度 15% 记满分）。
    """
    up, down = activity["up"], activity["down"]
    total = max(1, up + down)
    breadth = up / total
    lu, ld = activity["limit_up"], activity["limit_down"]
    limit_ratio = lu / max(1, lu + ld)
    activity_score = min(1.0, activity["active_pct"] / 15.0)
    score = 100.0 * (0.5 * breadth + 0.3 * limit_ratio + 0.2 * activity_score)
    score = round(max(0.0, min(100.0, score)), 1)

    state = "neutral"
    label = "中性"
    for threshold, st, lb in SENTIMENT_STATES:
        if score <= threshold:
            state, label = st, lb
            break
    # 冰点强判定：宽度极低且跌停显著多于涨停
    if state != "ice" and breadth < 0.15 and ld > lu:
        state, label = "ice", "冰点"
    return {
        "score": score,
        "state": state,
        "label": label,
        "breadth": round(breadth, 4),
        "limit_ratio": round(limit_ratio, 4),
        "active_pct": activity["active_pct"],
    }


def _ma(close: pd.Series, window: int) -> float | None:
    if len(close) < window:
        return None
    return float(close.rolling(window).mean().iloc[-1])


def index_direction(daily: pd.DataFrame, sentiment_score: float) -> dict[str, Any]:
    """指数方向（规则打分 + softmax 概率）：均线排列、5 日动量、市场情绪。"""
    close = daily["close"].astype(float)
    if len(close) < 65:
        return {"direction": "flat", "prob": {"up": 0.33, "flat": 0.34, "down": 0.33}, "confidence": 0.34}
    ma5, ma20, ma60 = _ma(close, 5), _ma(close, 20), _ma(close, 60)
    ret5 = float(close.iloc[-1] / close.iloc[-6] - 1.0)
    score = 0.0
    if ma5 is not None and ma20 is not None and ma60 is not None:
        if ma5 > ma20 > ma60:
            score += 1.0
        elif ma5 < ma20 < ma60:
            score -= 1.0
    if ret5 > 0.01:
        score += 0.8
    elif ret5 < -0.01:
        score -= 0.8
    score += (sentiment_score - 50.0) / 50.0 * 0.6
    z = np.array([score, 0.0, -score], dtype=float)
    exp = np.exp(z - z.max())
    prob = exp / exp.sum()
    p_up, p_flat, p_down = (float(x) for x in prob)
    order = ["up", "flat", "down"]
    values = [p_up, p_flat, p_down]
    idx = int(np.argmax(values))
    return {
        "direction": order[idx],
        "prob": {"up": round(p_up, 4), "flat": round(p_flat, 4), "down": round(p_down, 4)},
        "confidence": round(max(values), 4),
        "ma_state": "多头排列" if score > 0.8 else ("空头排列" if score < -0.8 else "震荡"),
    }


# ---------------------------------------------------------------- 快照 ----


def build_market_snapshot(cfg: dict | None = None) -> dict[str, Any]:
    """生成市场整体快照（契约 §13）。单接口失败降级为 partial，不整体失败。"""
    errors: list[str] = []
    activity: dict[str, Any] | None = None
    spot: pd.DataFrame | None = None

    try:
        activity = fetch_market_activity()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"activity: {type(exc).__name__}")
    time.sleep(0.5)
    try:
        spot = fetch_index_spot()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"index_spot: {type(exc).__name__}")

    sentiment = compute_sentiment(activity) if activity else {
        "score": 50.0, "state": "neutral", "label": "中性",
        "breadth": 0.5, "limit_ratio": 0.5, "active_pct": 0.0,
    }

    indices: list[dict[str, Any]] = []
    if spot is not None:
        by_code = {str(r["代码"]): r for _, r in spot.iterrows()}
        for code, name in MAJOR_INDICES:
            row = by_code.get(code)
            if row is None:
                continue
            try:
                price = float(row["最新价"])
                pct = float(row["涨跌幅"])
            except (TypeError, ValueError):
                continue
            try:
                daily = fetch_index_daily(code)
                d = index_direction(daily, sentiment["score"])
                time.sleep(0.3)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"index_daily:{code}: {type(exc).__name__}")
                d = {"direction": "flat", "prob": {"up": 0.33, "flat": 0.34, "down": 0.33}, "confidence": 0.34, "ma_state": "数据不足"}
            indices.append({
                "code": code,
                "name": name,
                "price": round(price, 2),
                "pct_chg": round(pct, 2),
                "direction": d["direction"],
                "prob": d["prob"],
                "confidence": d["confidence"],
                "ma_state": d["ma_state"],
            })

    # 市场整体方向 = 主要指数方向概率的均值（数据不足时中性）
    if indices:
        avg = {k: float(np.mean([i["prob"][k] for i in indices])) for k in ("up", "flat", "down")}
        order = ["up", "flat", "down"]
        values = [avg["up"], avg["flat"], avg["down"]]
        market_dir = {
            "direction": order[int(np.argmax(values))],
            "prob": {k: round(v, 4) for k, v in avg.items()},
            "confidence": round(max(values), 4),
        }
    else:
        market_dir = {"direction": "flat", "prob": {"up": 0.33, "flat": 0.34, "down": 0.33}, "confidence": 0.34}

    trade_date = str(activity.get("asof", ""))[:10] if activity else datetime.now(TZ).strftime("%Y-%m-%d")
    status = "ok" if not errors else ("partial" if (activity or indices) else "stale")
    return {
        "schema": 1,
        "asof": _now(),
        "trade_date": trade_date,
        "status": status,
        "errors": errors,
        "indices": indices,
        "activity": activity or {"up": 0, "down": 0, "flat": 0, "suspended": 0,
                                 "limit_up": 0, "limit_down": 0, "active_pct": 0.0, "asof": ""},
        "sentiment": sentiment,
        "market_direction": market_dir,
    }
