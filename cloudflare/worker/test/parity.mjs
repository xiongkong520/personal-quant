#!/usr/bin/env node
/**
 * 契约 §10：双端推理 parity 测试（Node 直跑，不依赖 npm install）
 *
 *   node test/parity.mjs
 *
 * 做什么：
 *   1. 自动配对 model.json 与 parity cases：
 *        a. tests/fixtures/model/parity_vectors.json + tests/fixtures/model/model.json（model-dev 固定 fixture，契约 §10 的官方对照）
 *        b. tests/fixtures/model/parity_cases.json + data/models/model.json（真实训练产物 + 对应用例，训练后可用）
 *        c. data/models/model.json + 自动合成输入（无 cases 时也验证 Python vs TS，输入由 mean/scale 生成）
 *        d. worker/models/model.json + worker/test/fixtures/parity_cases.json（自造 §5 占位模型，期望值由 Python 算出）
 *      也支持环境变量 QUANT_MODEL_JSON / QUANT_PARITY_CASES 显式指定。
 *   2. 用 worker/src/model.ts 的 predictProba 推理，逐条与 expected 比对（默认容差 1e-6，契约 §10）。
 *   3. case 没有 expected 时调用本地 Python 参考实现：
 *        优先 `quant.model.predict.predict_proba`（PYTHONPATH=src），失败则回退 Python 内联的 §5 公式实现。
 *
 * 退出码：0 通过；1 不通过（打印明细）。
 * 环境变量：QUANT_MODEL_JSON / QUANT_PARITY_CASES / PARITY_TOL
 */
import { readFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { predictProba, validateModel } from "../src/model.ts";

const HERE = dirname(fileURLToPath(import.meta.url)); // cloudflare/worker/test
const WORKER_DIR = resolve(HERE, ".."); // cloudflare/worker
const PROJECT_ROOT = resolve(WORKER_DIR, "../.."); // 仓库根
const FIXTURE_DIR = resolve(PROJECT_ROOT, "tests/fixtures/model");
const DEFAULT_TOL = 1e-6;

const log = (...args) => console.log(...args);
const fail = (msg) => {
  console.error(`PARITY FAIL: ${msg}`);
  process.exitCode = 1;
};

const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));

function pickFirstExisting(candidates) {
  for (const p of candidates) if (p && existsSync(p)) return p;
  return null;
}

// ---------------------------------------------------------------- pairing ----

/**
 * 候选配对，按优先级。每项：{model, cases|null, label}
 * cases=null 表示自动合成输入（需要 Python 计算期望值）。
 */
function buildPairs() {
  const selfModel = resolve(WORKER_DIR, "models/model.json");
  const selfCases = resolve(HERE, "fixtures/parity_cases.json");
  const realModel = resolve(PROJECT_ROOT, "data/models/model.json");
  const fixtureModel = resolve(FIXTURE_DIR, "model.json");
  const fixtureVectors = pickFirstExisting([resolve(FIXTURE_DIR, "parity_vectors.json")]);
  const realCases = pickFirstExisting([resolve(FIXTURE_DIR, "parity_cases.json")]);
  const pairs = [];

  if (process.env.QUANT_MODEL_JSON || process.env.QUANT_PARITY_CASES) {
    const modelPath = process.env.QUANT_MODEL_JSON || pickFirstExisting([realModel, selfModel]);
    let casesPath = process.env.QUANT_PARITY_CASES || null;
    if (!casesPath) {
      // 未显式给 cases 时，只选 features 能对上的候选；都不匹配则用自动合成输入
      for (const candidate of [realCases, fixtureVectors, selfCases]) {
        if (candidate && existsSync(candidate) && casesFeaturesMatchModel(candidate, modelPath)) {
          casesPath = candidate;
          break;
        }
      }
    }
    pairs.push({ model: modelPath, cases: casesPath, label: "env override" });
  }
  // a. model-dev 固定 fixture（契约 §10 官方对照：fixture model + parity vectors）
  if (fixtureModel && fixtureVectors) pairs.push({ model: fixtureModel, cases: fixtureVectors, label: "model-dev fixture" });
  // b. 真实训练模型 + 同名 parity_cases.json
  if (realModel && realCases) pairs.push({ model: realModel, cases: realCases, label: "trained model + parity_cases" });
  // c. 真实训练模型 + 自动合成输入（无用例也测）
  if (existsSync(realModel)) pairs.push({ model: realModel, cases: null, label: "trained model + synthesized cases" });
  // d. 自造占位模型 + 自造 fixture
  pairs.push({ model: selfModel, cases: selfCases, label: "self fixture" });
  return pairs;
}

