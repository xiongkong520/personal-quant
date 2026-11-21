#!/usr/bin/env node
// 校验 schema.sql / migrations/0001_create_signals.sql 与 src/schema.ts 三处 D1 建表语句一致。
import { readFileSync } from "node:fs";
import { SCHEMA_STATEMENTS } from "../src/schema.ts";

const strip = (s) => s.replace(/--[^\n]*/g, " ").replace(/\s+/g, " ").trim().replace(/;$/, "");
const fromSqlFile = (path) => readFileSync(new URL(path, import.meta.url), "utf8").split(";").map(strip).filter(Boolean);

const expected = SCHEMA_STATEMENTS.map(strip);
const files = ["../schema.sql", "../migrations/0001_create_signals.sql"];
let bad = 0;
for (const f of files) {
  const got = fromSqlFile(f);
  const same = got.length === expected.length && got.every((stmt, i) => stmt === expected[i]);
  if (!same) {
    bad += 1;
    console.error(`SCHEMA DRIFT: ${f} 与 src/schema.ts 不一致`);
    const max = Math.max(got.length, expected.length);
    for (let i = 0; i < max; i += 1) if (got[i] !== expected[i]) console.error(`  [${i}] file: ${got[i] ?? "<missing>"}\n       ts:   ${expected[i] ?? "<missing>"}`);
  } else {
    console.log(`schema OK: ${f} (${got.length} statements)`);
  }
}
if (bad) process.exit(1);
