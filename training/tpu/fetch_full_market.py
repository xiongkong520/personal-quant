#!/usr/bin/env python3
"""全市场日线抓取（Baostock 单源，分片并行 + 分块断点续抓）

为什么单独一个脚本
------------------
``training/fetch_data.py`` 是串行的（保守、适合日常增量），全市场 5,000+ 只按 3 秒/只
要 4 小时以上。这里把它按股票代码分片并行，并**按块落盘**：
每块 150 只抓完立即合并进分片 parquet，中途断了重跑会自动跳过已抓到的股票。

- 数据源固定 Baostock：东财（AKShare）对同一 IP 限流很狠，全市场批量必然被打死；
  但 Baostock 对**同时在线会话数**敏感，并行开太多会集体报「网络接收错误」，默认 3 个分片；
- 股票池来自仓库自己的全市场快照 ``reports/market_all.json``（新浪 5,579 只），
  剔除北交所（Baostock 不覆盖）与 ST/退市（名字含 ST/退）；
- 合并后写 ``training/data_full/daily.parquet``，训练脚本直接可读。

用法
----
    python training/tpu/fetch_full_market.py --workers 3 --start 2024-01-01
    python training/tpu/fetch_full_market.py --limit 800      # 只抓成交额前 800，先验证
    python training/tpu/fetch_full_market.py --merge-only     # 只合并已有分片
    # 断了直接重跑同一条命令：已完成的分片/分块会被跳过
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
    picked: list[tuple[float, str]] = []
    for s in snap["stocks"]:
        code = str(s.get("code", "")).zfill(6)
        name = str(s.get("name", ""))
        if len(code) != 6 or code[0] not in "036":  # 北交所 4/8/9 开头，Baostock 不覆盖
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        picked.append((float(s.get("amount") or 0.0), code))
    seen: set[str] = set()
    ordered: list[str] = []
    for _amt, code in sorted(picked, reverse=True):
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    return ordered[:limit] if limit else ordered


def shard(items: list[str], workers: int) -> list[list[str]]:
    """轮转分片：每片流动性分布接近，避免某片全是冷门股。"""
    buckets: list[list[str]] = [[] for _ in range(workers)]
    for i, code in enumerate(items):
        buckets[i % workers].append(code)
    return [b for b in buckets if b]


def existing_symbols(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        return set(pd.read_parquet(path, columns=["symbol"])["symbol"].astype(str).str.zfill(6))
    except Exception:  # noqa: BLE001
        return set()


def run_shard(idx: int, symbols: list[str], start: str, sleep: float, chunk: int) -> None:
    out_dir = DATA_FULL / f"shard_{idx:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = DATA_FULL / f"shard_{idx:02d}.log"
    done = existing_symbols(out_dir / "daily.parquet")
    pending = [s for s in symbols if s not in done]
    already = len(symbols) - len(pending)
    print(f"[shard {idx}] {len(symbols)} 只，已完成 {already}，待抓 {len(pending)}", flush=True)
    if not pending:
        return
    with open(log_path, "a", encoding="utf-8") as logf:
        for i in range(0, len(pending), chunk):
            part = pending[i:i + chunk]
            cmd = [
                sys.executable, "-u", str(FETCH),
                "--symbols", ",".join(part),
                "--start", start,
                "--source", "baostock",
                "--skip-zt",
                "--retries", "1",
                "--sleep", str(sleep),
                "--out-dir", str(out_dir),
            ]
            if idx != 0:
                cmd.append("--skip-index")
            logf.write(f"\n=== shard {idx} chunk {i // chunk + 1} ({len(part)} 只) "
                       f"{time.strftime('%H:%M:%S')} ===\n")
            logf.flush()
            t0 = time.time()
            subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
            logf.write(f"=== shard {idx} chunk {i // chunk + 1} 结束，用时 {time.time() - t0:.0f}s ===\n")
            logf.flush()
            got = len(existing_symbols(out_dir / "daily.parquet"))
            print(f"[shard {idx}] 进度 {min(i + chunk, len(pending))}/{len(pending)}，"
                  f"分片累计 {got} 只", flush=True)


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
    ap = argparse.ArgumentParser(description="全市场日线并行抓取（Baostock，分块可续）")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=0.05)
    ap.add_argument("--chunk", type=int, default=150, help="每块股票数（抓完即落盘，断了可续）")
    ap.add_argument("--merge-only", action="store_true")
    args = ap.parse_args(argv)

    if args.merge_only:
        print(json.dumps(merge_shards(), ensure_ascii=False, indent=2))
        return 0

    universe = load_universe(args.limit)
    buckets = shard(universe, args.workers)
    print(f"股票池 {len(universe)} 只 → {len(buckets)} 个分片，每块 {args.chunk} 只", flush=True)
    DATA_FULL.mkdir(parents=True, exist_ok=True)
    (DATA_FULL / "universe.txt").write_text(",".join(universe))

    started = time.time()
    with ThreadPoolExecutor(max_workers=len(buckets)) as pool:
        futures = [
            pool.submit(run_shard, i, b, args.start, args.sleep, args.chunk)
            for i, b in enumerate(buckets)
        ]
        for f in futures:
            f.result()
    elapsed = time.time() - started
    print(f"分片全部结束，用时 {elapsed / 60:.1f} 分钟", flush=True)
    summary = merge_shards()
    summary["elapsed_min"] = round(elapsed / 60, 1)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
