"""模型导出与推理测试（契约 §5 / §10）。

覆盖：
- ``model.json`` 必备字段 / 形状校验（load_model）；
- 推理严格符合 §5 公式：``z=(x-mean)/scale; p=softmax(z@coef.T+intercept)``；
- 概率和 = 1、class 顺序映射正确（含 classes 乱序的鲁棒性）；
- 固定 seed 重训权重逐位一致（可复现）；
- 与 ``tests/fixtures/model/parity_vectors.json`` 的独立实现对齐（Cloudflare TS 侧同源）；
- ``infer_signal`` / ``generate_signal`` 输出契约 §4 字段。
"""
from __future__ import annotations

import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.features.builder import FEATURE_COLUMNS
from quant.model.predict import (
    DIRECTIONS,
    REQUIRED_MODEL_KEYS,
    REQUIRED_SIGNAL_KEYS,
    generate_signal,
    infer_signal,
    load_model,
    predict_direction,
    predict_proba,
    predict_proba_batch,
    validate_model_structure,
)
from quant.model.train import train

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "model"
MODEL_FIXTURE = FIXTURE_DIR / "model.json"
PARITY_FIXTURE = FIXTURE_DIR / "parity_vectors.json"


def _load_synth():
    spec = importlib.util.spec_from_file_location(
        "model_synth_fixture_export", FIXTURE_DIR / "synth.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


SYNTH = _load_synth()


# ---------------------------------------------------------------------------
# module 级 fixture：合成库 + 一次真实训练
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    base = tmp_path_factory.mktemp("model_export")
    db = SYNTH.write_daily_db(SYNTH.load_fixture_csv(), base / "quant_synth.db")
    model_path = base / "model.json"
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
            "random_seed": 42,
            "version": "v-test",
            "out": str(model_path),
        },
        "backtest": {"start": "2025-01-01", "end": "2026-11-20", "report_dir": str(base / "reports")},
        "paper": {"initial_cash": 1000000},
    }
    metrics = train(cfg["symbols"], cfg)
    return {"cfg": cfg, "metrics": metrics, "model_path": model_path, "base": base, "db": db}


# ---------------------------------------------------------------------------
# model.json schema
# ---------------------------------------------------------------------------
def test_load_fixture_model_schema() -> None:
    model = load_model(MODEL_FIXTURE)
    for key in REQUIRED_MODEL_KEYS:
        assert key in model, key
    assert model["kind"] == "softmax_regression"
    assert model["schema"] == 1
    assert model["classes"] == ["down", "flat", "up"]
    assert len(model["features"]) == len(model["mean"]) == len(model["scale"])


def test_load_model_rejects_invalid(tmp_path: Path) -> None:
    raw = json.loads(MODEL_FIXTURE.read_text(encoding="utf-8"))
    cases = []
    broken = dict(raw)
    broken.pop("coef")
    cases.append(broken)
    broken = dict(raw, coef=[[0.0], [0.0]])  # 行数/列数都不对
    cases.append(broken)
    broken = dict(raw, classes=["down", "flat"])
    cases.append(broken)
    broken = dict(raw, kind="gbdt")
    cases.append(broken)
    for i, payload in enumerate(cases):
        path = tmp_path / f"bad_{i}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError):
            load_model(path)
    with pytest.raises(FileNotFoundError):
        load_model(tmp_path / "missing.json")


def test_validate_model_structure_ok() -> None:
    validate_model_structure(load_model(MODEL_FIXTURE))


# ---------------------------------------------------------------------------
# §5 推理公式
# ---------------------------------------------------------------------------
def _reference_proba(model: dict, x: dict[str, float]) -> dict[str, float]:
    """测试内独立实现 §5 公式（plain math，不复用生产代码路径）。"""
    features = model["features"]
    z = [
        (float(x[name]) - float(model["mean"][i])) / float(model["scale"][i])
        for i, name in enumerate(features)
    ]
    scores = [
        sum(float(model["coef"][c][i]) * z[i] for i in range(len(features)))
        + float(model["intercept"][c])
        for c in range(len(model["classes"]))
    ]
    top = max(scores)
    exp = [math.exp(s - top) for s in scores]
    total = sum(exp)
    return {name: exp[i] / total for i, name in enumerate(model["classes"])}


