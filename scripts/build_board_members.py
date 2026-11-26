"""板块成分股管道：新浪行业板块 → 成分股 → reports/board_members.json。

背景：前端「板块涨幅榜」的板块名来自新浪行业（Worker /api/market-overview 的
`sectors.industry`，与 `newSinaHy.php` 同源）；点击板块需要成分股数据。
本脚本用同一数据源生成静态 JSON，供 Cloudflare Worker `/api/board-members` 读取
（GitHub raw + 60s 缓存），避免 Worker 出口直连新浪被限流。

数据源（均为 AKShare / 社区常用公开接口，本机出口可用）：
1. 板块列表：`https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php`（GBK）
   `var S_Finance_bankuai_sinaindustry = {"new_blhy":"new_blhy,玻璃行业,19,均价,涨跌额,涨跌幅,..."}`
2. 成分股：`Market_Center.getHQNodeData?node=<code>&page=N&num=100&sort=changepercent&asc=0`
   （num 上限实测 100，需分页；返回 symbol/code/name/trade/changepercent/amount 等）

输出 schema（体积 < 2MB）：
```
{
  "schema": 1,
  "generated_at": "2026-11-26T21:00:00+08:00",
  "source": "sina",
  "board_count": 49,
  "member_count": 3030,
  "boards": [
    {"code": "new_blhy", "name": "玻璃行业", "pct_chg": 0.58, "count": 19,
     "members": [{"code": "600629", "name": "华建集团", "price": 13.08, "pct_chg": 10.01, "amount": 358730201}]}
  ],
  "data_quality": {"ok": true, "errors": []}
}
```

用法：
    python scripts/build_board_members.py                 # 全量板块 → reports/board_members.json
    python scripts/build_board_members.py --top 20        # 只保留当日涨幅前 20 的板块
    python scripts/build_board_members.py --out /tmp/x.json --throttle 0.5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "reports" / "board_members.json"

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"
HEADERS = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}
INDUSTRY_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
MEMBERS_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
INDUSTRY_RE = re.compile(r"=\s*(\{[\s\S]*\})\s*;?\s*$")
PAGE_SIZE = 100  # 新浪 getHQNodeData 的 num 上限（实测传 300/500 仍只返回 100）

_last_request_at = 0.0


def throttle(interval: float) -> None:
    """接口节流：两次请求间隔 ≥ interval 秒。"""
    global _last_request_at
    wait = _last_request_at + interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _get(url: str, params: dict | None, interval: float, retries: int = 3) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(retries):
        throttle(interval)
        try:
            resp = httpx.get(url, params=params, headers=HEADERS, timeout=25)
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败（重试 {retries} 次）：{last_exc}") from last_exc


# ---------------------------------------------------------------------------
# 板块列表
# ---------------------------------------------------------------------------
def fetch_industry_boards(interval: float) -> list[dict]:
    """拉新浪行业板块列表（code/name/count/pct_chg），按当日涨幅降序。"""
    resp = _get(INDUSTRY_URL, None, interval)
    text = resp.content.decode("gbk", errors="replace")
    match = INDUSTRY_RE.search(text.strip())
    if not match:
        raise RuntimeError("新浪行业板块列表解析失败（未匹配到 JSON 对象）")
    raw = json.loads(match.group(1))
    boards: list[dict] = []
    for code, value in raw.items():
        parts = str(value).split(",")
        if len(parts) < 6:
            continue
        try:
            count = int(parts[2])
            pct = float(parts[5])
        except (TypeError, ValueError):
            count, pct = 0, 0.0
        name = parts[1].strip()
        if not code or not name:
            continue
        boards.append({"code": str(code), "name": name, "count": count, "pct_chg": round(pct, 2)})
    boards.sort(key=lambda b: b["pct_chg"], reverse=True)
    return boards


# ---------------------------------------------------------------------------
# 成分股
# ---------------------------------------------------------------------------
def _member_row(raw: dict) -> dict | None:
    code = str(raw.get("code") or "").strip()
    name = str(raw.get("name") or "").strip()
    try:
        price = float(raw.get("trade") or 0)
        pct = float(raw.get("changepercent") or 0)
        amount = float(raw.get("amount") or 0)
    except (TypeError, ValueError):
        return None
    if not re.fullmatch(r"\d{6}", code) or not name:
        return None
    return {
        "code": code,
        "name": name,
        "price": round(price, 2),
        "pct_chg": round(pct, 2),
        "amount": int(round(amount)),
    }


def fetch_board_members(
    board: dict, interval: float, max_pages: int = 4
) -> tuple[list[dict], list[str], list[str]]:
    """拉单个板块全部成分股（分页直到不足一页）。返回 (members, errors, warnings)。"""
    members: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        params = {
            "page": page,
            "num": PAGE_SIZE,
            "sort": "changepercent",
            "asc": 0,
            "node": board["code"],
            "symbol": "",
            "_s_r_a": "page",
        }
        try:
            resp = _get(MEMBERS_URL, params, interval)
        except Exception as exc:  # noqa: BLE001 - 单板块失败不影响整体
            errors.append(f"{board['code']}:page{page}:{str(exc)[:80]}")
            break
        text = resp.text.strip()
        if not text or text == "null":
            break
        try:
            rows = json.loads(text)
        except json.JSONDecodeError:
            errors.append(f"{board['code']}:page{page}:json_decode")
            break
        if not isinstance(rows, list) or not rows:
            break
        for raw in rows:
            row = _member_row(raw)
            if row and row["code"] not in seen:
                seen.add(row["code"])
                members.append(row)
        if len(rows) < PAGE_SIZE:
            break
    members.sort(key=lambda m: m["pct_chg"], reverse=True)
    if board["count"] and len(members) != board["count"]:
        # 新浪板块「家数」含当日无行情的停牌票，成分列表少几只属正常，记 warning
        warnings.append(f"{board['code']}:count_mismatch(expect={board['count']},got={len(members)})")
    return members, errors, warnings


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build(
    top: int = 0,
    out: Path = DEFAULT_OUT,
    interval: float = 0.25,
    max_pages: int = 4,
) -> dict:
    started = time.time()
    boards = fetch_industry_boards(interval)
    if top > 0:
        boards = boards[:top]
    errors: list[str] = []
    warnings: list[str] = []
    payload_boards: list[dict] = []
    member_count = 0
    for idx, board in enumerate(boards, 1):
        members, board_errors, board_warnings = fetch_board_members(board, interval, max_pages)
        errors.extend(board_errors)
        warnings.extend(board_warnings)
        if not members:
            errors.append(f"{board['code']}:empty")
            continue
        member_count += len(members)
        payload_boards.append(
            {
                "code": board["code"],
                "name": board["name"],
                "pct_chg": board["pct_chg"],
                "count": len(members),
                "members": members,
            }
        )
        print(
            f"[{idx}/{len(boards)}] {board['name']}({board['code']}) "
            f"pct={board['pct_chg']:+.2f}% members={len(members)}",
            file=sys.stderr,
        )
    payload = {
        "schema": 1,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "source": "sina",
        "board_count": len(payload_boards),
        "member_count": member_count,
        "boards": payload_boards,
        "data_quality": {
            "ok": not errors,
            "errors": errors[:20],
            "warnings": warnings[:20],
            "elapsed_sec": round(time.time() - started, 1),
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(out)  # 原子替换，避免 Worker 读到半个文件
    size = out.stat().st_size
    print(
        f"boards={len(payload_boards)} members={member_count} "
        f"size={size}B ({size / 1024:.0f}KiB) elapsed={time.time() - started:.1f}s → {out}"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成板块成分股 JSON（新浪行业）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出路径")
    parser.add_argument("--top", type=int, default=0, help="只保留当日涨幅前 N 个板块（0=全部）")
    parser.add_argument("--throttle", type=float, default=0.25, help="请求间隔秒（默认 0.25）")
    parser.add_argument("--max-pages", type=int, default=4, help="单板块最多翻页数")
    args = parser.parse_args(argv)
    try:
        payload = build(
            top=max(0, args.top),
            out=Path(args.out),
            interval=max(0.0, args.throttle),
            max_pages=max(1, args.max_pages),
        )
    except Exception as exc:  # noqa: BLE001 - CLI 给出清晰失败
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    return 0 if payload["board_count"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
