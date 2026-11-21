/**
 * 最小 D1 binding 兼容层：用 Node 24 内置的 node:sqlite 实现 Worker 代码用到的 D1 API 子集。
 *
 * 用途：在 workerd / wrangler 无法运行的环境（例如 Android proot、39-bit 虚拟地址空间的手机）
 * 里，仍然可以用同一份 src/*.ts 代码做端到端验证与前端联调。
 * 只实现 Worker 代码实际使用的接口：prepare / bind / first / all / run / batch / exec。
 */
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";

function normalizeParam(value) {
  if (value === undefined) return null;
  if (typeof value === "boolean") return value ? 1 : 0;
  return value;
}

class PreparedStatementShim {
  constructor(db, sql, params = []) {
    this.db = db;
    this.sql = sql;
    this.params = params;
  }

  bind(...params) {
    return new PreparedStatementShim(this.db, this.sql, params.map(normalizeParam));
  }

  async first(columnName) {
    const row = this.db.prepare(this.sql).get(...this.params);
    if (row === undefined || row === null) return null;
    if (columnName) return row[columnName] ?? null;
    return row;
  }

  async all() {
    const results = this.db.prepare(this.sql).all(...this.params);
    return {
      success: true,
      results,
      meta: { duration: 0, changes: 0, last_row_id: 0, rows_read: results.length, rows_written: 0, size_after: 0 },
    };
  }

  async run() {
    const info = this.db.prepare(this.sql).run(...this.params);
    return {
      success: true,
      results: [],
      meta: {
        duration: 0,
        changes: Number(info.changes ?? 0),
        last_row_id: Number(info.lastInsertRowid ?? 0),
        rows_read: 0,
        rows_written: Number(info.changes ?? 0),
        size_after: 0,
      },
    };
  }
}

class D1DatabaseShim {
  constructor(path) {
    mkdirSync(dirname(path), { recursive: true });
    this.db = new DatabaseSync(path);
    this.db.exec("PRAGMA journal_mode = WAL;");
    this.db.exec("PRAGMA foreign_keys = ON;");
  }

  prepare(sql) {
    return new PreparedStatementShim(this.db, sql);
  }

  async batch(statements) {
    const results = [];
    this.db.exec("BEGIN");
    try {
      for (const statement of statements) results.push(await statement.run());
      this.db.exec("COMMIT");
    } catch (err) {
      try {
        this.db.exec("ROLLBACK");
      } catch {
        /* ignore */
      }
      throw err;
    }
    return results;
  }

  async exec(sql) {
    this.db.exec(sql);
    return [];
  }

  close() {
    try {
      this.db.close();
    } catch {
      /* ignore */
    }
  }
}

export function createD1(path) {
  return new D1DatabaseShim(path);
}
