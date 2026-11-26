#!/usr/bin/env python3
"""从全市场日线里切一个「训练子集」：按近期成交额取流动性最好的 N 只。

为什么需要
----------
全市场 5,000+ 只 × 3 年 ≈ 300 万行，CPU 上跑 Walk-Forward（6 折 LightGBM）要小时级；
社区常规做法是**全市场存着做展示/选股，训练用流动性前 N 只**（N 常取 300~1000）：
冷门股流动性差、滑点大，本来也不该进交易模型的训练集。

用法
----
    python training/tpu/make_train_subset.py --src training/data_full --limit 800 --out training/data
    python training/tpu/make_train_subset.py --src training/data_full --limit 300 --out training/data_nn
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按流动性切训练子集")
    ap.add_argument("--src", default="training/data_full", help="全市场 parquet 目录")
    ap.add_argument("--out", default="training/data", help="输出目录（写 daily/index）")
    ap.add_argument("--limit", type=int, default=800, help="保留成交额最高的 N 只")
    ap.add_argument("--lookback", type=int, default=60, help="用最近 N 个交易日的成交额排序")
    args = ap.parse_args(argv)

    src = Path(args.src)
    daily = pd.read_parquet(src / "daily.parquet")
    daily["symbol"] = daily["symbol"].astype(str).str.zfill(6)
    dates = sorted(daily["trade_date"].unique())[-args.lookback:]
    recent = daily[daily["trade_date"].isin(dates)]
    rank = recent.groupby("symbol")["amount"].mean().sort_values(ascending=False)
    keep = set(rank.head(args.limit).index)

    subset = daily[daily["symbol"].isin(keep)].copy()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    subset.to_parquet(out / "daily.parquet", index=False, compression="snappy")

    index_src = src / "index.parquet"
    if index_src.is_file():
        pd.read_parquet(index_src).to_parquet(out / "index.parquet", index=False, compression="snappy")
    zt_src = src / "zt_pool.parquet"
    if zt_src.is_file():
        pd.read_parquet(zt_src).to_parquet(out / "zt_pool.parquet", index=False, compression="snappy")

    summary = {
        "src": str(src),
        "out": str(out),
        "limit": args.limit,
        "symbols": int(subset["symbol"].nunique()),
        "rows": int(len(subset)),
        "dates": int(subset["trade_date"].nunique()),
        "range": [str(subset["trade_date"].min()), str(subset["trade_date"].max())],
        "size_mb": round((out / "daily.parquet").stat().st_size / 1024 / 1024, 1),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
