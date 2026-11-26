"""全链路验收：所有云端接口 + 数据完整性 + 日期新鲜度。一次性输出问题清单。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import httpx

BASE = "https://quant-board-9y2.pages.dev"
TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")

CHECKS = [
    ("GET", "/api/health", None),
    ("GET", "/api/me", None),
    ("GET", "/api/market-overview", None),
    ("GET", "/api/market", None),
    ("GET", "/api/kline?symbol=600519&days=30", None),
    ("GET", "/api/kline?symbol=sh000001&days=30", None),
    ("GET", "/api/intraday?code=sh000001", None),
    ("GET", "/api/intraday?code=sz399001", None),
    ("GET", "/api/intraday?code=sz399006", None),
    ("GET", "/api/intraday?code=sh000300", None),
    ("GET", "/api/regime", None),
    ("GET", "/api/events?limit=5", None),
    ("GET", "/api/summary/latest", None),
    ("GET", "/api/backtest", None),
    ("GET", "/api/signals?limit=3", None),
    ("GET", "/api/paper", None),
    ("GET", "/api/ai/insight", None),
    ("GET", "/api/search?q=600519", None),
]


def probe(method: str, path: str) -> dict:
    try:
        resp = httpx.request(method, BASE + path, timeout=45)
        try:
            body = resp.json()
        except Exception:
            body = {}
        return {"status": resp.status_code, "body": body, "len": len(resp.content)}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "body": {}, "error": str(exc)[:120], "len": 0}


def main() -> int:
    issues: list[str] = []
    rows: list[tuple[str, str, str]] = []
    for method, path, _ in CHECKS:
        r = probe(method, path)
        status = r["status"]
        problems: list[str] = []
        if status != 200:
            problems.append(f"HTTP {status}")
            if r.get("error"):
                problems.append(r["error"])
        body = r["body"]
        if status == 200 and isinstance(body, dict):
            if body.get("degraded"):
                problems.append("degraded")
            if path.startswith("/api/intraday"):
                data = body.get("data") or {}
                pts = data.get("points") or []
                if not pts:
                    problems.append("分时空")
                elif body.get("trade_date") != TODAY:
                    problems.append(f"日期 {body.get('trade_date')}")
            elif path.startswith("/api/kline"):
                if not (body.get("items") or []):
                    problems.append("K线空")
            elif path == "/api/market-overview":
                if not (body.get("indices") or []):
                    problems.append("指数空")
                if not ((body.get("sectors") or {}).get("industry") or {}).get("gainers"):
                    problems.append("板块空")
            elif path == "/api/signals?limit=3":
                if not (body.get("items") or []):
                    problems.append("信号空")
            elif path == "/api/ai/insight":
                ins = body.get("insight") or {}
                if not ins.get("generated_at"):
                    problems.append("AI空")
                elif not str(ins.get("summary") or "").strip():
                    problems.append("AI收评空")
            elif path == "/api/events":
                if not (body.get("items") or []):
                    problems.append("事件空")
            elif path == "/api/paper":
                snap = body.get("snapshot") or {}
                if not snap.get("account"):
                    problems.append("账户空")
        label = path.replace("/api/", "").replace("?code=", "").replace("?symbol=", "").replace("&days=30", "").replace("?limit=3", "").replace("?limit=5", "")
        rows.append((label, str(status), " / ".join(problems) if problems else "OK"))
        if problems:
            issues.append(f"{label}: {' / '.join(problems)}")
    print(f"{'端点':<38} {'状态':<5} 问题")
    for label, status, problem in rows:
        print(f"{label:<38} {status:<5} {problem}")
    print(f"\n== 问题汇总（{len(issues)} 项） ==")
    for issue in issues:
        print(" -", issue)
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
