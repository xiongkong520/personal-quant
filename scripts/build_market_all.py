"""全市场快照（社区常用新浪行情列表：Market_Center.getHQNodeData）→ reports/market_all.json。

数据源：新浪行情列表（分页 100/页，约 5x 页；本机/GitHub Actions 运行）。
用途：前端全市场表格、搜索联想（5000+ 个股）。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

TZ = timezone(timedelta(hours=8))
OUT = Path(__file__).resolve().parent.parent / "reports" / "market_all.json"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"
BASE = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"


def fetch_page(page: int, num: int = 100) -> list[dict]:
    params = {
        "page": page, "num": num, "sort": "symbol", "asc": 1,
        "node": "hs_a", "symbol": "", "_s_r_a": "page",
    }
    resp = httpx.get(BASE, params=params, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}, timeout=20)
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or text == "null":
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def to_row(raw: dict) -> dict | None:
    code = str(raw.get("code") or "")
    name = str(raw.get("name") or "")
    symbol = str(raw.get("symbol") or "")
    try:
        price = float(raw.get("trade") or 0)
        pct = float(raw.get("changepercent") or 0)
        amount = float(raw.get("amount") or 0)
        turnover = float(raw.get("turnoverratio") or 0)
        mktcap = float(raw.get("mktcap") or 0)
    except (TypeError, ValueError):
        return None
    if not code or not name or price <= 0:
        return None
    return {
        "code": code, "name": name, "symbol": symbol,
        "price": round(price, 2), "pct_chg": round(pct, 2),
        "amount": amount, "turnover": round(turnover, 2), "mktcap": mktcap,
    }


def main() -> int:
    rows: list[dict] = []
    page = 1
    empty_streak = 0
    while page <= 80:
        try:
            batch = fetch_page(page)
        except Exception as exc:  # noqa: BLE001
            print(f"  page {page} err: {str(exc)[:80]}", file=sys.stderr)
            time.sleep(2)
            page += 1
            continue
        if not batch:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
            for raw in batch:
                row = to_row(raw)
                if row:
                    rows.append(row)
        page += 1
        time.sleep(0.6)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "count": len(rows),
        "stocks": rows,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"stocks={len(rows)} size={OUT.stat().st_size}B")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
