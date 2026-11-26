"""离线快照管道（社区标准做法）：腾讯 HTTP 源 → reports/snapshot.json。

用途：GitHub Actions 每交易时段跑一次，生成全市场快照 JSON；
Worker /api/snapshot 从 GitHub raw 读并缓存 → 前端/接口在实时源不可达时兜底。
数据源：腾讯 web.ifzq.gtimg.cn（分时，无爬虫限制）+ qt.gtimg.cn（实时报价）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

TZ = timezone(timedelta(hours=8))
OUT = Path(__file__).resolve().parent.parent / "reports" / "snapshot.json"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"

INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000300", "沪深300"),
]


def fetch_minute(code: str) -> dict | None:
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
    try:
        resp = httpx.get(url, headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"}, timeout=15)
        resp.raise_for_status()
        node = (resp.json().get("data") or {}).get(code) or {}
        inner = node.get("data") or {}
        rows = inner.get("data") or []
        raw_date = str(inner.get("date") or "")
        if not rows or len(raw_date) < 8:
            return None
        points = []
        for line in rows:
            parts = str(line).split()
            if len(parts) < 2:
                continue
            t, price = parts[0], parts[1]
            try:
                value = float(price)
            except ValueError:
                continue
            points.append({"time": f"{t[:2]}:{t[2:]}" if len(t) == 4 else t, "price": value})
        if not points:
            return None
        return {
            "code": code,
            "name": node.get("qt", {}).get(code, [""])[1] if isinstance(node.get("qt"), dict) else "",
            "trade_date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}",
            "points": points,
        }
    except Exception as exc:  # noqa: BLE001
        print(f"  minute {code}: {exc}", file=sys.stderr)
        return None


def fetch_quotes() -> dict | None:
    codes = ",".join(code for code, _ in INDICES) + ",sh000905,sh000852,sh000688"
    url = f"https://qt.gtimg.cn/q={codes}"
    try:
        resp = httpx.get(url, headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"}, timeout=15)
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
        quotes = {}
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, raw = line.split("=", 1)
            fields = raw.strip().strip('";').split("~")
            if len(fields) < 6:
                continue
            code = key.split("_")[-1]
            try:
                price = float(fields[3])
                prev = float(fields[4])
                pct = (price / prev - 1) * 100 if prev else 0.0
            except (ValueError, IndexError):
                continue
            quotes[code] = {"price": price, "pct_chg": round(pct, 2), "name": fields[1]}
        return quotes or None
    except Exception as exc:  # noqa: BLE001
        print(f"  quotes: {exc}", file=sys.stderr)
        return None


def build() -> dict:
    quotes = fetch_quotes() or {}
    indices = []
    for code, name in INDICES:
        minute = fetch_minute(code)
        q = quotes.get(code) or {}
        if not minute:
            continue
        last = minute["points"][-1]["price"]
        indices.append({
            "code": code,
            "name": name,
            "price": q.get("price") or last,
            "pct_chg": q.get("pct_chg"),
            "trade_date": minute["trade_date"],
            "points": minute["points"],
        })
    return {
        "schema": 1,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "trade_date": indices[0]["trade_date"] if indices else "",
        "indices": indices,
    }


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    snapshot = build()
    if not snapshot["indices"]:
        print("无数据，保留旧快照", file=sys.stderr)
        return 1
    OUT.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    print(f"snapshot: {len(snapshot['indices'])} 指数, trade_date={snapshot['trade_date']}, size={OUT.stat().st_size}B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