/** 判断一个 cases 文件声明的 features 是否与给定 model 匹配（无声明视为匹配）。 */
function casesFeaturesMatchModel(casesPath, modelPath) {
  try {
    const casesRaw = readJson(casesPath);
    const model = readJson(modelPath);
    const declared = casesRaw && typeof casesRaw === "object" && !Array.isArray(casesRaw) ? casesRaw.features : null;
    if (!Array.isArray(declared)) return true;
    return sameStringList(declared, model.features);
  } catch {
    return false;
  }
}

function sameStringList(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => String(v) === String(b[i]));
}

/** 尝试一个候选配对，模型/用例不匹配时返回 {skip: reason}。 */
function tryPair(pair) {
  if (!pair.model || !existsSync(pair.model)) return { skip: `model 不存在：${pair.model}` };
  let model;
  try {
    model = readJson(pair.model);
    validateModel(model);
  } catch (err) {
    return { error: `${pair.model} 不符合契约 §5：${err.message}` };
  }
  if (!pair.cases) return { model, modelPath: pair.model, casesRaw: null, casesPath: null };
  if (!existsSync(pair.cases)) return { skip: `cases 不存在：${pair.cases}` };
  let casesRaw;
  try {
    casesRaw = readJson(pair.cases);
  } catch (err) {
    return { error: `${pair.cases} 不是合法 JSON：${err.message}` };
  }
  // 用例文件声明了 model_version 时校验一致（避免拿旧 fixture 套新模型造成假失败）
  const declaredVersion = casesRaw && typeof casesRaw === "object" && !Array.isArray(casesRaw) ? casesRaw.model_version ?? casesRaw.modelVersion : null;
  if (declaredVersion && model.version && String(declaredVersion) !== String(model.version)) {
    return { skip: `cases model_version=${declaredVersion} 与 model.version=${model.version} 不一致（fixture 属于别的模型版本）` };
  }
  // 用例自带 features/classes 时校验与模型一致；不一致就尝试用例同目录的 model.json
  const declaredFeatures = casesRaw && typeof casesRaw === "object" && !Array.isArray(casesRaw) ? casesRaw.features : null;
  if (Array.isArray(declaredFeatures) && !sameStringList(declaredFeatures, model.features)) {
    const sibling = resolve(dirname(pair.cases), "model.json");
    if (sibling !== pair.model && existsSync(sibling)) {
      try {
        const siblingModel = readJson(sibling);
        if (sameStringList(declaredFeatures, siblingModel.features)) {
          return { model: siblingModel, modelPath: sibling, casesRaw, casesPath: pair.cases };
        }
      } catch {
        /* fallthrough */
      }
    }
    return { skip: `${pair.cases} 的 features 与 ${pair.model} 不一致（可能是别的模型的用例）` };
  }
  return { model, modelPath: pair.model, casesRaw, casesPath: pair.cases };
}

// ---------------------------------------------------------------- 用例 ----

function vectorToFeatures(model, vec) {
  if (vec.length !== model.features.length) {
    throw new Error(`特征向量长度 ${vec.length} != model.features 长度 ${model.features.length}`);
  }
  const out = {};
  model.features.forEach((f, j) => {
    out[f] = Number(vec[j]);
  });
  return out;
}

