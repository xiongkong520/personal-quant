#!/usr/bin/env node
/**
 * 把 Python 训练产物 data/models/model.json 同步到 worker/models/model.json
 * （Worker / parity 测试使用；契约 §5 / §10）。
 * 训练侧没有产物时保留自造的 §5 占位模型（v0.1.0-fixture），不影响 worker 启动。
 */
import { copyFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const WORKER_DIR = resolve(HERE, "..");
const src = resolve(WORKER_DIR, "../../data/models/model.json");
const dst = resolve(WORKER_DIR, "models/model.json");

if (!existsSync(src)) {
  console.error(`[sync-model] 未找到 Python 模型产物：${src}`);
  console.error("[sync-model] 保留 worker/models/model.json（自造占位模型）；先跑 python -m quant.cli train 再重试。");
  process.exit(1);
}
copyFileSync(src, dst);
const model = JSON.parse(await import("node:fs").then((m) => m.readFileSync(dst, "utf8")));
console.log(`[sync-model] 已同步 ${src} -> ${dst}`);
console.log(`[sync-model] version=${model.version} kind=${model.kind} features=${model.features?.length ?? 0} classes=${(model.classes ?? []).join("/")}`);
