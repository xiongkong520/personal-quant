/**
 * 契约 §5：softmax 回归双端推理（TypeScript / Workers 侧）
 *
 * 推理公式（必须与 Python `quant.model.predict.predict_proba` 逐字节等价到 1e-6）：
 *   z = (x - mean) / scale
 *   score = z @ coef.T + intercept
 *   p = softmax(score)          # class 顺序与 `classes` 一致
 *
 * 本文件不 import JSON、不 import Node 内置模块，因此：
 *   - 可被 wrangler/esbuild 打进 Worker；
 *   - 可被 Node 24 直接 `import { predictProba } from "../src/model.ts"`（原生 type stripping）做 parity 测试。
 */

export interface QuantModel {
  schema: number;
  version: string;
  kind: string;
  classes: string[];
  features: string[];
  mean: number[];
  scale: number[];
  coef: number[][];
  intercept: number[];
  trained_at?: string;
  train_range?: [string, string];
  horizon?: string;
  label_rule?: string;
  metrics?: Record<string, number>;
}

/** 与契约 §4 的 `prob` 对象一致，键固定为 up/flat/down。 */
export interface Proba {
  up: number;
  flat: number;
  down: number;
}

const PROBA_KEYS = ["up", "flat", "down"] as const;

/** 校验 model.json 结构（§5）。不合法时抛错，用于启动自检与 parity 测试。 */
export function validateModel(model: QuantModel): void {
  if (!model || typeof model !== "object") throw new Error("model 必须是对象");
  if (model.schema !== 1) throw new Error(`model.schema 必须为 1，实际 ${String(model.schema)}`);
  if (model.kind !== "softmax_regression") throw new Error(`model.kind 必须为 softmax_regression，实际 ${String(model.kind)}`);
  if (!Array.isArray(model.classes) || model.classes.length < 2) throw new Error("model.classes 缺失");
  if (!Array.isArray(model.features) || model.features.length === 0) throw new Error("model.features 为空");
  const nFeat = model.features.length;
  const nClass = model.classes.length;
  const numeric = (arr: unknown, name: string, len: number): number[] => {
    if (!Array.isArray(arr) || arr.length !== len) throw new Error(`model.${name} 长度必须为 ${len}`);
    return arr.map((v, i) => {
      const num = typeof v === "number" ? v : Number(v);
      if (!Number.isFinite(num)) throw new Error(`model.${name}[${i}] 非有限数`);
      return num;
    });
  };
  numeric(model.mean, "mean", nFeat);
  const scale = numeric(model.scale, "scale", nFeat);
  scale.forEach((s, i) => {
    if (!Number.isFinite(s) || s === 0) throw new Error(`model.scale[${i}] 不能为 0`);
  });
  if (!Array.isArray(model.coef) || model.coef.length !== nClass) {
    throw new Error(`model.coef 行数必须为 class 数 ${nClass}`);
  }
  model.coef.forEach((row, ci) => numeric(row, `coef[${ci}]`, nFeat));
  numeric(model.intercept, "intercept", nClass);
  if (!model.classes.every((c) => (PROBA_KEYS as readonly string[]).includes(c))) {
    throw new Error(`model.classes 只能包含 ${PROBA_KEYS.join("/")}，实际 ${model.classes.join(",")}`);
  }
  if (new Set(model.classes).size !== model.classes.length) throw new Error("model.classes 有重复项");
}

/** z = (x - mean) / scale；缺失的特征按 0 处理（未标准化前）。 */
export function standardize(model: QuantModel, x: Record<string, number>): number[] {
  const z = new Array<number>(model.features.length);
  for (let j = 0; j < model.features.length; j += 1) {
    const raw = x[model.features[j]];
    const v = typeof raw === "number" && Number.isFinite(raw) ? raw : 0;
    const mean = Number(model.mean[j] ?? 0);
    const scale = Number(model.scale[j] ?? 1);
    z[j] = (v - mean) / (scale === 0 || !Number.isFinite(scale) ? 1 : scale);
  }
  return z;
}

/** score_c = sum_j coef[c][j] * z[j] + intercept[c]。 */
export function linearScores(model: QuantModel, z: number[]): number[] {
  const out = new Array<number>(model.classes.length);
  for (let c = 0; c < model.classes.length; c += 1) {
    const row = model.coef[c];
    let acc = Number(model.intercept[c] ?? 0);
    for (let j = 0; j < z.length; j += 1) acc += Number(row[j]) * z[j];
    out[c] = acc;
  }
  return out;
}

/** 数值稳定 softmax（先减 max，与 sklearn 一致）。 */
export function softmax(scores: number[]): number[] {
  if (scores.length === 0) throw new Error("softmax 输入为空");
  let max = -Infinity;
  for (const s of scores) max = Math.max(max, s);
  let sum = 0;
  const exps = scores.map((s) => {
    const e = Math.exp(s - max);
    sum += e;
    return e;
  });
  return exps.map((e) => e / sum);
}

/** 按 `classes` 顺序返回概率数组（便于逐元素对照 Python）。 */
export function predictOrdered(model: QuantModel, x: Record<string, number>): number[] {
  validateModel(model);
  return softmax(linearScores(model, standardize(model, x)));
}

/** 契约 §4 `prob`：{"up":p,"flat":p,"down":p}，class 索引由 model.classes 决定。 */
export function predictProba(model: QuantModel, x: Record<string, number>): Proba {
  const probs = predictOrdered(model, x);
  const out = { up: 0, flat: 0, down: 0 };
  model.classes.forEach((cls, i) => {
    if ((PROBA_KEYS as readonly string[]).includes(cls)) out[cls as (typeof PROBA_KEYS)[number]] = probs[i];
  });
  for (const k of PROBA_KEYS) {
    if (!Number.isFinite(out[k])) throw new Error(`predictProba: ${k} 非有限数`);
  }
  return out;
}

/** 契约 §4 `direction` = argmax(prob)。 */
export function directionOf(prob: Proba): "up" | "flat" | "down" {
  let best: "up" | "flat" | "down" = "flat";
  let bestP = -Infinity;
  for (const k of PROBA_KEYS) {
    if (prob[k] > bestP) {
      bestP = prob[k];
      best = k;
    }
  }
  return best;
}

/** 契约 §3.3 `confidence` = max(prob)，保留 4 位。 */
export function confidenceOf(prob: Proba): number {
  return Math.round(Math.max(prob.up, prob.flat, prob.down) * 1e4) / 1e4;
}
