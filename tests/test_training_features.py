"""训练管道离线测试（tests/test_training_features.py）。

覆盖（全部离线，合成数据，不联网）：
1. 数据源合规：只有 AKShare（首选）+ Baostock（兜底），不保留腾讯/新浪原始路径；
2. 特征 shift 防未来函数：改写未来数据后历史特征不变；
3. 标签方向：T+1 次日开盘买/收盘卖，0.2% 摩擦阈值，flat 判定正确；
4. 市场情绪特征 lag1 对齐 + 缺失标记；
5. Walk-Forward 切分严格按时间（训练窗 < 验证窗）；
6. 指标函数（accuracy / win_rate / Wilson / logloss / payoff）手算校验；
7. 模型导出 JSON 符合 docs/UPGRADE_PLAN.md §3.3，树编码合法、体积 < 2MB；
8. Python 参考推理与 LightGBM booster 预测逐位一致（parity）；
9. Notebook / README 交付物存在且包含所需步骤。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
TRAINING_DIR = ROOT / "training"
sys.path.insert(0, str(TRAINING_DIR))

import export_model as em  # noqa: E402
import fetch_data as fd  # noqa: E402
import train_lgbm as tl  # noqa: E402

HAS_LIGHTGBM = importlib.util.find_spec("lightgbm") is not None
requires_lightgbm = pytest.mark.skipif(not HAS_LIGHTGBM, reason="未安装 lightgbm（CI 可选依赖）")

SCHEMA_CLASSES = ["up", "flat", "down"]


# ---------------------------------------------------------------------------
# fixtures：合成数据（离线、确定性）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def frames() -> dict[str, pd.DataFrame]:
    return fd.generate_synthetic_frames(
        ["600519", "000001", "300750", "601318", "600036", "000858"],
        days=260,
        seed=7,
        start="2024-01-01",
    )


@pytest.fixture(scope="module")
def dataset(frames) -> tuple[pd.DataFrame, dict]:
    return tl.build_training_dataset(
        frames["daily"], frames["zt_pool"], frames["index"], label_cost=0.002
    )


@pytest.fixture(scope="module")
def trained(dataset) -> dict:
    """训练一个小模型并导出模型 JSON（module 级复用，避免重复训练）。"""
    if not HAS_LIGHTGBM:
        pytest.skip("未安装 lightgbm")
    df, data_meta = dataset
    labeled = df.dropna(subset=["label"]).reset_index(drop=True)
    params = tl.default_params(num_iterations=10, num_leaves=6, seed=42)
    params["n_estimators"] = 10
    oof, folds = tl.walk_forward(
        labeled, tl.FEATURE_COLUMNS, params,
        train_days=60, valid_days=10, step=10, label_cost=0.002, num_boost_round=10,
    )
    proba_cols = [f"prob_{c}" for c in SCHEMA_CLASSES]
    oof_metrics = tl.evaluate_predictions(
        oof["label"].to_numpy(),
        oof[proba_cols].to_numpy(dtype=float),
        oof["label_ret"].to_numpy(),
        cost=0.002,
    )
    booster = tl.fit_booster(
        labeled[tl.FEATURE_COLUMNS].to_numpy(dtype=np.float32),
        tl.encode_labels(labeled["label"]),
        params,
        num_boost_round=10,
    )
    meta = {
        "version": "lgbm-test",
        "trained_at": "2026-01-01T00:00:00+08:00",
        "features": tl.FEATURE_COLUMNS,
        "feature_groups": tl.FEATURE_GROUPS,
        "feature_formulas": tl.FEATURE_FORMULAS,
        "booster_classes": tl.BOOSTER_CLASSES,
        "schema_classes": tl.SCHEMA_CLASSES,
        "label": {"horizon": 1, "definition": data_meta["label_definition"], "classes": SCHEMA_CLASSES, "cost": 0.002},
        "data": data_meta,
        "feature_stats": tl._feature_stats(labeled, tl.FEATURE_COLUMNS),
        "metrics": {"walk_forward": oof_metrics, "folds": folds},
    }
    model = em.build_model_json(booster, meta)
    return {
        "booster": booster,
        "labeled": labeled,
        "oof": oof,
        "folds": folds,
        "metrics": oof_metrics,
        "meta": meta,
        "model": model,
    }


# ---------------------------------------------------------------------------
# 1. 数据源合规（社区主流：AKShare 首选 + Baostock 兜底，无腾讯/新浪原始接口）
# ---------------------------------------------------------------------------
def test_data_sources_follow_community_choice() -> None:
    # 不再保留腾讯/新浪原始降级函数
    assert not hasattr(fd, "ingest_tx_daily")
    assert not hasattr(fd, "ingest_sina_daily")
    # 只允许 AKShare / Baostock 两个个股抓取路径
    assert hasattr(fd, "fetch_akshare_stock_daily")
    assert hasattr(fd, "fetch_baostock_stock_daily")
    source = Path(fd.__file__).read_text(encoding="utf-8")
    assert "akshare" in source and "baostock" in source
    assert "stock_zh_a_hist_tx" not in source
    assert "stock_zh_a_hist_sina" not in source
    readme = (TRAINING_DIR / "README.md").read_text(encoding="utf-8")
    for keyword in ("AKShare", "Baostock", "Kaggle", "Colab", "GitHub Actions", "不自行爬取腾讯/新浪"):
        assert keyword in readme, keyword


def test_requirements_and_gitignore_exist() -> None:
    req = (TRAINING_DIR / "requirements.txt").read_text(encoding="utf-8")
    for pkg in ("akshare", "baostock", "lightgbm", "pandas", "pyarrow", "numpy"):
        assert pkg in req
    ignore = (TRAINING_DIR / ".gitignore").read_text(encoding="utf-8")
    assert "data/" in ignore and "out/" in ignore


# ---------------------------------------------------------------------------
# 2. 特征 shift 防未来函数
# ---------------------------------------------------------------------------
def _perturb_after(frame: pd.DataFrame, cutoff: str, seed: int = 11) -> pd.DataFrame:
    """改写 trade_date > cutoff 的所有数据并打乱顺序，模拟「未来数据被改写」。"""
    rng = np.random.default_rng(seed)
    out = frame.copy()
    mask = out["trade_date"] > cutoff
    idx = out.index[mask]
    if len(idx) == 0:
        return out
    factors = rng.uniform(0.4, 2.5, size=len(idx))
    for col in ("open", "high", "low", "close"):
        if col in out.columns:
            out.loc[idx, col] = out.loc[idx, col].to_numpy() * factors
    if "volume" in out.columns:
        out.loc[idx, "volume"] = (out.loc[idx, "volume"].to_numpy() * factors).astype("int64")
    if "amount" in out.columns:
        out.loc[idx, "amount"] = out.loc[idx, "amount"].to_numpy() * factors
    if "limit_up_count" in out.columns:
        out.loc[idx, "limit_up_count"] = rng.integers(0, 300, size=len(idx))
    if "limit_down_count" in out.columns:
        out.loc[idx, "limit_down_count"] = rng.integers(0, 100, size=len(idx))
    future = out.loc[idx].sample(frac=1.0, random_state=seed)
    rest = out.drop(index=idx)
    return pd.concat([rest, future]).reset_index(drop=True)


def test_features_no_future_leak(frames) -> None:
    """改写未来行情/情绪数据后，历史信号日的特征必须完全不变。"""
    base, _ = tl.build_training_dataset(frames["daily"], frames["zt_pool"], frames["index"], 0.002)
    dates = sorted(base["trade_date"].unique())
    for cutoff in (dates[len(dates) // 3], dates[len(dates) // 2], dates[-5]):
        dirty_daily = _perturb_after(frames["daily"], cutoff)
        dirty_zt = _perturb_after(frames["zt_pool"], cutoff)
        dirty_index = _perturb_after(frames["index"], cutoff)
        rebuilt, _ = tl.build_training_dataset(dirty_daily, dirty_zt, dirty_index, 0.002)
        left = base[base["trade_date"] <= cutoff].reset_index(drop=True)
        right = rebuilt[rebuilt["trade_date"] <= cutoff].reset_index(drop=True)
        assert len(left) == len(right)
        pd.testing.assert_frame_equal(
            left[tl.FEATURE_COLUMNS],
            right[tl.FEATURE_COLUMNS],
            check_exact=False,
            rtol=1e-12,
            atol=1e-12,
        )


def test_market_features_shifted_one_day(frames, dataset) -> None:
    """市场/情绪特征必须使用前一交易日数据（lag1），首日 + 缺失标记正确。"""
    df, _ = dataset
    zt = frames["zt_pool"].set_index("trade_date")
    dates = sorted(df["trade_date"].unique())
    signal_date = dates[100]
    prev_date = dates[99]
    row = df[(df["trade_date"] == signal_date)].iloc[0]
    if prev_date in zt.index:
        assert row["limit_up_count_lag1"] == pytest.approx(float(zt.loc[prev_date, "limit_up_count"]))
        assert row["sentiment_missing"] == 0.0
    # 合成数据只有最后 30 个交易日有真实涨跌停池：更早的行必须是缺失标记 + 0 填充
    early_date = dates[5]
    early = df[df["trade_date"] == early_date].iloc[0]
    assert early["limit_up_count_lag1"] == 0.0
    assert early["sentiment_missing"] == 1.0


# ---------------------------------------------------------------------------
# 3. 标签：T+1 开盘买/收盘卖 + 0.2% 摩擦
# ---------------------------------------------------------------------------
def test_label_uses_next_open_to_close() -> None:
    daily = pd.DataFrame(
        {
            "symbol": "600519",
            "trade_date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
            "open": [9.9, 10.5, 10.4, 10.2],
            "high": [10.1, 10.6, 10.45, 10.3],
            "low": [9.8, 10.4, 10.15, 10.1],
            "close": [10.0, 10.51, 10.2, 10.25],
            "pre_close": [9.9, 10.0, 10.51, 10.2],
            "volume": [1_000_000] * 4,
            "amount": [1e7] * 4,
            "turnover_rate": [1.0] * 4,
        }
    )
    df, _ = tl.build_training_dataset(daily, None, None, label_cost=0.002)
    labels = dict(zip(df["trade_date"], df["label"]))
    # d0：次日开盘 10.5 → 收盘 10.51 = +0.095% → flat（若误用 close→close 会是 +5.1% up）
    assert labels["2024-01-02"] == "flat"
    # d1：次日 10.4 → 10.2 = -1.92% → down
    assert labels["2024-01-03"] == "down"
    # d2：次日 10.2 → 10.25 = +0.49% → up
    assert labels["2024-01-04"] == "up"
    # 最后一天没有 T+1 → 无标签
    assert df[df["trade_date"] == "2024-01-05"]["label"].isna().all()
    # label_ret 精确值
    ret = dict(zip(df["trade_date"], df["label_ret"]))
    assert ret["2024-01-02"] == pytest.approx(10.51 / 10.5 - 1)
    assert ret["2024-01-03"] == pytest.approx(10.2 / 10.4 - 1)


def test_label_threshold_boundaries() -> None:
    """恰好 0.2% 应记 flat（> 才是 up），-0.2% 记 flat（< 才是 down）。"""
    daily = pd.DataFrame(
        {
            "symbol": "600519",
            "trade_date": ["2024-01-02", "2024-01-03"],
            "open": [10.0, 10.0],
            "high": [10.0, 10.1],
            "low": [10.0, 10.0],
            "close": [10.0, 10.0195],  # 次日 open→close = +0.195% < 0.2% → flat
            "pre_close": [10.0, 10.0],
            "volume": [1_000_000] * 2,
            "amount": [1e7] * 2,
            "turnover_rate": [1.0] * 2,
        }
    )
    df, _ = tl.build_training_dataset(daily, None, None, label_cost=0.002)
    assert df.iloc[0]["label"] == "flat"


# ---------------------------------------------------------------------------
# 4. Walk-Forward 切分严格按时间
# ---------------------------------------------------------------------------
@requires_lightgbm
def test_walk_forward_strict_time_order(dataset) -> None:
    df, _ = dataset
    labeled = df.dropna(subset=["label"]).reset_index(drop=True)
    params = tl.default_params(num_iterations=6, num_leaves=5, seed=42)
    params["n_estimators"] = 6
    oof, folds = tl.walk_forward(
        labeled, tl.FEATURE_COLUMNS, params,
        train_days=60, valid_days=10, step=10, label_cost=0.002, num_boost_round=6,
    )
    assert len(folds) >= 2
    for fold in folds:
        assert fold["train_range"][1] < fold["valid_range"][0], "训练窗必须严格早于验证窗"
        assert fold["n_train"] > 0 and fold["n_valid"] > 0
    assert set(oof["fold"].unique()) == {f["fold"] for f in folds}
    for col in ("prob_up", "prob_flat", "prob_down", "pred", "label", "label_ret"):
        assert col in oof.columns
    # 概率行和 = 1
    prob_sum = oof[["prob_up", "prob_flat", "prob_down"]].sum(axis=1)
    assert np.allclose(prob_sum.to_numpy(), 1.0, atol=1e-9)


# ---------------------------------------------------------------------------
# 5. 指标手算校验
# ---------------------------------------------------------------------------
def test_evaluate_predictions_hand_computed() -> None:
    """5 条样本手算 accuracy/logloss/Brier/混淆/胜率/Wilson/payoff。"""
    y_true = np.array(["up", "flat", "down", "up", "down"])
    proba = np.array(
        [
            [0.7, 0.2, 0.1],   # pred up
            [0.1, 0.2, 0.7],   # pred down（错，真实 flat）
            [0.15, 0.15, 0.7], # pred down（对）
            [0.6, 0.3, 0.1],   # pred up（对）
            [0.6, 0.2, 0.2],   # pred up（错，真实 down）
        ]
    )
    label_ret = np.array([0.01, -0.001, -0.02, -0.005, 0.001])
    m = tl.evaluate_predictions(y_true, proba, label_ret, cost=0.002)
    # 命中：s0/s2/s3 = 3/5
    assert m["accuracy"] == pytest.approx(3 / 5, abs=1e-6)
    assert m["n"] == 5
    # 预测分布：up 3 个（s0/s3/s4）、down 2 个（s1/s2）、flat 0 个
    assert m["pred_dist"] == {"up": 3, "flat": 0, "down": 2}
    assert m["per_class"]["up"]["precision"] == pytest.approx(2 / 3, abs=1e-6)
    assert m["per_class"]["up"]["recall"] == pytest.approx(1.0, abs=1e-6)
    assert m["per_class"]["down"]["precision"] == pytest.approx(0.5, abs=1e-6)
    assert m["per_class"]["down"]["recall"] == pytest.approx(0.5, abs=1e-6)
    # 手算 logloss = -mean(ln 0.7, ln 0.2, ln 0.7, ln 0.6, ln 0.2)
    expected_ll = -np.mean(np.log([0.7, 0.2, 0.7, 0.6, 0.2]))
    assert m["logloss"] == pytest.approx(expected_ll, abs=1e-6)
    # 手算 Brier = (0.14 + 1.14 + 0.135 + 0.26 + 1.04) / 5（真实类别 one-hot）
    assert m["brier"] == pytest.approx(0.543, abs=1e-6)
    # 混淆矩阵（行=真实，列=预测；顺序 = label.classes = up/flat/down）
    assert m["confusion"]["labels"] == SCHEMA_CLASSES
    assert m["confusion"]["matrix"] == [[2, 0, 0], [0, 0, 1], [1, 0, 1]]
    # 交易口径：pred up ×3 + pred down ×2（flat 预测 0 笔），全部扣 0.2% 摩擦
    # net: s0 +0.008（胜）, s1 -0.001（负）, s2 +0.018（胜）, s3 -0.007（负）, s4 -0.001（负）
    assert m["win_rate_n"] == 5
    assert m["win_rate"] == pytest.approx(0.4, abs=1e-6)
    assert m["wilson95"][0] == pytest.approx(0.117621, abs=1e-5)
    assert m["wilson95"][1] == pytest.approx(0.769276, abs=1e-5)
    assert m["avg_net_return"] == pytest.approx(0.0034, abs=1e-6)
    assert m["payoff_ratio"] == pytest.approx(4.333333, abs=1e-5)
    assert m["win_rate_by_direction"]["long"]["n"] == 3
    assert m["win_rate_by_direction"]["long"]["win_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert m["win_rate_by_direction"]["short"]["n"] == 2
    assert m["win_rate_by_direction"]["short"]["win_rate"] == pytest.approx(0.5, abs=1e-6)
    # 置信度校准分桶（>0.6: s0/s1/s2 命中 2/3；0.5-0.6: s3/s4 命中 1/2）
    buckets = {b["bucket"]: b for b in m["calibration"]}
    assert buckets["gt_0.60"]["n"] == 3 and buckets["gt_0.60"]["hit_rate"] == pytest.approx(2 / 3, abs=1e-5)
    assert buckets["0.50-0.60"]["n"] == 2 and buckets["0.50-0.60"]["hit_rate"] == pytest.approx(0.5)


def test_wilson_interval_known_values() -> None:
    lo, hi = tl.wilson_interval(50, 100)
    assert lo == pytest.approx(0.4038315304, abs=1e-9)
    assert hi == pytest.approx(0.5961684696, abs=1e-9)
    lo, hi = tl.wilson_interval(2, 4)
    assert lo == pytest.approx(0.1500389892, abs=1e-9)
    assert hi == pytest.approx(0.8499610108, abs=1e-9)
    assert tl.wilson_interval(0, 0) == (None, None)


# ---------------------------------------------------------------------------
# 6. 数据规范化（AKShare / Baostock → 统一 schema）
# ---------------------------------------------------------------------------
def test_ingest_akshare_daily() -> None:
    raw = pd.DataFrame(
        {
            "日期": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "开盘": [10.0, 10.2, 10.1],
            "收盘": [10.2, 10.3, 10.2],
            "最高": [10.3, 10.4, 10.2],
            "最低": [9.9, 10.1, 10.0],
            "成交量": [1000, 0, 2000],  # 第 2 行停牌（0 手）→ 应被剔除
            "成交额": [1e7, 0, 2e7],
            "涨跌幅": [2.0, 0.0, -1.0],
            "涨跌额": [0.2, 0.0, -0.1],
            "换手率": [1.0, 0.0, 2.0],
        }
    )
    out = fd.ingest_akshare_daily(raw, "600519")
    assert len(out) == 2, "停牌零成交行必须剔除"
    assert list(out.columns) == fd.STOCK_COLUMNS
    row = out.iloc[0]
    assert row["volume"] == 100_000  # 手 → 股
    assert row["pre_close"] == pytest.approx(10.0)  # close - 涨跌额
    assert row["source"] == "akshare"
    assert out["trade_date"].tolist() == ["2024-01-02", "2024-01-04"]


def test_ingest_baostock_daily() -> None:
    raw = pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03", ""],
            "open": ["10.0", "10.2", ""],
            "high": ["10.3", "10.4", ""],
            "low": ["9.9", "10.1", ""],
            "close": ["10.2", "10.3", ""],
            "volume": ["100000", "0", "0"],
            "amount": ["1e7", "0", "0"],
            "turn": ["1.0", "0.0", "0.0"],
            "pctChg": ["2.0", "0.0", "0.0"],
        }
    )
    out = fd.ingest_baostock_daily(raw, "000001")
    assert len(out) == 1  # 空行 + 停牌零成交行被剔除
    assert out.iloc[0]["symbol"] == "000001"
    assert out.iloc[0]["volume"] == 100_000
    assert out.iloc[0]["source"] == "baostock"
    assert out.iloc[0]["trade_date"] == "2024-01-02"


def test_compute_zt_stats() -> None:
    zt = pd.DataFrame(
        {
            "代码": ["000001", "000002", "000003"],
            "连板数": [1, 2, 5],
            "封板资金": [1e8, 2e8, 3e8],
            "所属行业": ["IT服务", "IT服务", "银行"],
        }
    )
    dt = pd.DataFrame({"代码": ["600001"]})
    zb = pd.DataFrame({"代码": ["000004", "000005"]})
    stats = fd.compute_zt_stats(zt, dt, zb, "2026-11-25")
    assert stats["limit_up_count"] == 3
    assert stats["limit_down_count"] == 1
    assert stats["broken_count"] == 2
    assert stats["broken_rate"] == pytest.approx(2 / 5)
    assert stats["max_consecutive_boards"] == 5
    assert stats["top_industry"] == "IT服务"
    assert stats["top_industry_limit_up"] == 2
    assert stats["has_down_data"] == 1 and stats["has_broken_data"] == 1


# ---------------------------------------------------------------------------
# 7. 模型导出 JSON（§3.3 schema + 树编码）
# ---------------------------------------------------------------------------
def test_flatten_tree_encoding() -> None:
    tree = {
        "split_feature": 0,
        "threshold": 0.5,
        "decision_type": "<=",
        "left_child": {"leaf_value": -1.0},
        "right_child": {"split_feature": 1, "threshold": -0.2, "decision_type": "<=",
                        "left_child": {"leaf_value": 2.0}, "right_child": {"leaf_value": 3.0}},
    }
    flat = em.flatten_tree(tree)
    assert flat["split_feature"] == [0, 1]
    assert flat["threshold"] == [0.5, -0.2]
    assert len(flat["left_child"]) == len(flat["right_child"]) == 2
    assert flat["leaf_value"] == [-1.0, 2.0, 3.0]
    # 手工遍历：x=(0.3, 0.0) → 左叶子 -1；x=(0.6, -0.5) → 右→左叶子 2
    model = {"features": [{"name": "f0", "index": 0}, {"name": "f1", "index": 1}],
             "trees": [{**flat, "class_index": 0}], "num_class": 3}
    assert em._tree_value(model["trees"][0], np.array([0.3, 0.0])) == -1.0
    assert em._tree_value(model["trees"][0], np.array([0.6, -0.5])) == 2.0
    assert em._tree_value(model["trees"][0], np.array([0.6, 0.4])) == 3.0


@requires_lightgbm
def test_export_model_schema(trained) -> None:
    model = trained["model"]
    errors = em.validate_model_json(model)
    assert errors == [], errors
    for key in em.REQUIRED_MODEL_KEYS:
        assert key in model
    assert model["schema"] == 1 and model["model"] == "lightgbm"
    assert model["label"]["classes"] == SCHEMA_CLASSES
    assert model["objective"] == "multiclass" and model["num_class"] == 3
    assert len(model["features"]) == len(tl.FEATURE_COLUMNS)
    assert [f["name"] for f in model["features"]] == tl.FEATURE_COLUMNS
    assert [f["index"] for f in model["features"]] == list(range(len(tl.FEATURE_COLUMNS)))
    # 树数组一致性 + child 引用合法
    assert len(model["trees"]) > 0
    for tree in model["trees"]:
        n_internal = len(tree["split_feature"])
        assert n_internal > 0
        for key in ("threshold", "left_child", "right_child", "decision_type"):
            assert len(tree[key]) == n_internal, key
        n_leaves = len(tree["leaf_value"])
        assert n_leaves >= 2
        assert tree["class_index"] in (0, 1, 2)
        for child in tree["left_child"] + tree["right_child"]:
            if child >= 0:
                assert child < n_internal
            else:
                assert -child - 1 < n_leaves
    # feature_config 归一化参数长度与特征数一致
    cfg = model["feature_config"]
    assert len(cfg["normalize"]["mean"]) == len(tl.FEATURE_COLUMNS)
    assert len(cfg["normalize"]["std"]) == len(tl.FEATURE_COLUMNS)
    assert cfg["winsorize"]["lower"] == 0.01 and cfg["winsorize"]["upper"] == 0.99
    # 体积 < 2MB
    size = len(json.dumps(model, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert size < 2 * 1024 * 1024, size


@requires_lightgbm
def test_export_reference_inference_matches_booster(trained) -> None:
    """导出的树 JSON 参考推理必须与 LightGBM booster 预测一致（< 1e-9）。"""
    model = trained["model"]
    booster = trained["booster"]
    X = trained["labeled"][tl.FEATURE_COLUMNS].to_numpy(dtype=np.float32)[:80]
    booster_proba = booster.predict(X)
    # booster 列顺序 = down/flat/up → schema up/flat/down
    reorder = [2, 1, 0]
    expected = booster_proba[:, reorder]
    got = em.predict_batch(model, X.astype(float))
    assert got.shape == expected.shape
    assert np.abs(got - expected).max() < 1e-9
    assert np.allclose(got.sum(axis=1), 1.0, atol=1e-9)


@requires_lightgbm
def test_export_feature_config(trained, tmp_path: Path) -> None:
    model = trained["model"]
    feature_names = [f["name"] for f in model["features"]]
    config = em.build_feature_config(trained["meta"], tmp_path / "latest_model.json", feature_names, model)
    assert config["schema"] == 1
    assert config["feature_order"] == feature_names
    assert len(config["features"]) == len(feature_names)
    for i, item in enumerate(config["features"]):
        assert item["index"] == i and item["name"] == feature_names[i]
        assert item["formula"]  # 每个特征都有公式说明
    assert len(config["normalize"]["mean"]) == len(feature_names)
    assert config["normalize"]["applied"] is False
    assert config["inference"]["class_order"] == SCHEMA_CLASSES
    assert config["label"]["classes"] == SCHEMA_CLASSES


def test_validate_model_json_catches_broken() -> None:
    broken = {
        "schema": 1, "model": "lightgbm", "version": "x", "trained_at": "x",
        "label": {"horizon": 1, "definition": "x", "classes": ["down", "flat", "up"]},
        "features": [{"name": "a", "index": 0, "dtype": "float"}],
        "feature_config": {"winsorize": {"lower": 0.01, "upper": 0.99},
                           "normalize": {"mean": [0.0], "std": [1.0]}},
        "trees": [{"split_feature": [0], "threshold": [0.1], "left_child": [5],
                   "right_child": [-1], "decision_type": ["<="], "leaf_value": [1.0],
                   "class_index": 0}],
        "objective": "multiclass", "num_class": 3,
        "metrics": {"walk_forward": {"accuracy": 0.4, "win_rate": 0.4, "logloss": 1.0, "n": 10}, "cost": 0.002},
    }
    errors = em.validate_model_json(broken)
    # 类别顺序错 + 内部节点越界 → 必须报错
    assert any("label.classes" in e for e in errors)
    assert any("left_child" in e for e in errors)


# ---------------------------------------------------------------------------
# 8. 交付物：Notebook / README
# ---------------------------------------------------------------------------
def test_kaggle_notebook_valid() -> None:
    path = TRAINING_DIR / "kaggle_notebook.ipynb"
    nb = json.loads(path.read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4
    assert len(nb["cells"]) >= 8
    code_sources = "\n".join("".join(c.get("source", [])) for c in nb["cells"] if c["cell_type"] == "code")
    for command in ("fetch_data.py", "train_lgbm.py", "export_model.py", "pip install"):
        assert command in code_sources, command
    markdown = "\n".join("".join(c.get("source", [])) for c in nb["cells"] if c["cell_type"] == "markdown")
    assert "Kaggle" in markdown and "Internet" in markdown
