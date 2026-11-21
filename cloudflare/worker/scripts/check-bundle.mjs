#!/usr/bin/env node
/**
 * 打包自检：用 esbuild 以 Workers 的方式把 src/index.ts 打成 ESM bundle（不启动 workerd）。
 * 用途：在 workerd 无法运行的环境里仍能验证 TS/JSON import/依赖图可被正确打包。
 * 标准校验仍是 wrangler build/dev；本脚本是无需 Cloudflare 账号、无需 workerd 的快速烟测。
 */
import { build } from "esbuild";
import { statSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const WORKER_DIR = resolve(HERE, "..");
const outfile = resolve(WORKER_DIR, "dist/index.mjs");

rmSync(resolve(WORKER_DIR, "dist"), { recursive: true, force: true });

await build({
  entryPoints: [resolve(WORKER_DIR, "src/index.ts")],
  absWorkingDir: WORKER_DIR,
  outfile,
  bundle: true,
  format: "esm",
  platform: "neutral",
  target: "es2022",
  conditions: ["workerd", "worker", "browser"],
  logLevel: "warning",
  metafile: false,
});

const size = statSync(outfile).size;
console.log(`BUNDLE OK: ${outfile} (${(size / 1024).toFixed(1)} KiB)`);
