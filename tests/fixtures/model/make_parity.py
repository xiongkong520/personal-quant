"""生成双端推理 parity 向量：``parity_vectors.json``。

只用标准库 ``math`` 按契约 §5 公式独立实现一遍（不 import quant），
Python 侧 ``predict_proba`` 与 Cloudflare Worker 的 TS 推理都必须与此文件一致
（误差 < 1e-6，契约 §10）。改 ``model.json`` 后重跑本脚本即可。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
MODEL_PATH = FIXTURE_DIR / "model.json"
VECTORS_PATH = FIXTURE_DIR / "parity_vectors.json"

INPUTS = [
    {"f_ret": 0.001, "f_vol": 0.02},
    {"f_ret": 0.02, "f_vol": 0.01},
    {"f_ret": -0.03, "f_vol": 0.06},
    {"f_ret": 0.0, "f_vol": 0.03},
    {"f_ret": 0.012, "f_vol": 0.08},
    {"f_ret": -0.005, "f_vol": 0.015},
    {"f_ret": 0.05, "f_vol": 0.02},
    {"f_ret": -0.02, "f_vol": 0.09},
]


def reference_prob(model: dict, x: dict[str, float]) -> dict[str, float]:
    """按契约 §5 公式的独立实现：z=(x-mean)/scale; scores=z@coef.T+intercept; softmax。"""
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
    probs = [e / total for e in exp]
    return {name: probs[i] for i, name in enumerate(model["classes"])}


def main() -> None:
    model = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    vectors = [
        {
            "input": x,
            "expected": reference_prob(model, x),
        }
        for x in INPUTS
    ]
    payload = {
        "description": "契约 §5 双端推理 parity 向量；Python/TS 都必须复现 expected（误差<1e-6）",
        "model_version": model["version"],
        "classes": model["classes"],
        "features": model["features"],
        "vectors": vectors,
    }
    VECTORS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {VECTORS_PATH} ({len(vectors)} vectors)")


if __name__ == "__main__":
    main()
