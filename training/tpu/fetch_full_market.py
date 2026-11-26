#!/usr/bin/env python3
"""全市场日线抓取（Baostock 单源，并行分片）

为什么单独一个脚本
------------------
``training/fetch_data.py`` 是串行的（保守、适合日常增量），全市场 5,000+ 只按 3 秒/只
要 4 小时以上。这里把它按股票代码分片并行跑，最后合并成一份 parquet。

- 数据源固定 Baostock：东财（AKShare）对同一 IP 限流很狠，全市场批量必然被打死；
- 股票池来自仓库自己的全市场快照 ``reports/market_all.json``（新浪 5,579 只），
  剔除北交所（Baostock 不覆盖）与 ST/退市（名字含 ST/退）；
- 每个分片用独立子进程 + 独立输出目录，失败的分片不影响其它分片；
- 合并后写 ``training/data_full/daily.parquet``（+ index.parquet），训练脚本直接可读。

用法
----
    python training/tpu/fetch_full_market.py --workers 6 --start 2024-01-01
    python training/tpu/fetch_full_market.py --limit 800      # 只抓成交额前 800，先验证
    python training/tpu/fetch_full_market.py --merge-only     # 只合并已有分片
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

TPU_DIR = Path(__file__).resolve().parent
REPO_DIR = TPU_DIR.parent.parent
DATA_FULL = REPO_DIR / "training" / "data_full"
SNAPSHOT = REPO_DIR / "reports" / "market_all.json"
FETCH = REPO_DIR / "training" / "fetch_data.py"


def load_universe(limit: int | None = None) -> list[str]:
    """从全市场快照取 sh/sz 主板+创业板+科创板，按成交额降序。"""
    snap = json.loads(SNAPSHOT.read_text())
    stocks = snap["stocks"]
    picked: list[tuple[float, str]] = []
    for s in stocks:
        code = str(s.get("code", "")).zfill(6)
        name = str(s.get("name", ""))
        if not code or len(code) != 6:
            continue
        if code[0] not in "036":  # 北交所 4/8/9 开头，Baostock 不覆盖
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        picked.append((float(s.get("amount") or 0.0), code))
    # 按成交额降序，去重
    seen: set[str] = set()
    ordered: list[str] = []
    for _amt, code in sorted(picked, reverse=True):
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    if limit:
        ordered = ordered[:limit]
    return ordered


def shard(items: list[str], workers: int) -> list[list[str]]:
    """轮转分片：每个分片内流动性分布接近，避免某片全是冷门股。"""
    buckets: list[list[str]] = [[] for _ in range(workers)]
    for i, code in enumerate(items):
        buckets[i % workers].append(code)
    return [b for b in buckets if b]


def run_shard(idx: int, total: int, symbols: list[str], start: str, sleep: float) -> subprocess.Popen:
    out_dir = DATA_FULL / f"shard_{idx:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-u", str(FETCH),
        "--symbols", ",".join(symbols),
        "--start", start,
        "--source", "baostock",
        "--skip-zt",
        "--retries", "1",
        "--sleep", str(sleep),
        "--out-dir", str(out_dir),
    ]
    if idx != 0:
        cmd.append("--skip-index")
    log = open(DATA_FULL / f"shard_{idx:02d}.log", "w")
    print(f"[shard {idx}/{total}] {len(symbols)} 只 → {out_dir}", flush=True)
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)


def merge_shards() -> dict:
    parts, index_parts = [], []
    for shard_dir in sorted(DATA_FULL.glob("shard_*")):
        daily = shard_dir / "daily.parquet"
        if daily.is_file():
            df = pd.read_parquet(daily)
            if len(df):
                parts.append(df)
        idx_file = shard_dir / "index.parquet"
        if idx_file.is_file():
            idx = pd.read_parquet(idx_file)
            if len(idx):
                index_parts.append(idx)
    if not parts:
        raise SystemExit("没有任何分片数据可合并")
    daily = pd.concat(parts, ignore_index=True)
    daily["symbol"] = daily["symbol"].astype(str).str.zfill(6)
    daily = daily.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    daily = daily.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    DATA_FULL.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(DATA_FULL / "daily.parquet", index=False, compression="snappy")
    summary = {
        "symbols": int(daily["symbol"].nunique()),
        "rows": int(len(daily)),
        "dates": int(daily["trade_date"].nunique()),
        "range": [str(daily["trade_date"].min()), str(daily["trade_date"].max())],
    }
    if index_parts:
        index = pd.concat(index_parts, ignore_index=True).drop_duplicates(
            subset=["symbol", "trade_date"], keep="last"
        )
        index.to_parquet(DATA_FULL / "index.parquet", index=False, compression="snappy")
        summary["index_rows"] = int(len(index))
    summary["size_mb"] = round((DATA_FULL / "daily.parquet").stat().st_size / 1024 / 1024, 1)
    (DATA_FULL / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全市场日线并行抓取（Baostock）")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=0.1)
    ap.add_argument("--merge-only", action="store_true")
    args = ap.parse_args(argv)

    if args.merge_only:
        print(json.dumps(merge_shards(), ensure_ascii=False, indent=2))
        return 0

    universe = load_universe(args.limit)
    buckets = shard(universe, args.workers)
    print(f"股票池 {len(universe)} 只 → {len(buckets)} 个分片", flush=True)
    DATA_FULL.mkdir(parents=True, exist_ok=True)
    (DATA_FULL / "universe.txt").write_text(",".join(universe))

    started = time.time()
    procs = [run_shard(i, len(buckets), b, args.start, args.sleep) for i, b in enumerate(buckets)]
    for p in procs:
        p.wait()
    elapsed = time.time() - started
    print(f"分片全部结束，用时 {elapsed / 60:.1f} 分钟", flush=True)
    summary = merge_shards()
    summary["elapsed_min"] = round(elapsed / 60, 1)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