def test_predict_proba_matches_contract_formula() -> None:
    model = load_model(MODEL_FIXTURE)
    parity = json.loads(PARITY_FIXTURE.read_text(encoding="utf-8"))
    for vector in parity["vectors"]:
        x = vector["input"]
        got = predict_proba(model, x)
        want = _reference_proba(model, x)
        for name in DIRECTIONS:
            assert got[name] == pytest.approx(want[name], abs=1e-12)
            assert got[name] == pytest.approx(vector["expected"][name], abs=1e-6)


def test_probabilities_sum_to_one() -> None:
    model = load_model(MODEL_FIXTURE)
    rng = np.random.default_rng(3)
    for _ in range(50):
        x = {"f_ret": float(rng.normal(0, 0.05)), "f_vol": float(rng.uniform(0, 0.2))}
        p = predict_proba(model, x)
        assert set(p) == set(DIRECTIONS)
        assert sum(p.values()) == pytest.approx(1.0, abs=1e-12)
        assert all(0.0 <= v <= 1.0 for v in p.values())


def test_classes_order_mapping() -> None:
    """classes 顺序必须按名字映射；乱序模型也要给出同样的按类概率。"""
    model = load_model(MODEL_FIXTURE)
    order = [2, 1, 0]  # ["up", "flat", "down"]，coef/intercept 同步重排
    reordered = dict(
        model,
        classes=[model["classes"][i] for i in order],
        coef=[model["coef"][i] for i in order],
        intercept=[model["intercept"][i] for i in order],
    )
    x = {"f_ret": 0.012, "f_vol": 0.03}
    base = predict_proba(model, x)
    other = predict_proba(reordered, x)
    for name in DIRECTIONS:
        assert base[name] == pytest.approx(other[name], abs=1e-12)

    # 方向 = classes 顺序里概率最大者（同概率优先靠前），与批量 argmax 一致
    batch = predict_proba_batch(model, np.array([[x["f_ret"], x["f_vol"]]]))[0]
    expect = model["classes"][int(np.argmax(batch))]
    assert predict_direction(base, model["classes"]) == expect


def test_predict_proba_batch_matches_single() -> None:
    model = load_model(MODEL_FIXTURE)
    parity = json.loads(PARITY_FIXTURE.read_text(encoding="utf-8"))
    X = np.array([[v["input"]["f_ret"], v["input"]["f_vol"]] for v in parity["vectors"]])
    batch = predict_proba_batch(model, X)
    for row, vector in zip(batch, parity["vectors"]):
        single = predict_proba(model, vector["input"])
        for i, cls in enumerate(model["classes"]):
            assert row[i] == pytest.approx(single[cls], abs=1e-12)
        assert row.sum() == pytest.approx(1.0, abs=1e-12)


def test_predict_proba_input_errors() -> None:
    model = load_model(MODEL_FIXTURE)
    with pytest.raises(KeyError):
        predict_proba(model, {"f_ret": 0.01})
    with pytest.raises(ValueError):
        predict_proba(model, {"f_ret": float("nan"), "f_vol": 0.02})


# ---------------------------------------------------------------------------
# 训练导出（可复现性 / §5 字段）
# ---------------------------------------------------------------------------
def test_trained_model_export_contract(trained) -> None:
    model = load_model(trained["model_path"])
    for key in REQUIRED_MODEL_KEYS:
        assert key in model
    assert model["kind"] == "softmax_regression"
    assert model["classes"] == ["down", "flat", "up"]
    assert model["features"] == FEATURE_COLUMNS
    n = len(FEATURE_COLUMNS)
    assert len(model["mean"]) == n and len(model["scale"]) == n
    assert np.asarray(model["coef"]).shape == (3, n)
    assert len(model["intercept"]) == 3
    assert np.all(np.asarray(model["scale"]) > 0)
    for key in ("accuracy", "logloss", "n"):
        assert key in model["metrics"]
    assert 0.0 <= model["metrics"]["accuracy"] <= 1.0
    assert model["metrics"]["n"] > 0
    assert model["metrics"]["random_seed"] == 42
    assert model["train_range"][0] <= model["train_range"][1]
    assert model["horizon"] == "1d"
    assert "up" in model["label_rule"] and "flat" in model["label_rule"]


def test_train_reproducible(trained) -> None:
    """固定 random_seed=42 重训，权重逐位一致（trained_at 除外）。"""
    model1 = load_model(trained["model_path"])
    cfg = dict(trained["cfg"])
    cfg["model"] = dict(cfg["model"], out=str(trained["base"] / "model_again.json"))
    train(cfg["symbols"], cfg)
    model2 = load_model(trained["base"] / "model_again.json")
    for key in ("classes", "features", "mean", "scale", "coef", "intercept"):
        assert model1[key] == model2[key], key
    assert model1["metrics"] == model2["metrics"]


