"""模型加载、推理与对外信号 JSON（契约 §4 / §5 / §9）。

推理公式（与 ``model.json`` 双端推理约定严格一致，Cloudflare Worker 端必须可复现）::

    z = (x - mean) / scale
    p = softmax(z @ coef.T + intercept)     # class 顺序 = model["classes"]

只读本地 SQLite / 本地 model.json，不联网，不做实盘。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from quant.config import project_path
from quant.features.builder import FEATURE_COLUMNS, build_features
from quant.model._data_io import load_daily, resolve_model_path

TZ = timezone(timedelta(hours=8))
SIGNAL_SCHEMA = 1
DIRECTIONS = ("up", "flat", "down")

#: ``model.json`` 必须包含的字段（契约 §5）。
REQUIRED_MODEL_KEYS = (
    "schema",
    "version",
    "kind",
    "classes",
    "features",
    "mean",
    "scale",
    "coef",
    "intercept",
    "trained_at",
    "train_range",
    "horizon",
    "label_rule",
    "metrics",
)

#: 信号 JSON 必须包含的字段（契约 §4）。
REQUIRED_SIGNAL_KEYS = (
    "schema",
    "symbol",
    "name",
    "asof",
    "trade_date",
    "price",
    "direction",
    "prob",
    "confidence",
    "horizon",
    "expires_at",
    "model_version",
    "status",
    "top_features",
    "paper",
)

_SESSION_RANGES = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))
_KNOWN_NAMES = {"600519": "贵州茅台", "000001": "平安银行", "300750": "宁德时代"}


def now_tz() -> datetime:
    """当前北京时间（带 +08:00）。"""
    return datetime.now(TZ)


# ---------------------------------------------------------------------------
# 模型加载 / 校验
# ---------------------------------------------------------------------------
def validate_model_structure(model: dict) -> None:
    """校验 ``model.json`` 字段与形状（契约 §5），不合法抛 ``ValueError``。"""
    if not isinstance(model, dict):
        raise ValueError("model 必须是 dict")
    missing = [k for k in REQUIRED_MODEL_KEYS if k not in model]
    if missing:
        raise ValueError(f"model.json 缺少字段: {missing}")
    if model.get("kind") != "softmax_regression":
        raise ValueError(f"不支持的模型类型: {model.get('kind')!r}")
    classes = model["classes"]
    if not isinstance(classes, list) or sorted(map(str, classes)) != sorted(DIRECTIONS):
        raise ValueError(f"classes 必须为 {list(DIRECTIONS)} 的排列，实际 {classes!r}")
    features = model["features"]
    if not isinstance(features, list) or not features:
        raise ValueError("features 必须是非空列表")
    if len(set(map(str, features))) != len(features):
        raise ValueError("features 存在重复列名")
    n = len(features)
    mean = np.asarray(model["mean"], dtype=float)
    scale = np.asarray(model["scale"], dtype=float)
    coef = np.asarray(model["coef"], dtype=float)
    intercept = np.asarray(model["intercept"], dtype=float)
    if mean.shape != (n,) or scale.shape != (n,):
        raise ValueError(f"mean/scale 形状应为 ({n},)，实际 {mean.shape}/{scale.shape}")
    if coef.shape != (len(classes), n):
        raise ValueError(f"coef 形状应为 ({len(classes)}, {n})，实际 {coef.shape}")
    if intercept.shape != (len(classes),):
        raise ValueError(f"intercept 形状应为 ({len(classes)},)，实际 {intercept.shape}")
    if not (np.isfinite(mean).all() and np.isfinite(scale).all()):
        raise ValueError("mean/scale 含 NaN/Inf")
    if not (np.isfinite(coef).all() and np.isfinite(intercept).all()):
        raise ValueError("coef/intercept 含 NaN/Inf")


def load_model(path: str | Path) -> dict:
    """读取并校验 ``model.json``（契约 §5）。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"模型文件不存在: {path}（请先运行 python -m quant.cli train）"
        )
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型文件不是合法 JSON: {path}: {exc}") from exc
    validate_model_structure(model)
    return model


