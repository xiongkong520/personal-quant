"""因子评估（P0）：IC / RankIC / ICIR / 分层回测 + 报告渲染。

方法（参照多因子标准流程）：
1. 每个交易日做截面：MAD 去极值 → z-score 标准化；
2. 计算因子值与未来收益的 Pearson IC、Spearman RankIC；
3. 汇总 IC 均值、标准差、ICIR、t 值、胜率（IC>0 比例）；
4. 按因子值分 N 组，统计各组下期平均收益与 Top-Bottom 价差（单调性）。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

MIN_CROSS_SECTION = 10  # 每日截面最少股票数（少于该值跳过当日）


def mad_zscore(series: pd.Series) -> pd.Series:
    """MAD 去极值（median ± 3*MAD 截断）后做 z-score。"""
    values = series.astype(float)
    median = values.median()
    mad = (values - median).abs().median()
    if not np.isfinite(mad) or mad == 0:
        std = values.std(ddof=0)
        if not np.isfinite(std) or std == 0:
            return values * 0.0
        return (values - values.mean()) / std
    clipped = values.clip(lower=median - 3 * mad, upper=median + 3 * mad)
    std = clipped.std(ddof=0)
    if not np.isfinite(std) or std == 0:
        return clipped * 0.0
    return (clipped - clipped.mean()) / std


def _pearson(a: pd.Series, b: pd.Series) -> float | None:
    if len(a) < 3:
        return None
    va = a.to_numpy(dtype=float)
    vb = b.to_numpy(dtype=float)
    if va.std() == 0 or vb.std() == 0:
        return None
    return float(np.corrcoef(va, vb)[0, 1])


def _spearman(a: pd.Series, b: pd.Series) -> float | None:
    if len(a) < 3:
        return None
    va = a.rank().to_numpy(dtype=float)
    vb = b.rank().to_numpy(dtype=float)
    if va.std() == 0 or vb.std() == 0:
        return None
    return float(np.corrcoef(va, vb)[0, 1])


def evaluate_factor(
    frame: pd.DataFrame,
    factor: str,
    groups: int = 5,
) -> dict[str, Any]:
    """单因子评估：IC/RankIC/ICIR/分层收益。"""
    ics: list[float] = []
    rank_ics: list[float] = []
    ls_spread: list[float] = []
    group_means: dict[int, list[float]] = {i: [] for i in range(groups)}
    n_days = 0

    for _, day in frame.groupby("trade_date", sort=True):
        clean = day.dropna(subset=[factor, "fwd_ret"])
        if len(clean) < MIN_CROSS_SECTION:
            continue
        normalized = mad_zscore(clean[factor])
        ic = _pearson(normalized, clean["fwd_ret"])
        rank_ic = _spearman(normalized, clean["fwd_ret"])
        if ic is None or rank_ic is None:
            continue
        ics.append(ic)
        rank_ics.append(rank_ic)
        n_days += 1

        # 分层：按因子值分 N 组，统计各组下期收益
        try:
            labels = pd.qcut(clean[factor].rank(method="first"), groups, labels=False, duplicates="drop")
        except ValueError:
            continue
        for gid in range(groups):
            mask = labels == gid
            if mask.any():
                group_means[gid].append(float(clean.loc[mask, "fwd_ret"].mean()))
        if 0 in group_means and (groups - 1) in group_means:
            top = group_means[groups - 1][-1]
            bottom = group_means[0][-1]
            ls_spread.append(top - bottom)

    def _stats(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"mean": 0.0, "std": 0.0, "ir": 0.0, "t": 0.0, "win_rate": 0.0, "n": 0}
        arr = np.asarray(values, dtype=float)
        std = float(arr.std(ddof=0))
        mean = float(arr.mean())
        ir = mean / std if std > 0 else 0.0
        t = ir * np.sqrt(len(arr)) if len(arr) > 0 else 0.0
        return {
            "mean": round(mean, 4),
            "std": round(std, 4),
            "ir": round(float(ir), 3),
            "t": round(float(t), 2),
            "win_rate": round(float((arr > 0).mean()), 3),
            "n": int(len(arr)),
        }

    layers = {
        f"g{gid + 1}": round(float(np.mean(vals)), 5) if vals else None
        for gid, vals in group_means.items()
    }
    spread_stats = _stats(ls_spread)
    return {
        "factor": factor,
        "days": n_days,
        "ic": _stats(ics),
        "rank_ic": _stats(rank_ics),
        "layers": layers,
        "top_bottom": spread_stats,
        "monotonic": _is_monotonic(layers),
    }


def _is_monotonic(layers: dict[str, float | None]) -> bool:
    values = [layers[k] for k in sorted(layers) if layers[k] is not None]
    if len(values) < 3:
        return False
    return all(values[i] <= values[i + 1] for i in range(len(values) - 1))


def evaluate_factors(
    factor_frame: pd.DataFrame,
    factors: list[str],
    groups: int = 5,
) -> dict[str, Any]:
    """批量评估所有因子，按 |IC| 排序。"""
    results: dict[str, Any] = {}
    for factor in factors:
        if factor not in factor_frame.columns:
            continue
        try:
            results[factor] = evaluate_factor(factor_frame, factor, groups=groups)
        except Exception as exc:  # noqa: BLE001
            results[factor] = {"factor": factor, "error": f"{type(exc).__name__}: {exc}"}
    ranked = sorted(
        [item for item in results.values() if "ic" in item],
        key=lambda item: abs(item["ic"]["mean"]),
        reverse=True,
    )
    return {
        "schema": 1,
        "horizon": 1,
        "groups": groups,
        "n_samples": int(len(factor_frame)),
        "n_dates": int(factor_frame["trade_date"].nunique()),
        "factors": results,
        "ranked": [item["factor"] for item in ranked],
        "created_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(timespec="seconds"),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 因子评估报告（IC / RankIC / 分层回测）",
        "",
        f"- 样本：{report['n_samples']} 行，{report['n_dates']} 个交易日，分 {report['groups']} 组",
        "- 方法：每日截面 MAD 去极值 + z-score；IC=Pearson(因子, T+1 收益)，RankIC=Spearman",
        "",
        "| 因子 | IC 均值 | ICIR | IC t 值 | IC 胜率 | RankIC 均值 | 单调性 | Top-Bottom |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in report["ranked"]:
        item = report["factors"][name]
        ic = item["ic"]
        ric = item["rank_ic"]
        lines.append(
            f"| {name} | {ic['mean']:.4f} | {ic['ir']:.3f} | {ic['t']:.2f} | "
            f"{ic['win_rate']:.1%} | {ric['mean']:.4f} | {'是' if item['monotonic'] else '否'} | "
            f"{item['top_bottom']['mean']:.4f} |"
        )
    lines += [
        "",
        "> 经验门槛：|IC| > 0.02、|ICIR| > 0.5 才算有参考价值；IC t 值 > 2 表示统计显著。",
        "> 历史评估不代表未来；本项目仅供研究。",
        "",
    ]
    return "\n".join(lines)