def test_infer_signal_contract(trained) -> None:
    model = load_model(trained["model_path"])
    daily = SYNTH.load_fixture_csv()
    one = daily[daily["symbol"] == "600519"].reset_index(drop=True)
    inferred = infer_signal(model, one)
    assert inferred is not None
    assert inferred["direction"] in model["classes"]
    assert sum(inferred["prob"].values()) == pytest.approx(1.0, abs=1e-9)
    assert inferred["confidence"] == pytest.approx(max(inferred["prob"].values()), abs=1e-3)
    top = inferred["top_features"]
    assert 1 <= len(top) <= 5
    contribs = [abs(item["contrib"]) for item in top]
    assert contribs == sorted(contribs, reverse=True)
    assert inferred["trade_date"] == one["trade_date"].max()
    # 数据不足（只有 10 行）→ None
    assert infer_signal(model, one.iloc[:10].reset_index(drop=True)) is None


def test_generate_signal_contract(trained) -> None:
    cfg = trained["cfg"]
    sig = generate_signal("600519", cfg)
    for key in REQUIRED_SIGNAL_KEYS:
        assert key in sig, key
    assert sig["schema"] == 1
    assert sig["symbol"] == "600519"
    assert sig["direction"] in DIRECTIONS
    assert sum(sig["prob"].values()) == pytest.approx(1.0, abs=1e-6)
    assert sig["confidence"] == pytest.approx(round(max(sig["prob"].values()), 4), abs=1e-9)
    assert sig["status"] in {"ok", "stale", "warming_up"}
    assert sig["horizon"] == "1d"
    assert sig["model_version"] == "v-test"
    assert len(sig["top_features"]) <= 5
    exp = datetime.fromisoformat(sig["expires_at"])
    assert exp.tzinfo is not None and exp.strftime("%H:%M") == "09:30"
    assert exp.date().isoformat() > sig["trade_date"]
    assert set(sig["paper"]) == {"equity", "position_qty", "today_pnl"}
    assert sig["price"] > 0


def test_generate_signal_includes_paper_position(trained, monkeypatch) -> None:
    """契约 §4 paper 字段：position_qty 取该 symbol 的持仓，equity/today_pnl 来自 paper 快照。"""
    from quant.paper import engine as paper_engine

    monkeypatch.setattr(
        paper_engine,
        "snapshot",
        lambda cfg: {
            "equity": 123456.0,
            "today_pnl": 12.5,
            "positions": [
                {"symbol": "600519", "qty": 300},
                {"symbol": "000001", "qty": 100},
            ],
        },
    )
    sig = generate_signal("600519", trained["cfg"])
    assert sig["paper"] == {"equity": 123456.0, "position_qty": 300, "today_pnl": 12.5}
    sig2 = generate_signal("300750", trained["cfg"])
    assert sig2["paper"]["position_qty"] == 0


def test_generate_signal_warming_up_when_no_data(trained) -> None:
    cfg = dict(trained["cfg"])
    cfg["data"] = dict(cfg["data"], db=str(trained["base"] / "no_such_db" / "x.db"))
    sig = generate_signal("600519", cfg)
    assert sig["status"] == "warming_up"
    assert sig["direction"] == "flat"
    assert sum(sig["prob"].values()) == pytest.approx(1.0, abs=1e-6)
    assert sig["top_features"] == []


def test_generate_signal_warming_up_when_no_model(trained) -> None:
    cfg = dict(trained["cfg"])
    cfg["model"] = dict(cfg["model"], out=str(trained["base"] / "absent_model.json"))
    sig = generate_signal("600519", cfg)
    assert sig["status"] == "warming_up"
    assert sig["model_version"] == "unavailable"
    assert sig["direction"] == "flat"


def test_parity_fixture_matches_model() -> None:
    """parity 向量与固定 model.json 一致（供 cloud-dev TS 侧对照，契约 §10）。"""
    model = load_model(MODEL_FIXTURE)
    parity = json.loads(PARITY_FIXTURE.read_text(encoding="utf-8"))
    assert parity["classes"] == model["classes"]
    assert parity["features"] == model["features"]
    for vector in parity["vectors"]:
        got = predict_proba(model, vector["input"])
        for name in DIRECTIONS:
            assert got[name] == pytest.approx(vector["expected"][name], abs=1e-6)
