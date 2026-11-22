#!/usr/bin/env python3
"""扩展云端股票池：精选股票 → 拉日线 → 生成信号 → 推送云端（含代码→名称映射）。

用法：
    set -a; . ~/.quant_secrets/cloudflare.env; set +a
    python scripts/push_stock_pool.py --limit 40
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from quant.config import cfg_path, load_config

# 精选常见 A 股（覆盖大多数查询；可按需扩充）
POOL: list[str] = [
    "600519", "000001", "300750", "601318", "600036", "000858", "002594", "601899",
    "600900", "601088", "600030", "000651", "000333", "600276", "300059", "002415",
    "600887", "601012", "600585", "601166", "000725", "601398", "601857", "600028",
    "601988", "601601", "601628", "600016", "600104", "601668", "000002", "000063",
    "600009", "601888", "603259", "688981", "688111", "300760", "002475", "002352",
]


def fetch_names(symbols: list[str]) -> dict[str, str]:
    try:
        import akshare as ak

        df = ak.stock_info_a_code_name()
        mapping = dict(zip(df["code"].astype(str), df["name"].astype(str)))
        return {s: mapping.get(s, "") for s in symbols}
    except Exception as exc:  # noqa: BLE001
        print(f"[pool] 名称获取失败（忽略）：{type(exc).__name__}: {exc}")
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--skip-fetch", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    token = os.environ.get("QUANT_INGEST_TOKEN", "")
    if not token:
        print("缺少环境变量 QUANT_INGEST_TOKEN", file=sys.stderr)
        return 2
    base = cfg["cloudflare"]["worker_url"].rstrip("/")
    symbols = POOL[: max(1, args.limit)]

    if not args.skip_fetch:
        from quant.data.fetch import sync_daily
        from quant.data.store import init_db

        init_db(cfg_path(cfg, "data.db"))
        print(f"[pool] 拉取 {len(symbols)} 只日线…")
        stats = sync_daily(symbols, args.start, None, cfg)
        ok = sum(1 for v in stats.values() if v > 0)
        print(f"[pool] 拉取完成：{ok}/{len(symbols)} 只有数据")

    names = fetch_names(symbols)

    from quant.model.predict import generate_signal

    signals = []
    for symbol in symbols:
        try:
            sig = generate_signal(symbol, cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"[pool] {symbol} 信号失败：{type(exc).__name__}")
            continue
        if sig and sig.get("price"):
            if names.get(symbol):
                sig["name"] = names[symbol]
            signals.append(sig)
    print(f"[pool] 生成信号 {len(signals)} 条")

    headers = {"x-ingest-token": token, "User-Agent": "quant-local/1.0"}
    with httpx.Client(timeout=40) as client:
        resp = client.post(f"{base}/api/ingest", json={"schema": 1, "items": signals}, headers=headers)
        print("[pool] signals ingest:", resp.status_code, resp.text[:160])
        name_items = [{"symbol": s, "name": n} for s, n in names.items() if n]
        if name_items:
            resp = client.post(f"{base}/api/stock-names/ingest", json={"schema": 1, "items": name_items}, headers=headers)
            print("[pool] names ingest:", resp.status_code, resp.text[:160])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