/** 兼容几种可能的 fixture 形态（见 README）。 */
function normalizeCases(model, raw) {
  let list = raw;
  if (!Array.isArray(raw)) {
    list = raw.cases ?? raw.items ?? raw.inputs ?? raw.vectors ?? raw.samples ?? raw.data;
  }
  if (!Array.isArray(list)) throw new Error("parity cases 结构不认识（需要数组或 {cases|items|inputs|vectors:[...]}）");
  return list.map((item, i) => {
    if (Array.isArray(item)) return { name: `case_${i}`, features: vectorToFeatures(model, item), expected: null };
    const featuresRaw = item.features ?? item.feature ?? item.x ?? item.input ?? item.vector;
    if (featuresRaw == null) throw new Error(`case[${i}] 缺少 features/input 字段`);
    const features = Array.isArray(featuresRaw) ? vectorToFeatures(model, featuresRaw) : { ...featuresRaw };
    const expectedRaw =
      item.expected ?? item.expected_proba ?? item.proba ?? item.probabilities ?? item.output ?? item.expected_output ?? null;
    let expected = null;
    if (expectedRaw != null) {
      if (Array.isArray(expectedRaw)) {
        expected = {};
        model.classes.forEach((cls, j) => {
          expected[cls] = Number(expectedRaw[j]);
        });
      } else {
        expected = { up: Number(expectedRaw.up), flat: Number(expectedRaw.flat), down: Number(expectedRaw.down) };
      }
      for (const k of ["up", "flat", "down"]) {
        if (!Number.isFinite(expected[k])) throw new Error(`case[${i}].expected.${k} 缺失或非数`);
      }
    }
    return { name: item.name ?? item.id ?? `case_${i}`, features, expected };
  });
}

/** 无 cases 时按 mean/scale 生成确定性输入（覆盖 softmax 各分支，含极端值）。 */
function synthesizeCases(model) {
  const patterns = [
    () => 0,
    (j) => Math.sin(j + 1) * 1.0,
    (j) => Math.cos(j * 2 + 0.5) * 1.5,
    (j) => (j % 5) - 2,
    (j) => 2.5,
    (j) => -2.5,
    (j) => ((j * 7) % 11) / 5 - 1,
    (j) => 0.25,
  ];
  return patterns.map((pattern, i) => {
    const features = {};
    model.features.forEach((f, j) => {
      const mean = Number(model.mean[j] ?? 0);
      const scale = Number(model.scale[j] ?? 1) || 1;
      features[f] = mean + pattern(j) * scale;
    });
    return { name: `synth_${i}`, features, expected: null };
  });
}

// ------------------------------------------------- Python 参考实现（期望值） ----

const PY_INLINE_FORMULA = String.raw`
import json, math, sys

def reference(model, x):
    features = model["features"]
    z = []
    for j in range(len(features)):
        v = x.get(features[j], 0.0)
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = 0.0
        if not math.isfinite(v):
            v = 0.0
        mean = float(model["mean"][j])
        scale = float(model["scale"][j]) or 1.0
        z.append((v - mean) / scale)
    scores = []
    for row, b in zip(model["coef"], model["intercept"]):
        acc = float(b)
        for c, zz in zip(row, z):
            acc += float(c) * zz
        scores.append(acc)
    mx = max(scores)
    ex = [math.exp(s - mx) for s in scores]
    tot = sum(ex)
    probs = [e / tot for e in ex]
    return {cls: probs[i] for i, cls in enumerate(model["classes"])}

payload = json.load(sys.stdin)
out = [reference(payload["model"], x) for x in payload["inputs"]]
print(json.dumps(out))
`;

