/**
 * 轻量 LightGBM 模型推理（云端，零依赖）。
 *
 * 模型来源：GitHub 仓库 model/latest_model.json（training/export_model.py 产物，约 325KB）。
 * 树编码（与 model/README 约定一致）：
 *   - left_child/right_child >= 0 → 内部节点下标
 *   - < 0 → 叶子：leaf_idx = -child - 1，值取 leaf_value[leaf_idx]
 *   - 每棵树带 class_index（0=up, 1=flat, 2=down），raw[class_index] += tree(x)
 *   - 输出 softmax(raw) → 三分类概率
 * 特征需按 features[].index 顺序传入（缺失按 0，winsorize/normalize 由调用方处理）。
 */
export interface LgbTree {
  split_feature: number[];
  threshold: number[];
  decision_type: string[];
  left_child: number[];
  right_child: number[];
  leaf_value: number[];
  class_index: number;
}

export interface LgbModel {
  schema: number;
  model: "lightgbm";
  version: string;
  trained_at?: string;
  classes?: string[];
  num_class: number;
  features: Array<{ name: string; index: number; dtype?: string }>;
  feature_config?: Record<string, unknown>;
  trees: LgbTree[];
  objective?: string;
  metrics?: Record<string, unknown>;
}

const MODEL_RAW_URL =
  "https://raw.githubusercontent.com/xiongkong520/personal-quant/main/model/latest_model.json";
const MODEL_CACHE_KEY = "https://quant-edge-cache.local/model/latest_model.json";
const MODEL_CACHE_TTL = 86400;

export function parseModel(json: unknown): LgbModel {
  const model = json as LgbModel;
  if (!model || model.model !== "lightgbm" || !Array.isArray(model.trees)) {
    throw new Error("模型 JSON 格式不正确");
  }
  if (model.num_class !== 3) throw new Error("仅支持三分类模型");
  return model;
}

/** 单棵树求值：返回该树的叶子值。 */
export function evalTree(tree: LgbTree, features: number[]): number {
  let node = 0;
  for (let guard = 0; guard < 4096; guard += 1) {
    const left = tree.left_child[node];
    const right = tree.right_child[node];
    if (left === undefined || right === undefined) return 0;
    const featureIndex = tree.split_feature[node];
    const value = Number.isFinite(features[featureIndex]) ? features[featureIndex] : 0;
    const decision = String(tree.decision_type[node] ?? "<=");
    const goLeft = decision.includes("<=") ? value <= tree.threshold[node] : value > tree.threshold[node];
    const child = goLeft ? left : right;
    if (child < 0) {
      const leafIndex = -child - 1;
      return Number(tree.leaf_value[leafIndex] ?? 0);
    }
    node = child;
  }
  return 0;
}

export interface PredictResult {
  prob: { up: number; flat: number; down: number };
  raw: [number, number, number];
  direction: "up" | "flat" | "down";
  confidence: number;
}

/** 三分类推理：raw[class] 累加 → softmax → 方向/置信度。 */
export function predictProba(model: LgbModel, features: number[]): PredictResult {
  const raw: [number, number, number] = [0, 0, 0];
  for (const tree of model.trees) {
    const classIndex = Number(tree.class_index ?? 0);
    if (classIndex >= 0 && classIndex < 3) raw[classIndex] += evalTree(tree, features);
  }
  const max = Math.max(raw[0], raw[1], raw[2]);
  const exps = raw.map((v) => Math.exp(v - max));
  const sum = exps[0] + exps[1] + exps[2];
  const prob = { up: exps[0] / sum, flat: exps[1] / sum, down: exps[2] / sum };
  const order: Array<"up" | "flat" | "down"> = ["up", "flat", "down"];
  const values = [prob.up, prob.flat, prob.down];
  let best = 0;
  for (let i = 1; i < 3; i += 1) if (values[i] > values[best]) best = i;
  return {
    prob: {
      up: Math.round(prob.up * 10000) / 10000,
      flat: Math.round(prob.flat * 10000) / 10000,
      down: Math.round(prob.down * 10000) / 10000,
    },
    raw: [raw[0], raw[1], raw[2]],
    direction: order[best],
    confidence: Math.round(values[best] * 10000) / 10000,
  };
}

/** 加载模型（Cache API 24h → GitHub raw 兜底）。 */
export async function loadLatestModel(): Promise<LgbModel | null> {
  try {
    const cached = await caches.default.match(MODEL_CACHE_KEY);
    if (cached) return parseModel(await cached.json());
  } catch {
    /* ignore */
  }
  try {
    const resp = await fetch(MODEL_RAW_URL, {
      headers: { "user-agent": "quant-worker/1.0" },
      signal: AbortSignal.timeout(15000),
    });
    if (!resp.ok) return null;
    const json = await resp.json();
    const model = parseModel(json);
    await caches.default.put(
      MODEL_CACHE_KEY,
      new Response(JSON.stringify(json), {
        headers: { "content-type": "application/json; charset=utf-8", "cache-control": `public, max-age=${MODEL_CACHE_TTL}` },
      }),
    );
    return model;
  } catch {
    return null;
  }
}
