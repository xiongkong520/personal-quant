"""特征工程测试（契约 §9 / 架构 §1：禁止未来函数）。

覆盖：
- 特征列数量 / 命名 / 输出结构；
- **打乱未来数据后，历史特征完全不变**（未来函数回归测试）；
- 标签规则（T+1、阈值、前复权口径）；
- 多股票分组隔离；
- 预热期 NaN 与末尾标签为空。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.features.builder import FEATURE_COLUMNS, LABEL_CLASSES, build_features

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "model"


def _load_synth():
    spec = importlib.util.spec_from_file_location(
        "model_synth_fixture", FIXTURE_DIR / "synth.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


SYNTH = _load_synth()


@pytest.fixture(scope="module")
def daily() -> pd.DataFrame:
    df = SYNTH.load_fixture_csv()
    return df


def _perturb_future(df: pd.DataFrame, cutoff: str, seed: int = 7) -> pd.DataFrame:
    """把 trade_date > cutoff 的行大幅扰动并打乱顺序（模拟"未来数据被改写"）。"""
    rng = np.random.default_rng(seed)
    out = df.copy()
    mask = out["trade_date"] > cutoff
    idx = out.index[mask]
    if len(idx) == 0:
        return out
    factors = rng.uniform(0.5, 2.0, size=len(idx))
    for col in ("open", "high", "low", "close", "pre_close", "adj_close"):
        out.loc[idx, col] = out.loc[idx, col].to_numpy() * factors
    out.loc[idx, "volume"] = (
        out.loc[idx, "volume"].to_numpy() * rng.uniform(0.2, 5.0, size=len(idx))
    ).astype("int64")
    out.loc[idx, "turnover_rate"] = (
        out.loc[idx, "turnover_rate"].to_numpy() * rng.uniform(0.2, 5.0, size=len(idx))
    )
    future = out.loc[idx].sample(frac=1.0, random_state=seed)
    rest = out.drop(index=idx)
    return pd.concat([rest, future]).reset_index(drop=True)


def _feature_only(df: pd.DataFrame) -> pd.DataFrame:
    return df[["symbol", "trade_date", *FEATURE_COLUMNS]].copy()


def test_feature_columns_contract(daily: pd.DataFrame) -> None:
    feats = build_features(daily, horizon_days=1, label_threshold=0.005)
    assert 20 <= len(FEATURE_COLUMNS) <= 30
    for col in ("symbol", "trade_date", "label", *FEATURE_COLUMNS):
        assert col in feats.columns
    assert len(set(FEATURE_COLUMNS)) == len(FEATURE_COLUMNS)
    assert [c for c in feats.columns if c in FEATURE_COLUMNS] == FEATURE_COLUMNS
    assert set(feats["label"].dropna().unique()) <= set(LABEL_CLASSES)
    # 特征列是数值型
    numeric = feats[FEATURE_COLUMNS].to_numpy(dtype=float)
    assert numeric.shape == (len(feats), len(FEATURE_COLUMNS))


def test_fixture_csv_matches_generator() -> None:
    """fixture CSV 必须与生成器同种子输出一致（防止 fixture 漂移）。"""
    generated = SYNTH.make_daily()
    stored = SYNTH.load_fixture_csv()
    pd.testing.assert_frame_equal(generated, stored, check_dtype=False)


def test_no_lookahead_future_perturbation(daily: pd.DataFrame) -> None:
    """核心回归测试：改写/打乱未来数据后，过去所有特征不变。"""
    dates = sorted(daily["trade_date"].unique())
    base = build_features(daily, 1, 0.005)
    for cutoff in (dates[80], dates[300], dates[800]):
        perturbed = _perturb_future(daily, cutoff)
        rebuilt = build_features(perturbed, 1, 0.005)
        left = _feature_only(base[base["trade_date"] <= cutoff])
        right = _feature_only(rebuilt[rebuilt["trade_date"] <= cutoff])
        pd.testing.assert_frame_equal(
            left.reset_index(drop=True),
            right.reset_index(drop=True),
            check_exact=False,
            rtol=1e-12,
            atol=1e-12,
        )


def test_no_lookahead_single_symbol(daily: pd.DataFrame) -> None:
    """单票路径同样无未来函数。"""
    one = daily[daily["symbol"] == "600519"].reset_index(drop=True)
    cutoff = sorted(one["trade_date"].unique())[500]
    base = build_features(one, 1, 0.005)
    rebuilt = build_features(_perturb_future(one, cutoff), 1, 0.005)
    pd.testing.assert_frame_equal(
        _feature_only(base[base["trade_date"] <= cutoff]).reset_index(drop=True),
        _feature_only(rebuilt[rebuilt["trade_date"] <= cutoff]).reset_index(drop=True),
        check_exact=False,
        rtol=1e-12,
        atol=1e-12,
    )


def test_label_rule_and_adj_close() -> None:
    """T+1 标签：|r| <= 0.5% → flat；且必须用前复权价（除权日不误判为 down）。"""
    frame = SYNTH.make_dividend_frame()
    feats = build_features(frame, horizon_days=1, label_threshold=0.005)
    labels = feats["label"].tolist()
    # 除权日（第 4 行）：不复权 -2.91%，前复权仅 +0.09% → flat
    assert labels[3] == "flat", labels
    # 前 3 日与后段均约 +1% / +2% → up
    assert labels[:3] == ["up", "up", "up"], labels
    assert labels[4:7] == ["up", "up", "up"], labels
    # 最后一行没有 T+1 → 无标签
    assert labels[-1] is None or (isinstance(labels[-1], float) and np.isnan(labels[-1]))


def test_label_threshold_boundaries() -> None:
    """阈值边界：恰好 ±0.5% 算 flat，略超算 up/down。"""
    frame = SYNTH.make_dividend_frame().iloc[:4].copy()
    # 构造 label 由第 0 行决定：r = close1/close0 - 1
    frame.loc[1, "adj_close"] = frame.loc[0, "adj_close"] * 1.005
    frame.loc[1, "close"] = frame.loc[1, "adj_close"]
    feats = build_features(frame, 1, 0.005)
    assert feats["label"].iloc[0] == "flat"
    frame.loc[1, "adj_close"] = frame.loc[0, "adj_close"] * 1.0051
    frame.loc[1, "close"] = frame.loc[1, "adj_close"]
    feats = build_features(frame, 1, 0.005)
    assert feats["label"].iloc[0] == "up"
    frame.loc[1, "adj_close"] = frame.loc[0, "adj_close"] * 0.9949
    frame.loc[1, "close"] = frame.loc[1, "adj_close"]
    feats = build_features(frame, 1, 0.005)
    assert feats["label"].iloc[0] == "down"


def test_multi_symbol_no_cross_contamination(daily: pd.DataFrame) -> None:
    """多票输入时按 symbol 分组计算，与单票单独计算完全一致。"""
    one = daily[daily["symbol"] == "000001"].reset_index(drop=True)
    combined = build_features(daily, 1, 0.005)
    single = build_features(one, 1, 0.005)
    got = combined[combined["symbol"] == "000001"][
        ["trade_date", *FEATURE_COLUMNS]
    ].reset_index(drop=True)
    want = single[["trade_date", *FEATURE_COLUMNS]].reset_index(drop=True)
    pd.testing.assert_frame_equal(got, want, check_exact=False, rtol=1e-12, atol=1e-12)


def test_warmup_and_tail(daily: pd.DataFrame) -> None:
    """预热期返回 NaN，最后 horizon 行无标签；有足够历史后特征全部有限。"""
    one = daily[daily["symbol"] == "600519"].reset_index(drop=True)
    feats = build_features(one, 1, 0.005)
    # 第一行：依赖历史窗口的特征全为 NaN（当日盘面类特征本身可用，不属于未来函数）
    same_day = {"amplitude", "gap_open", "turnover_rate"}
    first = feats[FEATURE_COLUMNS].iloc[0]
    assert first[list(set(FEATURE_COLUMNS) - same_day)].isna().all()
    assert first[list(same_day)].notna().all()
    # 60 日预热后，中间段特征应全部有限
    mid = feats.iloc[120]
    assert np.isfinite(mid[FEATURE_COLUMNS].to_numpy(dtype=float)).all()
    # 最后一行标签为空
    assert feats["label"].iloc[-1] is None or pd.isna(feats["label"].iloc[-1])


def test_unsorted_input_same_result(daily: pd.DataFrame) -> None:
    """输入乱序不影响结果（内部按 symbol/trade_date 排序）。"""
    shuffled = daily.sample(frac=1.0, random_state=11).reset_index(drop=True)
    base = build_features(daily, 1, 0.005)
    other = build_features(shuffled, 1, 0.005)
    pd.testing.assert_frame_equal(
        base.reset_index(drop=True),
        other.reset_index(drop=True),
        check_exact=False,
        rtol=1e-12,
        atol=1e-12,
    )