function pythonReference(model, inputs) {
  const payload = JSON.stringify({ model, inputs });
  const env = { ...process.env, PYTHONPATH: resolve(PROJECT_ROOT, "src") };

  // 3a. 先尝试 model-dev 的 Python 实现（真正的双端 parity）
  if (existsSync(resolve(PROJECT_ROOT, "src/quant/model/predict.py"))) {
    const pyImport = String.raw`
import json, sys
from quant.model.predict import predict_proba
payload = json.load(sys.stdin)
out = []
for x in payload["inputs"]:
    p = predict_proba(payload["model"], x)
    out.append({"up": p["up"], "flat": p["flat"], "down": p["down"]})
print(json.dumps(out))
`;
    try {
      const stdout = execFileSync("python3", ["-c", pyImport], { input: payload, env, timeout: 180_000, stdio: ["pipe", "pipe", "pipe"] });
      return { probs: JSON.parse(String(stdout)), source: "python:quant.model.predict.predict_proba" };
    } catch (err) {
      log(`[parity] 调用 quant.model.predict.predict_proba 失败，回退 §5 公式的 Python 参考实现：${String(err.message).split("\n")[0]}`);
    }
  }

  // 3b. 回退：Python 内联 §5 公式（仍与 TS 实现独立）
  const stdout = execFileSync("python3", ["-c", PY_INLINE_FORMULA], { input: payload, env, timeout: 180_000, stdio: ["pipe", "pipe", "pipe"] });
  return { probs: JSON.parse(String(stdout)), source: "python:§5-inline-formula" };
}

// ---------------------------------------------------------------- main ----

/** 计算单个配对的结果（verbose=false 时不打印逐 case 明细）。 */
function evaluatePair(selected, verbose) {
  let cases;
  try {
    cases = selected.casesRaw ? normalizeCases(selected.model, selected.casesRaw) : synthesizeCases(selected.model);
  } catch (err) {
    return { error: `${selected.casesPath}: ${err.message}` };
  }
  if (cases.length === 0) return { error: "parity cases 为空" };

  // 期望值：fixture 未提供 expected 时用 Python 参考实现补
  let expectedSource = selected.casesRaw ? "fixture" : "python";
  const missingExpected = cases.filter((c) => !c.expected);
  if (missingExpected.length > 0) {
    try {
      const ref = pythonReference(selected.model, missingExpected.map((c) => c.features));
      missingExpected.forEach((c, i) => {
        c.expected = ref.probs[i];
      });
      expectedSource = ref.source;
    } catch (err) {
      return { error: `case 缺少 expected 且 Python 参考实现不可用：${String(err.message).split("\n")[0]}` };
    }
  }

  const tol = Number(
    process.env.PARITY_TOL ||
      (selected.casesRaw && !Array.isArray(selected.casesRaw) ? selected.casesRaw.tolerance : 0) ||
      DEFAULT_TOL,
  );
  if (verbose) {
    log(`[parity] pair       = ${selected.label}`);
    log(`[parity] model      = ${selected.modelPath} (version=${selected.model.version}, features=${selected.model.features.length})`);
    log(`[parity] cases      = ${selected.casesPath ?? "<synthesized from model mean/scale>"} (${cases.length} cases)`);
    log(`[parity] expected   = ${expectedSource}`);
    log(`[parity] tolerance  = ${tol}`);
  }
  let maxDiff = 0;
  const failures = [];
  for (const c of cases) {
    const got = predictProba(selected.model, c.features);
    let caseMax = 0;
    for (const k of ["up", "flat", "down"]) caseMax = Math.max(caseMax, Math.abs(got[k] - c.expected[k]));
    maxDiff = Math.max(maxDiff, caseMax);
    const sum = got.up + got.flat + got.down;
    const sumOk = Math.abs(sum - 1) <= 1e-9;
    const ok = caseMax <= tol && sumOk;
    if (!ok) failures.push({ case: c, got, caseMax, sum, sumOk });
    else if (verbose) log(`  ✓ ${String(c.name).padEnd(14)} max|Δ|=${caseMax.toExponential(3)}`);
  }

  return { cases, maxDiff, failures, tol, expectedSource };
}

