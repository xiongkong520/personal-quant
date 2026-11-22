#!/usr/bin/env python3
"""薄入口：抓 A 股日线/分钟线到 Parquet（+ DuckDB 状态库；等价于 ``python -m quant.cli fetch``）。

Example:
    python scripts/fetch_data.py --symbols 600519,000001 \
        --start 2024-01-01 --end 2026-11-20
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许未安装成包时直接从仓库根目录运行
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from quant.config import cfg_path, load_config  # noqa: E402
from quant.data.fetch import sync_daily, sync_minute  # noqa: E402
from quant.data import store  # noqa: E402
from quant.data.store import init_db  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取 A 股日线/分钟线到 Parquet 行情库")
    parser.add_argument("--symbols", default="600519,000001,300750")
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default=None, help="默认今天")
    parser.add_argument("--minute", action="store_true", help="同时抓分钟线")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    db = cfg_path(cfg, "data.db")
    init_db(db)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    daily = sync_daily(symbols, args.start, args.end, cfg)
    result: dict = {"duckdb": str(db), "parquet": str(store.parquet_root(db)), "daily": daily}
    if args.minute:
        result["minute"] = sync_minute(symbols, int(cfg["data"]["minute_period"]), cfg)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
