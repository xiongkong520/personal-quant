#!/usr/bin/env python3
"""旧 SQLite 库 → 新存储（Parquet 行情 + DuckDB 状态库）一次性迁移脚本。

用法::

    python scripts/migrate_storage.py                     # 迁移 data/db/quant.db
    python scripts/migrate_storage.py --sqlite data/db/quant.db --db data/db/quant.duckdb

迁移内容（契约 §3 / §9）
------------------------
- ``daily_bars``  → ``<parquet_dir>/daily_bars/symbol=<code>/daily_bars.parquet``
- ``minute_bars`` → ``<parquet_dir>/minute_bars/symbol=<code>/period=<p>/minute_bars.parquet``
- ``signals``     → DuckDB ``signals`` 表
- ``paper_*``     → DuckDB 同名表（paper_account/positions/orders/trades/equity_curve）

幂等性
------
- 行情：按主键 upsert（同主键旧值覆盖新值，迁移以旧库为准）；
- 状态表：``ON CONFLICT DO NOTHING``——若新库已有（例如迁移前跑过 paper/signal），
  保留新数据，只补缺失行，避免把新状态回滚成旧状态；
- 可重复执行，行数不会翻倍。

退出码：迁移校验全部通过 0，否则 1（旧库不存在视作无需迁移，返回 0 并打印 skipped）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

import duckdb  # noqa: E402

from quant.config import cfg_path, load_config  # noqa: E402
from quant.data import db as dbmod  # noqa: E402
from quant.data import parquet_store, store  # noqa: E402
from quant.data.models import DAILY_COLUMNS, MINUTE_COLUMNS  # noqa: E402
from quant.paper import engine  # noqa: E402

#: 状态表主键（用于去重/校验）
_STATE_PK: dict[str, tuple[str, ...]] = {
    "signals": ("signal_id",),
    "paper_account": ("id",),
    "paper_positions": ("symbol",),
    "paper_orders": ("order_id",),
    "paper_trades": ("trade_id",),
    "paper_equity_curve": ("ts",),
}
_PAPER_TABLES = (
    "paper_account",
    "paper_positions",
    "paper_orders",
    "paper_trades",
    "paper_equity_curve",
)


# ---------------------------------------------------------------------------
# SQLite 读取
# ---------------------------------------------------------------------------
def _q(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _sqlite_tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [str(r[0]) for r in rows if not str(r[0]).startswith("sqlite_")]


def _sqlite_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in con.execute(f"PRAGMA table_info({_q(table)})").fetchall()]


def _sqlite_rows(con: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    cursor = con.execute(f"SELECT * FROM {_q(table)}")
    columns = [str(desc[0]) for desc in cursor.description or ()]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _frame(rows: list[dict[str, Any]], columns: tuple[str, ...] | list[str]) -> pd.DataFrame:
    """按列投影成 DataFrame（旧库缺列补 None，多余列丢弃）。"""
    if not rows:
        return pd.DataFrame(columns=list(columns))
    frame = pd.DataFrame(rows)
    for column in columns:
        if column not in frame.columns:
            frame[column] = None
    return frame[list(columns)]


# ---------------------------------------------------------------------------
# 迁移：行情 → Parquet
# ---------------------------------------------------------------------------
def _migrate_daily(
    sqlite: sqlite3.Connection, tables: list[str], db_file: Path
) -> dict[str, Any]:
    if "daily_bars" not in tables:
        return {"present": False, "source_rows": 0, "written_rows": 0}
    rows = _sqlite_rows(sqlite, "daily_bars")
    frame = _frame(rows, DAILY_COLUMNS)
    written = store.upsert_daily(frame, db_file) if len(frame) else 0
    return {"present": True, "source_rows": len(rows), "written_rows": written}


def _migrate_minute(
    sqlite: sqlite3.Connection, tables: list[str], db_file: Path
) -> dict[str, Any]:
    if "minute_bars" not in tables:
        return {"present": False, "source_rows": 0, "written_rows": 0}
    rows = _sqlite_rows(sqlite, "minute_bars")
    frame = _frame(rows, MINUTE_COLUMNS)
    written = store.upsert_minute(frame, db_file) if len(frame) else 0
    return {"present": True, "source_rows": len(rows), "written_rows": written}


# ---------------------------------------------------------------------------
# 迁移：状态 → DuckDB
# ---------------------------------------------------------------------------
def _insert_state_rows(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    rows: list[dict[str, Any]],
    columns: list[str],
) -> int:
    """向 DuckDB 状态表插入行（主键冲突保留已有新数据）。"""
    if not rows or not columns:
        return 0
    pk = _STATE_PK.get(table, ())
    placeholders = ", ".join("?" for _ in columns)
    col_sql = ", ".join(_q(c) for c in columns)
    conflict = f" ON CONFLICT ({', '.join(_q(c) for c in pk)}) DO NOTHING" if pk else ""
    sql = f"INSERT INTO {_q(table)} ({col_sql}) VALUES ({placeholders}){conflict}"
    payload = [tuple(row.get(c) for c in columns) for row in rows]
    with dbmod.transaction(conn):
        conn.executemany(sql, payload)
    return len(payload)


def _migrate_state(
    sqlite: sqlite3.Connection,
    tables: list[str],
    db_file: Path,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with dbmod.connect(db_file) as conn:
        engine.ensure_paper_tables(conn)  # paper_* 表（含旧库只增列兼容）
        for table in ("signals",) + _PAPER_TABLES:
            if table not in tables:
                result[table] = {"present": False, "source_rows": 0, "written_rows": 0}
                continue
            rows = _sqlite_rows(sqlite, table)
            target_columns = dbmod.columns(conn, table)
            source_columns = set(_sqlite_columns(sqlite, table))
            # 只迁移目标表存在的列；旧库缺失列由 DuckDB 默认值/NULL 补齐
            columns = [c for c in target_columns if c in source_columns]
            written = _insert_state_rows(conn, table, rows, columns)
            dropped = sorted(source_columns - set(target_columns))
            result[table] = {
                "present": True,
                "source_rows": len(rows),
                "written_rows": written,
                "columns": columns,
                "dropped_columns": dropped,
            }
    return result


# ---------------------------------------------------------------------------
# 校验：旧库每一行在新存储里都能找到
# ---------------------------------------------------------------------------
def _count_state(
    db_file: Path, table: str, legacy: pd.DataFrame, pk: tuple[str, ...]
) -> dict[str, int]:
    if not db_file.is_file() or dbmod.is_duckdb_file(db_file) is False:
        return {"storage_rows": 0, "matched_rows": 0}
    with dbmod.connect(db_file) as conn:
        if not dbmod.table_exists(conn, table):
            return {"storage_rows": 0, "matched_rows": 0}
        storage_rows = dbmod.count_rows(conn, table)
        if legacy.empty:
            return {"storage_rows": storage_rows, "matched_rows": 0}
        conn.register("legacy_keys", legacy[list(pk)])
        matched = dbmod.fetch_scalar(
            conn,
            f"SELECT COUNT(*) FROM {_q(table)} t JOIN legacy_keys l "
            f"USING ({', '.join(_q(c) for c in pk)})",
            default=0,
        )
    return {"storage_rows": storage_rows, "matched_rows": int(matched or 0)}


def _verify_parquet(
    root: Path, legacy: pd.DataFrame, *, kind: str, keys: tuple[str, ...]
) -> dict[str, int]:
    pattern = parquet_store.daily_glob(root) if kind == "daily" else parquet_store.minute_glob(root)
    if not any(Path(root).glob("**/*.parquet")):
        return {"storage_rows": 0, "matched_rows": 0}
    con = duckdb.connect()
    try:
        if legacy.empty:
            storage_rows = con.execute(
                "SELECT COUNT(*) FROM read_parquet(?, union_by_name=true, hive_partitioning=false)",
                [pattern],
            ).fetchall()[0][0]
            return {"storage_rows": int(storage_rows), "matched_rows": 0}
        con.register("legacy_keys", legacy[list(keys)])
        storage_rows = con.execute(
            "SELECT COUNT(*) FROM read_parquet(?, union_by_name=true, hive_partitioning=false)",
            [pattern],
        ).fetchall()[0][0]
        matched = con.execute(
            "SELECT COUNT(*) FROM read_parquet(?, union_by_name=true, hive_partitioning=false) p "
            f"JOIN legacy_keys l USING ({', '.join(_q(c) for c in keys)})",
            [pattern],
        ).fetchall()[0][0]
    finally:
        con.close()
    return {"storage_rows": int(storage_rows), "matched_rows": int(matched)}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    cfg = load_config(args.config)
    sqlite = Path(args.sqlite) if args.sqlite else PROJECT_ROOT / "data" / "db" / "quant.db"
    if not sqlite.is_absolute():
        sqlite = PROJECT_ROOT / sqlite
    db_file = Path(args.db) if args.db else cfg_path(cfg, "data.db")
    if not db_file.is_absolute():
        db_file = PROJECT_ROOT / db_file
    if args.parquet_dir:
        parquet_root = Path(args.parquet_dir)
        if not parquet_root.is_absolute():
            parquet_root = PROJECT_ROOT / parquet_root
    else:
        configured = (cfg.get("data") or {}).get("parquet_dir")
        parquet_root = PROJECT_ROOT / str(configured) if configured else store.parquet_root(db_file)
    return sqlite, db_file, parquet_root


def run(args: argparse.Namespace) -> dict[str, Any]:
    sqlite_path, db_file, parquet_root = _resolve_paths(args)
    parquet_root = Path(parquet_root).resolve()
    os.environ[store.PARQUET_DIR_ENV] = str(parquet_root)

    report: dict[str, Any] = {
        "sqlite": str(sqlite_path),
        "duckdb": str(db_file),
        "parquet": str(parquet_root),
        "ok": True,
        "skipped": False,
        "tables": {},
    }
    if not sqlite_path.is_file():
        report["skipped"] = True
        report["reason"] = f"旧 SQLite 库不存在: {sqlite_path}"
        return report

    sqlite = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sqlite.row_factory = sqlite3.Row
    try:
        tables = _sqlite_tables(sqlite)
        report["source_tables"] = tables
        store.init_db(db_file)

        migrated: dict[str, dict[str, Any]] = {}
        migrated["daily_bars"] = {
            "target": "parquet",
            **_migrate_daily(sqlite, tables, db_file),
        }
        migrated["minute_bars"] = {
            "target": "parquet",
            **_migrate_minute(sqlite, tables, db_file),
        }
        for table, info in _migrate_state(sqlite, tables, db_file).items():
            migrated[table] = {"target": "duckdb", **info}

        # 校验：旧库行数 vs 新存储命中行数
        verification: dict[str, dict[str, Any]] = {}
        legacy_daily = (
            _frame(_sqlite_rows(sqlite, "daily_bars"), ("symbol", "trade_date"))
            if "daily_bars" in tables
            else pd.DataFrame(columns=["symbol", "trade_date"])
        )
        legacy_minute = (
            _frame(_sqlite_rows(sqlite, "minute_bars"), ("symbol", "period", "ts"))
            if "minute_bars" in tables
            else pd.DataFrame(columns=["symbol", "period", "ts"])
        )
        verification["daily_bars"] = {
            "source_rows": len(legacy_daily),
            **_verify_parquet(
                parquet_root, legacy_daily, kind="daily", keys=("symbol", "trade_date")
            ),
        }
        verification["minute_bars"] = {
            "source_rows": len(legacy_minute),
            **_verify_parquet(
                parquet_root, legacy_minute, kind="minute", keys=("symbol", "period", "ts")
            ),
        }
        for table in ("signals",) + _PAPER_TABLES:
            if table not in tables:
                continue
            pk = _STATE_PK[table]
            legacy = _frame(_sqlite_rows(sqlite, table), pk)
            verification[table] = {
                "source_rows": len(legacy),
                **_count_state(db_file, table, legacy, pk),
            }

        for table, info in verification.items():
            info["ok"] = int(info.get("matched_rows", 0)) >= int(info["source_rows"])
            report["ok"] = report["ok"] and bool(info["ok"])
            if migrated.get(table) is not None:
                report["tables"][table] = {
                    **migrated[table],
                    **info,
                }
        report["source_rows_total"] = int(
            sum(int(v.get("source_rows", 0)) for v in verification.values())
        )
        report["matched_rows_total"] = int(
            sum(int(v.get("matched_rows", 0)) for v in verification.values())
        )
    finally:
        sqlite.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="旧 SQLite 库 → Parquet 行情 + DuckDB 状态库"
    )
    parser.add_argument("--sqlite", default=None, help="旧 SQLite 路径（默认 data/db/quant.db）")
    parser.add_argument(
        "--db",
        default=None,
        help="目标 DuckDB 路径（默认取 config data.db，即 data/db/quant.duckdb）",
    )
    parser.add_argument(
        "--parquet-dir",
        default=None,
        help="行情 Parquet 根目录（默认 config data.parquet_dir / 由 data.db 推导）",
    )
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/default.yaml）")
    args = parser.parse_args(argv)

    report = run(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("skipped"):
        return 0
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