const allPairs = buildPairs();
const onlyEnvOverride = Boolean(process.env.QUANT_MODEL_JSON || process.env.QUANT_PARITY_CASES);
const resolved = [];
for (const pair of allPairs) {
  const result = tryPair(pair);
  if (result.error) {
    fail(`候选配对失败（${pair.label}）：${result.error}`);
    continue;
  }
  if (result.skip) {
    log(`[parity] 跳过候选（${pair.label}）：${result.skip}`);
    continue;
  }
  resolved.push({ ...result, label: pair.label });
}
if (resolved.length === 0) {
  fail("找不到可用的 model + cases 配对（检查 cloudflare/worker/models/model.json 与 tests/fixtures/model/）");
  process.exit(1);
}
// 显式指定 QUANT_* 时只跑指定的那一组；否则跑所有能解析的配对（fixture + 真实模型 + 占位模型）
const selectedPairs = onlyEnvOverride ? resolved.slice(0, 1) : resolved;
// 主配对（输出最详细）= 优先级最高的一组；其余配对静默校验
const [primary, ...extras] = selectedPairs;

log(`[parity] 共 ${selectedPairs.length} 组候选配对`);
const primaryResult = evaluatePair(primary, true);
if (primaryResult.error) {
  fail(primaryResult.error);
  process.exit(1);
}
if (primaryResult.failures.length > 0) {
  for (const f of primaryResult.failures) {
    console.error(`  ✗ ${f.case.name}: max|Δ|=${f.caseMax.toExponential(3)} sum=${f.sum}`);
    console.error(
      `      got  up=${f.got.up} flat=${f.got.flat} down=${f.got.down}\n      want up=${f.case.expected.up} flat=${f.case.expected.flat} down=${f.case.expected.down}`,
    );
    if (!f.sumOk) console.error(`      prob 和 = ${f.sum}（应为 1 ± 1e-9）`);
  }
}

let totalCases = primaryResult.cases.length;
let overallMax = primaryResult.maxDiff;
let failedPairs = primaryResult.failures.length > 0 ? 1 : 0;
log(`PARITY ${primaryResult.failures.length === 0 ? "OK" : "FAIL"}: ${primaryResult.cases.length} cases, max|Δ|=${primaryResult.maxDiff.toExponential(3)} (tolerance ${primaryResult.tol}), model=${primary.model.version}, pair=${primary.label}`);

for (const extra of extras) {
  const result = evaluatePair(extra, false);
  if (result.error) {
    fail(`附加配对（${extra.label}）：${result.error}`);
    continue;
  }
  totalCases += result.cases.length;
  overallMax = Math.max(overallMax, result.maxDiff);
  if (result.failures.length > 0) {
    failedPairs += 1;
    for (const f of result.failures) {
      console.error(
        `  ✗ [${extra.label}] ${f.case.name}: max|Δ|=${f.caseMax.toExponential(3)} got up=${f.got.up} flat=${f.got.flat} down=${f.got.down} want up=${f.case.expected.up} flat=${f.case.expected.flat} down=${f.case.expected.down}`,
      );
    }
    fail(`附加配对（${extra.label}）${result.failures.length}/${result.cases.length} cases 超差，max|Δ|=${result.maxDiff.toExponential(3)} > ${result.tol}`);
  } else {
    log(`  ✓ 附加配对 ${extra.label}: ${result.cases.length} cases max|Δ|=${result.maxDiff.toExponential(3)} model=${extra.model.version}`);
  }
}

if (failedPairs > 0) {
  fail(`${failedPairs}/${selectedPairs.length} 组配对失败`);
  process.exit(1);
}
log(`PARITY OK: ${selectedPairs.length} 组配对 / ${totalCases} cases, 整体 max|Δ|=${overallMax.toExponential(3)}`);