# ---------------------------------------------------------------------------
# 推理（严格按契约 §5 公式）
# ---------------------------------------------------------------------------
def _arrays(model: dict) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    features = [str(f) for f in model["features"]]
    mean = np.asarray(model["mean"], dtype=float)
    scale = np.asarray(model["scale"], dtype=float)
    scale = np.where(scale == 0.0, 1.0, scale)  # 防除零：常量特征不贡献信息
    coef = np.asarray(model["coef"], dtype=float)
    intercept = np.asarray(model["intercept"], dtype=float)
    return features, mean, scale, coef, intercept


def _standardize_row(x: dict[str, float], features, mean, scale) -> np.ndarray:
    missing = [name for name in features if name not in x]
    if missing:
        raise KeyError(f"特征向量缺少: {missing}")
    values = np.array([float(x[name]) for name in features], dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("特征向量含 NaN/Inf，无法推理")
    return (values - mean) / scale


def _softmax(scores: np.ndarray) -> np.ndarray:
    """数值稳定 softmax（减最大值不改变结果）。"""
    shifted = scores - np.max(scores, axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / np.sum(exp, axis=-1, keepdims=True)


def predict_proba(model: dict, x: dict[str, float]) -> dict[str, float]:
    """单样本推理，返回 ``{"up": p, "flat": p, "down": p}``（契约 §5 公式）。"""
    features, mean, scale, coef, intercept = _arrays(model)
    z = _standardize_row(x, features, mean, scale)
    scores = coef @ z + intercept
    probs = _softmax(scores)
    by_class = {str(c): float(p) for c, p in zip(model["classes"], probs)}
    return {d: by_class[d] for d in DIRECTIONS}


def predict_proba_batch(model: dict, X: np.ndarray) -> np.ndarray:
    """批量推理：返回 ``(n, 3)`` 概率矩阵，列顺序与 ``model["classes"]`` 一致。"""
    features, mean, scale, coef, intercept = _arrays(model)
    X = np.asarray(X, dtype=float)
    if X.ndim == 1:
        X = X.reshape(1, -1)
    if X.shape[1] != len(features):
        raise ValueError(f"特征矩阵列数 {X.shape[1]} != 模型特征数 {len(features)}")
    z = (X - mean) / scale
    scores = z @ coef.T + intercept
    return _softmax(scores)


def predict_direction(prob: dict[str, float], classes: list[str] | None = None) -> str:
    """按 ``classes`` 顺序取概率最大的方向（同概率时顺序靠前者优先，双端一致）。"""
    order = list(classes or DIRECTIONS)
    return max(order, key=lambda c: (prob[c], -order.index(c)))


# ---------------------------------------------------------------------------
# 单票推理（供回测 / 双端 parity 测试对照）
# ---------------------------------------------------------------------------
def _latest_feature_row(model: dict, daily: pd.DataFrame) -> dict[str, float] | None:
    if daily is None or len(daily) == 0:
        return None
    frame = daily.copy()
    if "symbol" in frame.columns and frame["symbol"].nunique(dropna=True) > 1:
        raise ValueError("infer_signal 只接受单只股票日线")
    features = [str(f) for f in model["features"]]
    computed = build_features(frame, horizon_days=1, label_threshold=0.005)
    if computed is None or len(computed) == 0:
        return None
    last = computed.iloc[-1]
    missing = [name for name in features if name not in computed.columns]
    if missing:
        raise KeyError(f"特征表缺少模型所需列: {missing}")
    values = {name: float(last[name]) for name in features}
    if not np.isfinite(np.array([values[n] for n in features], dtype=float)).all():
        return None  # 预热期数据不足 → warming_up
    return values


def infer_signal(model: dict, daily: pd.DataFrame) -> dict | None:
    """用最新一根日线推理；数据不足返回 ``None``（调用方据此置 warming_up）。

    返回内部结构（非契约 §4 最终形态），字段：trade_date / price / direction /
    prob / confidence / horizon / top_features / features / status。
    """
    features, mean, scale, coef, intercept = _arrays(model)
    x = _latest_feature_row(model, daily)
    if x is None:
        return None
    z = _standardize_row(x, features, mean, scale)
    scores = coef @ z + intercept
    probs = _softmax(scores)
    by_class = {str(c): float(p) for c, p in zip(model["classes"], probs)}
    prob = {d: by_class[d] for d in DIRECTIONS}
    direction = predict_direction(prob, [str(c) for c in model["classes"]])
    confidence = round(max(prob.values()), 4)

    # 线性模型贡献度 = 标准化特征 × 该类别系数（近似解释，契约 §4 top_features）
    class_idx = [str(c) for c in model["classes"]].index(direction)
    contrib = coef[class_idx] * z
    ranked = sorted(range(len(features)), key=lambda i: (-abs(float(contrib[i])), i))[:5]
    top_features = [
        {
            "name": features[i],
            "value": round(float(x[features[i]]), 6),
            "contrib": round(float(contrib[i]), 6),
        }
        for i in ranked
    ]

    close = pd.to_numeric(daily["close"], errors="coerce").dropna()
    price = float(close.iloc[-1]) if len(close) else 0.0
    last_date = str(pd.to_datetime(daily["trade_date"]).max().strftime("%Y-%m-%d"))
    return {
        "trade_date": last_date,
        "price": price,
        "direction": direction,
        "prob": prob,
        "confidence": confidence,
        "horizon": str(model.get("horizon") or "1d"),
        "top_features": top_features,
        "features": {name: float(x[name]) for name in features},
        "status": "ok",
    }


# ---------------------------------------------------------------------------
# 交易时段 / 交易日历（只读本地缓存，不联网）
# ---------------------------------------------------------------------------
def _cached_trade_days(cfg: dict) -> set[str] | None:
    """读本地交易日历缓存（``data.calendar_cache``）；不存在返回 None（退回工作日判断）。"""
    data_cfg = cfg.get("data") or {}
    rel = data_cfg.get("calendar_cache")
    if not rel:
        return None
    path = project_path(rel)
    if not path.exists():
        return None
    for kwargs in ({"dtype": str}, {}):
        try:
            df = pd.read_csv(path, **kwargs)
            if len(df) == 0:
                return None
            col = "trade_date" if "trade_date" in df.columns else df.columns[0]
            values = pd.to_datetime(df[col], errors="coerce").dropna()
            if len(values) == 0:
                continue
            return set(values.dt.strftime("%Y-%m-%d"))
        except Exception:  # pragma: no cover - 缓存损坏时退回工作日
            continue
    return None


def _is_trading_day(day: date, trade_days: set[str] | None) -> bool:
    if trade_days is not None:
        return day.strftime("%Y-%m-%d") in trade_days
    return day.weekday() < 5


def _next_trading_day(day: date, trade_days: set[str] | None, max_search: int = 20) -> date:
    candidate = day
    for _ in range(max_search):
        candidate = candidate + timedelta(days=1)
        if _is_trading_day(candidate, trade_days):
            return candidate
    # 兜底：直接取下一个工作日
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate + timedelta(days=1)
    return candidate


def _session_status(now: datetime, last_bar_date: str, trade_days: set[str] | None) -> str:
    """交易时段内且当日数据已入库 → ok；否则 stale（契约 §4 / 架构失败模式表）。"""
    if not _is_trading_day(now.date(), trade_days):
        return "stale"
    minutes = now.hour * 60 + now.minute
    in_session = any(start <= minutes <= end for start, end in _SESSION_RANGES)
    if not in_session:
        return "stale"
    if last_bar_date != now.date().strftime("%Y-%m-%d"):
        return "stale"  # 交易时段内但行情未更新到今天，避免旧数据冒充满仓信号
    return "ok"


# ---------------------------------------------------------------------------
# 对外信号（契约 §4）
# ---------------------------------------------------------------------------
def _symbol_name(symbol: str, cfg: dict) -> str:
    names = cfg.get("symbol_names") or {}
    if isinstance(names, dict) and symbol in names:
        return str(names[symbol])
    return _KNOWN_NAMES.get(symbol, symbol)


def _paper_snapshot(cfg: dict, symbol: str) -> dict:
    """模拟盘快照（由 paper 模块提供；不可用时回退到初始资金）。

    契约 §4 的 ``paper.position_qty`` 指该 ``symbol`` 的持仓数量。
    """
    try:  # pragma: no cover - 依赖 paper 模块
        from quant.paper.engine import snapshot  # type: ignore

        snap = snapshot(cfg)
        if isinstance(snap, dict):
            equity = snap.get("equity", snap.get("total_equity"))
            if equity is not None:
                positions = snap.get("positions") or []
                qty = sum(
                    int(pos.get("qty") or 0)
                    for pos in positions
                    if str(pos.get("symbol", "")).zfill(6) == symbol
                )
                return {
                    "equity": float(equity),
                    "position_qty": int(qty),
                    "today_pnl": float(snap.get("today_pnl") or 0.0),
                }
    except Exception:
        pass
    initial = float((cfg.get("paper") or {}).get("initial_cash", 0.0))
    return {"equity": initial, "position_qty": 0, "today_pnl": 0.0}


def _neutral_prob(model: dict) -> dict[str, float]:
    """数据不足时的兜底概率：特征取训练均值（标准化后为 0）。"""
    features, mean, _, coef, intercept = _arrays(model)
    scores = coef @ np.zeros(len(features)) + intercept
    probs = _softmax(scores)
    by_class = {str(c): float(p) for c, p in zip(model["classes"], probs)}
    if set(by_class) >= set(DIRECTIONS):
        return {d: by_class[d] for d in DIRECTIONS}
    return {"up": 1.0 / 3, "flat": 1.0 / 3, "down": 1.0 / 3}


def _build_signal(
    *,
    symbol: str,
    cfg: dict,
    model: dict | None,
    inferred: dict | None,
    last_trade_date: str,
    last_price: float,
    now: datetime,
    trade_days: set[str] | None,
) -> dict:
    if model is None:
        prob = {"up": 1.0 / 3, "flat": 1.0 / 3, "down": 1.0 / 3}
        direction = "flat"
        confidence = round(max(prob.values()), 4)
        status = "warming_up"
        model_version = "unavailable"
        top_features: list[dict] = []
        horizon = "1d"
    elif inferred is None:
        prob = _neutral_prob(model)
        direction = "flat"
        confidence = round(max(prob.values()), 4)
        status = "warming_up"
        model_version = str(model.get("version", "unknown"))
        top_features = []
        horizon = str(model.get("horizon") or "1d")
    else:
        prob = dict(inferred["prob"])
        direction = str(inferred["direction"])
        confidence = float(inferred["confidence"])
        status = _session_status(now, last_trade_date, trade_days)
        model_version = str(model.get("version", "unknown"))
        top_features = list(inferred["top_features"])
        horizon = str(inferred.get("horizon") or "1d")

    expiry_day = _next_trading_day(
        date.fromisoformat(last_trade_date), trade_days
    ) if last_trade_date else _next_trading_day(now.date(), trade_days)
    expires_at = datetime.combine(expiry_day, time(9, 30), tzinfo=TZ).isoformat()

    return {
        "schema": SIGNAL_SCHEMA,
        "symbol": symbol,
        "name": _symbol_name(symbol, cfg),
        "asof": now.isoformat(timespec="seconds"),
        "trade_date": last_trade_date or now.date().strftime("%Y-%m-%d"),
        "price": float(last_price),
        "direction": direction,
        "prob": prob,
        "confidence": confidence,
        "horizon": horizon,
        "expires_at": expires_at,
        "model_version": model_version,
        "status": status,
        "top_features": top_features[:5],
        "paper": _paper_snapshot(cfg, symbol),
    }


def generate_signal(symbol: str, cfg: dict) -> dict:
    """生成契约 §4 信号 JSON：数据/模型缺失 → ``status=warming_up``（不抛错）。"""
    symbol = str(symbol).strip().zfill(6)
    now = now_tz()
    trade_days = _cached_trade_days(cfg)

    model: dict | None
    try:
        model = load_model(resolve_model_path(cfg))
    except (FileNotFoundError, ValueError):
        model = None

    daily: pd.DataFrame | None
    try:
        daily = load_daily(symbol, cfg)
    except (FileNotFoundError, RuntimeError):
        daily = None

    last_trade_date = ""
    last_price = 0.0
    inferred: dict | None = None
    if daily is not None and len(daily) > 0:
        last_trade_date = str(
            pd.to_datetime(daily["trade_date"]).max().strftime("%Y-%m-%d")
        )
        close = pd.to_numeric(daily["close"], errors="coerce").dropna()
        last_price = float(close.iloc[-1]) if len(close) else 0.0
        if model is not None:
            try:
                inferred = infer_signal(model, daily)
            except (ValueError, KeyError):
                inferred = None

    return _build_signal(
        symbol=symbol,
        cfg=cfg,
        model=model,
        inferred=inferred,
        last_trade_date=last_trade_date,
        last_price=last_price,
        now=now,
        trade_days=trade_days,
    )
