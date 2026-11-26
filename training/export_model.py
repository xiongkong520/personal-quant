#!/usr/bin/env python3
"""零成本训练管道 · 第 3 步：LightGBM → 轻量模型 JSON（供 Cloudflare Worker 加载推理）

输出
----
- ``model/latest_model.json``   轻量树模型（schema 见 docs/UPGRADE_PLAN.md §3.3，目标 < 2MB）
- ``model/feature_config.json`` 特征名/顺序/归一化参数/公式/重要性/推理约定

树编码约定（与 Worker 的 model_infer.ts 对齐）
----------------------------------------------
每棵树::

    {
      "split_feature": [int, ...],     # 内部节点分裂特征下标（对应 features[].index）
      "threshold":     [float, ...],   # 内部节点阈值
      "decision_type": ["<=", ...],    # 内部节点比较方向（数值特征均为 "<="）
      "left_child":    [int, ...],     # >=0 指向内部节点下标；<0 指向叶子：leaf_idx = -child-1
      "right_child":   [int, ...],
      "leaf_value":    [float, ...],   # 叶子输出值（按叶子下标顺序）
      "class_index":   int,            # 该树贡献的类别，0=up 1=flat 2=down（与 label.classes 一致）
    }

推理：``raw[class_index] += tree(x)``；三分类 ``p = softmax(raw)``；特征按 ``features[].index`` 顺序输入，
缺失特征按 0 处理（与训练 ``fillna(0)`` 同口径）。

体积控制
--------
默认参数（120 轮 × 15 叶 × 3 类）导出约 200KB；若超过 ``--max-size-mb`` 会自动按类裁剪树数，
并用 ``training/out/oof.parquet`` 重新评估样本外指标（报告 ``trees_per_class``/``truncated``）。

用法
----
    python training/export_model.py
    python training/export_model.py --max-size-mb 2.0 --out-model model/latest_model.json
    python training/export_model.py --validate-only model/latest_model.json
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

LOGGER = logging.getLogger("training.export_model")
TRAINING_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TRAINING_DIR.parent
DEFAULT_OUT_DIR = TRAINING_DIR / "out"
DEFAULT_MODEL_DIR = PROJECT_ROOT / "model"
TZ = timezone(timedelta(hours=8))

SCHEMA_CLASSES: list[str] = ["up", "flat", "down"]
REQUIRED_MODEL_KEYS = (
    "schema", "model", "version", "trained_at", "label", "features", "feature_config",
    "trees", "objective", "num_class", "metrics",
)
Z95 = 1.959963984540054


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _lgb():
    import lightgbm as lgb  # noqa: PLC0415

    return lgb


def load_booster(path: Path):
    return _lgb().Booster(model_file=str(path))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _compact_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# LightGBM dump → 轻量树
# ---------------------------------------------------------------------------
def flatten_tree(root: dict) -> dict[str, list]:
    """把 LightGBM 嵌套 ``tree_structure`` 压平成 Worker 友好的数组结构。

    内部节点 id = 首次访问顺序 0..n_internal-1；叶子 child 用 ``-(leaf_idx+1)`` 编码。
    """
    split_feature: list[int] = []
    threshold: list[float] = []
    decision_type: list[str] = []
    left_child: list[int] = []
    right_child: list[int] = []
    leaf_value: list[float] = []

    def visit(node: dict) -> int:
        if "split_feature" not in node:  # 叶子
            leaf_value.append(float(node["leaf_value"]))
            return -(len(leaf_value))
        node_id = len(split_feature)
        split_feature.append(int(node["split_feature"]))
        threshold.append(float(node["threshold"]))
        decision_type.append(str(node.get("decision_type", "<=")))
        left_child.append(0)   # 占位
        right_child.append(0)
        left_ref = visit(node["left_child"])
        right_ref = visit(node["right_child"])
        left_child[node_id] = left_ref
        right_child[node_id] = right_ref
        return node_id

    visit(root)
    return {
        "split_feature": split_feature,
        "threshold": threshold,
        "decision_type": decision_type,
        "left_child": left_child,
        "right_child": right_child,
        "leaf_value": leaf_value,
    }


def trees_from_dump(
    dump: dict,
    booster_classes: list[str] | None = None,
    schema_classes: list[str] | None = None,
) -> list[dict]:
    """dump_model() → 轻量树列表；``class_index`` 重排到 schema 类别顺序。

    LightGBM 多分类 dump 的 ``tree_info`` 按迭代主序排列：第 i 棵树属于
    ``booster_classes[i % num_class]``。
    """
    booster_classes = list(booster_classes or ["down", "flat", "up"])
    schema_classes = list(schema_classes or SCHEMA_CLASSES)
    num_class = int(dump.get("num_class", 3))
    tree_info = dump["tree_info"]
    trees: list[dict] = []
    for position, info in enumerate(tree_info):
        booster_class_idx = position % num_class
        booster_class = booster_classes[booster_class_idx]
        if booster_class not in schema_classes:
            raise ValueError(f"未知类别 {booster_class!r}，schema 类别={schema_classes}")
        tree = flatten_tree(info["tree_structure"])
        tree["class_index"] = schema_classes.index(booster_class)
        trees.append(tree)
    return trees


# ---------------------------------------------------------------------------
# 模型 JSON 构建
# ---------------------------------------------------------------------------
def build_model_json(
    booster,
    meta: dict,
    version: str | None = None,
    trained_at: str | None = None,
    trees: list[dict] | None = None,
) -> dict:
    """按 §3.3 组装轻量模型 JSON。"""
    dump = booster.dump_model()
    feature_names = [str(f) for f in (meta.get("features") or booster.feature_name())]
    infos = dump.get("feature_infos")
    if isinstance(infos, list) and len(feature_names) != len(infos):
        LOGGER.warning("特征数（%d）与 dump 特征信息（%d）不一致", len(feature_names), len(infos))
    if trees is None:
        trees = trees_from_dump(dump, meta.get("booster_classes"), meta.get("schema_classes"))
    stats = meta.get("feature_stats") or {}
    n_feat = len(feature_names)
    mean = list(stats.get("mean") or [0.0] * n_feat)
    std = list(stats.get("std") or [1.0] * n_feat)
    label = meta.get("label") or {
        "horizon": 1,
        "definition": "T+1 收益 > 0.2%（双边摩擦）→ up；< -0.2% → down；否则 flat",
        "classes": list(SCHEMA_CLASSES),
        "cost": 0.002,
    }
    walk = (meta.get("metrics") or {}).get("walk_forward") or {}
    metrics = {
        "walk_forward": {
            "accuracy": walk.get("accuracy"),
            "win_rate": walk.get("win_rate"),
            "wilson95": walk.get("wilson95"),
            "logloss": walk.get("logloss"),
            "n": walk.get("n"),
            "folds": len((meta.get("metrics") or {}).get("folds") or []),
            "win_rate_by_direction": walk.get("win_rate_by_direction"),
        },
        "cost": float(label.get("cost", 0.002)),
    }
    model = {
        "schema": 1,
        "model": "lightgbm",
        "version": version or meta.get("version") or f"lgbm-{datetime.now(TZ).strftime('%Y-%m-%d')}",
        "trained_at": trained_at or meta.get("trained_at") or now_iso(),
        "label": {
            "horizon": int(label.get("horizon", 1)),
            "definition": str(label.get("definition", "")),
            "classes": list(label.get("classes") or SCHEMA_CLASSES),
        },
        "features": [
            {"name": name, "index": i, "dtype": "float"} for i, name in enumerate(feature_names)
        ],
        "feature_config": {
            "winsorize": {"lower": 0.01, "upper": 0.99},
            "normalize": {"mean": mean, "std": std},
        },
        "trees": trees,
        "objective": "multiclass",
        "num_class": 3,
        "metrics": metrics,
    }
    return model


def build_feature_config(meta: dict, model_path: Path, feature_names: list[str], model: dict) -> dict:
    """feature_config.json：特征名/顺序/公式/归一化参数/推理约定。"""
    stats = meta.get("feature_stats") or {}
    n_feat = len(feature_names)
    return {
        "schema": 1,
        "model_version": model["version"],
        "model_file": str(model_path.name),
        "feature_order": list(feature_names),
        "features": [
            {
                "name": name,
                "index": i,
                "dtype": "float",
                "group": (meta.get("feature_groups") or {}).get(name, "其他"),
                "formula": (meta.get("feature_formulas") or {}).get(name, ""),
            }
            for i, name in enumerate(feature_names)
        ],
        "winsorize": {
            "lower": 0.01,
            "upper": 0.99,
            "values_lower": list(stats.get("winsor_lower") or [None] * n_feat),
            "values_upper": list(stats.get("winsor_upper") or [None] * n_feat),
            "applied": False,
            "note": "LightGBM 树对单调变换不敏感，训练未做缩尾/标准化；此处供展示与未来预处理参考",
        },
        "normalize": {
            "mean": list(stats.get("mean") or [0.0] * n_feat),
            "std": list(stats.get("std") or [1.0] * n_feat),
            "applied": False,
            "note": "树模型推理不需要标准化；Worker 按 feature_order 原始值遍历即可",
        },
        "label": model["label"],
        "benchmark": {
            "code": (meta.get("data") or {}).get("benchmark"),
            "excess_return_fallback": "股票池等权",
        },
        "inference": {
            "feature_missing_policy": "缺失特征填 0（与训练 fillna(0) 同口径）",
            "tree_encoding": "child>=0 → 内部节点下标；child<0 → 叶子，leaf_idx = -child-1",
            "class_order": list(SCHEMA_CLASSES),
            "raw_score": "raw[class_index] += tree(x)",
            "probability": "softmax(raw)",
        },
        "feature_importance": (meta.get("metrics") or {}).get("feature_importance") or [],
        "data": meta.get("data") or {},
    }


# ---------------------------------------------------------------------------
# 参考推理（Python 侧，用于 parity 测试 / 裁剪重评估）
# ---------------------------------------------------------------------------
def _feature_vector(model: dict, x: dict[str, float] | Iterable[float]) -> np.ndarray:
    if isinstance(x, dict):
        return np.array([float(x.get(f["name"], 0.0) or 0.0) for f in model["features"]], dtype=float)
    return np.asarray(list(x), dtype=float)


def _tree_value(tree: dict, x: np.ndarray) -> float:
    node = 0
    split_feature = tree["split_feature"]
    threshold = tree["threshold"]
    decision_type = tree["decision_type"]
    left = tree["left_child"]
    right = tree["right_child"]
    leaf_values = tree["leaf_value"]
    while node >= 0:
        feature_value = x[split_feature[node]]
        if decision_type[node] == "<=":
            node = left[node] if feature_value <= threshold[node] else right[node]
        elif decision_type[node] == "==":
            node = left[node] if feature_value == threshold[node] else right[node]
        else:  # pragma: no cover - LightGBM 数值特征只会出现 "<="
            raise ValueError(f"不支持的 decision_type: {decision_type[node]}")
    return leaf_values[-node - 1]


def predict_raw(model: dict, x: dict[str, float] | Iterable[float]) -> np.ndarray:
    """原始分（未 softmax）：``raw[class_index] += tree(x)``。"""
    vector = _feature_vector(model, x)
    raw = np.zeros(int(model["num_class"]), dtype=float)
    for tree in model["trees"]:
        raw[int(tree["class_index"])] += _tree_value(tree, vector)
    return raw


def predict_proba(model: dict, x: dict[str, float] | Iterable[float]) -> np.ndarray:
    """三分类概率，顺序与 ``label.classes``（up/flat/down）一致。"""
    raw = predict_raw(model, x)
    shifted = raw - np.max(raw)
    exp = np.exp(shifted)
    return exp / exp.sum()


def predict_batch(model: dict, X: np.ndarray) -> np.ndarray:
    """批量参考推理 → ``(n, 3)`` 概率矩阵（列顺序 = label.classes）。"""
    X = np.asarray(X, dtype=float)
    if X.ndim == 1:
        X = X.reshape(1, -1)
    if X.shape[1] != len(model["features"]):
        raise ValueError(f"特征矩阵列数 {X.shape[1]} != 模型特征数 {len(model['features'])}")
    return np.array([predict_proba(model, row) for row in X])


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def validate_model_json(model: dict) -> list[str]:
    """校验 §3.3 模型 JSON 结构与树编码，返回错误列表（空 = 通过）。"""
    errors: list[str] = []
    for key in REQUIRED_MODEL_KEYS:
        if key not in model:
            errors.append(f"缺少字段: {key}")
    if errors:
        return errors
    if model["schema"] != 1:
        errors.append(f"schema 应为 1，实际 {model['schema']!r}")
    if model["model"] != "lightgbm":
        errors.append(f"model 应为 lightgbm，实际 {model['model']!r}")
    if model["objective"] != "multiclass":
        errors.append(f"objective 应为 multiclass，实际 {model['objective']!r}")
    if int(model["num_class"]) != 3:
        errors.append(f"num_class 应为 3，实际 {model['num_class']!r}")
    classes = (model.get("label") or {}).get("classes")
    if list(classes or []) != SCHEMA_CLASSES:
        errors.append(f"label.classes 应为 {SCHEMA_CLASSES}，实际 {classes!r}")
    features = model.get("features") or []
    if not features:
        errors.append("features 为空")
    names = [f.get("name") for f in features]
    if len(set(names)) != len(names):
        errors.append("features 存在重名")
    for i, feature in enumerate(features):
        if feature.get("index") != i:
            errors.append(f"features[{i}].index 应为 {i}，实际 {feature.get('index')!r}")
        if not isinstance(feature.get("name"), str) or not feature.get("name"):
            errors.append(f"features[{i}].name 非法")
    feature_config = model.get("feature_config") or {}
    normalize = feature_config.get("normalize") or {}
    n_feat = len(features)
    if len(normalize.get("mean") or []) != n_feat or len(normalize.get("std") or []) != n_feat:
        errors.append("feature_config.normalize.mean/std 长度与特征数不一致")
    if "winsorize" not in feature_config:
        errors.append("feature_config 缺少 winsorize")

    trees = model.get("trees") or []
    if not trees:
        errors.append("trees 为空")
    for t_idx, tree in enumerate(trees):
        missing_keys = [
            key for key in ("split_feature", "threshold", "left_child", "right_child", "decision_type")
            if key not in tree
        ]
        if missing_keys:
            errors.append(f"trees[{t_idx}] 缺少 {missing_keys}")
            continue
        n_internal = len(tree["split_feature"])
        if n_internal == 0:
            errors.append(f"trees[{t_idx}] 没有内部节点")
            continue
        for key in ("threshold", "left_child", "right_child", "decision_type"):
            if len(tree[key]) != n_internal:
                errors.append(f"trees[{t_idx}].{key} 长度应为 {n_internal}")
        n_leaves = len(tree.get("leaf_value") or [])
        if n_leaves == 0:
            errors.append(f"trees[{t_idx}] 没有叶子")
            continue
        if int(tree.get("class_index", -1)) not in (0, 1, 2):
            errors.append(f"trees[{t_idx}].class_index 非法: {tree.get('class_index')!r}")
        child_valid = True
        for key in ("left_child", "right_child"):
            for child in tree[key]:
                if child >= 0:
                    if child >= n_internal:
                        errors.append(f"trees[{t_idx}].{key} 内部节点下标越界: {child}")
                        child_valid = False
                elif -child - 1 >= n_leaves:
                    errors.append(f"trees[{t_idx}].{key} 叶子下标越界: {child}")
                    child_valid = False
        for dtype in tree["decision_type"]:
            if dtype not in ("<=", "==", "<", ">"):
                errors.append(f"trees[{t_idx}].decision_type 非法: {dtype!r}")
        # 根节点可达性 & 全部节点被引用（无孤立节点）；child 已越界时跳过
        if child_valid:
            referenced = set()
            stack = [0]
            while stack:
                node = stack.pop()
                if node < 0:
                    continue
                if node in referenced and node != 0:
                    continue
                referenced.add(node)
                stack.extend([tree["left_child"][node], tree["right_child"][node]])
            if len(referenced) != n_internal:
                errors.append(f"trees[{t_idx}] 存在不可达内部节点: {n_internal - len(referenced)} 个")
    metrics = model.get("metrics") or {}
    walk = metrics.get("walk_forward") or {}
    for key in ("accuracy", "win_rate", "logloss", "n"):
        if key not in walk:
            errors.append(f"metrics.walk_forward 缺少 {key}")
    if "cost" not in metrics:
        errors.append("metrics 缺少 cost")
    return errors


# ---------------------------------------------------------------------------
# 裁剪（体积控制）
# ---------------------------------------------------------------------------
def _count_trees_per_class(trees: list[dict]) -> int:
    counts = [0, 0, 0]
    for tree in trees:
        counts[int(tree["class_index"])] += 1
    return max(counts)


def _truncate_trees(trees: list[dict], keep_per_class: int) -> list[dict]:
    counts = [0, 0, 0]
    kept: list[dict] = []
    for tree in trees:
        cls = int(tree["class_index"])
        if counts[cls] < keep_per_class:
            kept.append(tree)
            counts[cls] += 1
    return kept


def _reevaluate(model: dict, oof: pd.DataFrame, meta: dict) -> dict:
    """用 OOF 重算裁剪后模型的样本外指标。"""
    sys.path.insert(0, str(TRAINING_DIR))
    from train_lgbm import evaluate_predictions  # noqa: PLC0415

    feature_names = [f["name"] for f in model["features"]]
    missing = [n for n in feature_names if n not in oof.columns]
    if missing:
        LOGGER.warning("OOF 缺少特征 %s，跳过重评估", missing[:5])
        return {}
    X = oof[feature_names].to_numpy(dtype=float)
    proba = predict_batch(model, X)
    labels = oof["label"].astype(str).to_numpy()
    label_ret = pd.to_numeric(oof["label_ret"], errors="coerce").to_numpy()
    cost = float((meta.get("label") or {}).get("cost", 0.002))
    return evaluate_predictions(labels, proba, label_ret, cost=cost)


def export_model(
    booster,
    meta: dict,
    oof: pd.DataFrame | None = None,
    max_size_mb: float = 2.0,
    version: str | None = None,
) -> tuple[dict, dict]:
    """导出 (model_json, stats)，自动做体积裁剪。"""
    model = build_model_json(booster, meta, version=version)
    features = [f["name"] for f in model["features"]]
    payload = _compact_json(model)
    size = len(payload.encode("utf-8"))
    max_bytes = int(max_size_mb * 1024 * 1024)
    stats: dict[str, Any] = {"size_bytes": size, "truncated": False, "trees_per_class": _count_trees_per_class(model["trees"])}
    if size > max_bytes and len(model["trees"]) > 3:
        keep = _count_trees_per_class(model["trees"])
        while keep > 1:
            keep = max(1, int(keep * 0.8))
            candidate = dict(model, trees=_truncate_trees(model["trees"], keep))
            candidate_size = len(_compact_json(candidate).encode("utf-8"))
            if candidate_size <= max_bytes:
                model = candidate
                stats.update({
                    "truncated": True,
                    "trees_per_class": keep,
                    "size_bytes": candidate_size,
                    "note": f"为满足 <{max_size_mb}MB 按类别裁剪到每类 {keep} 棵树",
                })
                LOGGER.warning("模型超过 %.1fMB，已裁剪到每类 %d 棵树（%.1fKB）", max_size_mb, keep, candidate_size / 1024)
                break
        if not stats["truncated"]:
            raise RuntimeError(
                f"模型体积 {size / 1024 / 1024:.2f}MB 超过上限 {max_size_mb}MB，且无法通过裁剪降到限制内；"
                "请减小 --num-iterations / --num-leaves"
            )
    if stats["truncated"] and oof is not None and len(oof) > 0:
        new_metrics = _reevaluate(model, oof, meta)
        if new_metrics:
            model["metrics"]["walk_forward"].update({
                "accuracy": new_metrics["accuracy"],
                "win_rate": new_metrics["win_rate"],
                "wilson95": new_metrics["wilson95"],
                "logloss": new_metrics["logloss"],
                "n": new_metrics["n"],
            })
            stats["reevaluated"] = True
    stats["size_bytes"] = len(_compact_json(model).encode("utf-8"))
    stats["walk_forward"] = model["metrics"]["walk_forward"]
    stats["features"] = len(features)
    stats["trees"] = len(model["trees"])
    return model, stats


def write_model(model: dict, config: dict, model_path: Path, config_path: Path) -> int:
    model_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # 紧凑 JSON 控制体积；Worker 解析不需要缩进
    model_path.write_text(_compact_json(model) + "\n", encoding="utf-8")
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return model_path.stat().st_size


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LightGBM → 轻量模型 JSON（零成本训练管道第 3 步）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--booster", default=str(DEFAULT_OUT_DIR / "model.txt"))
    parser.add_argument("--meta", default=str(DEFAULT_OUT_DIR / "train_meta.json"))
    parser.add_argument("--oof", default=str(DEFAULT_OUT_DIR / "oof.parquet"))
    parser.add_argument("--out-model", default=str(DEFAULT_MODEL_DIR / "latest_model.json"))
    parser.add_argument("--out-config", default=str(DEFAULT_MODEL_DIR / "feature_config.json"))
    parser.add_argument("--max-size-mb", type=float, default=2.0)
    parser.add_argument("--version", default=None, help="覆盖版本号（默认取 train_meta.json）")
    parser.add_argument("--validate-only", default=None, help="只校验已有 latest_model.json")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.validate_only:
        model = load_json(Path(args.validate_only))
        errors = validate_model_json(model)
        print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
        return 0 if not errors else 1

    booster_path = Path(args.booster)
    meta_path = Path(args.meta)
    if not booster_path.exists():
        raise FileNotFoundError(f"缺少 booster：{booster_path}（先运行 training/train_lgbm.py）")
    if not meta_path.exists():
        raise FileNotFoundError(f"缺少训练元数据：{meta_path}（先运行 training/train_lgbm.py）")
    booster = load_booster(booster_path)
    meta = load_json(meta_path)

    oof = None
    oof_path = Path(args.oof)
    if oof_path.exists():
        try:
            oof = pd.read_parquet(oof_path)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("读取 OOF 失败（跳过裁剪重评估）：%s", exc)

    model, stats = export_model(booster, meta, oof=oof, max_size_mb=args.max_size_mb, version=args.version)
    errors = validate_model_json(model)
    if errors:
        raise RuntimeError(f"导出模型 JSON 未通过 schema 校验：{errors}")
    feature_names = [f["name"] for f in model["features"]]
    config = build_feature_config(meta, Path(args.out_model), feature_names, model)
    size = write_model(model, config, Path(args.out_model), Path(args.out_config))
    summary = {
        "ok": True,
        "model": str(args.out_model),
        "feature_config": str(args.out_config),
        "size_bytes": size,
        "size_kb": round(size / 1024, 1),
        "size_mb": round(size / 1024 / 1024, 3),
        "under_limit_mb": args.max_size_mb,
        "features": stats["features"],
        "trees": stats["trees"],
        "trees_per_class": stats["trees_per_class"],
        "truncated": stats["truncated"],
        "walk_forward": stats["walk_forward"],
        "schema_ok": not errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
