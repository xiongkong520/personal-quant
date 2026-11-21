#!/usr/bin/env node
// 校验 schema.sql / migrations/0001_create_signals.sql 与 src/schema.ts 三处 D1 建表语句一致。
import { readdirSync, readFileSync } from "node:fs";
import { SCHEMA_STATEMENTS } from "../src/schema.ts";

const strip = (s) => s.replace(/--[^\n]*/g, " ").replace(/\s+/g, " ").trim().replace(/;$/, "");
const fromSqlFile = (path) => readFileSync(new URL(path, import.meta.url), "utf8").split(";").map(strip).filter(Boolean);

const expected = SCHEMA_STATEMENTS.map(strip);
// 迁移文件按文件名排序合并（0001 signals + 0002 market ...），与 schema.ts 全量比对
const migrationFiles = readdirSync(new URL("../migrations/", import.meta.url))
  .filter((f) => f.endsWith(".sql"))
  .sort();
const checks = [
  ["../schema.sql", fromSqlFile("../schema.sql")],
  [`migrations (${migrationFiles.join(" + ")})`, migrationFiles.flatMap((f) => fromSqlFile(`../migrations/${f}`))],
];
let bad = 0;
for (const [label, got] of checks) {
  const same = got.length === expected.length && got.every((stmt, i) => stmt === expected[i]);
  if (!same) {
    bad += 1;
    console.error(`SCHEMA DRIFT: ${label} 与 src/schema.ts 不一致`);
    const max = Math.max(got.length, expected.length);
    for (let i = 0; i < max; i += 1) if (got[i] !== expected[i]) console.error(`  [${i}] file: ${got[i] ?? "<missing>"}\n       ts:   ${expected[i] ?? "<missing>"}`);
  } else {
    console.log(`schema OK: ${label} (${got.length} statements)`);
  }
}
if (bad) process.exit(1);
