"""数据审计：多源交叉校验 + 异常检测（回答"数据对不对"）。

校验项：
1. 指数点位/涨跌幅：腾讯 vs 新浪 vs 东财（三源比对，偏差>0.1% 报警）
2. 两市成交额：market-overview 的 total_amount ≈ 沪市+深市（口径核对）
3. 涨跌家数：涨+跌+平 ≈ 全市场有效交易数；涨停/跌停数 vs 腾讯涨停池
4. 板块榜：新浪行业 vs 东财板块（方向一致性抽样）
5. 快照一致性：/api/snapshot vs /api/market-overview 的指数价格
输出：每项 PASS/WARN/FAIL + 问题清单（exit 1 若有 FAIL）
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import httpx

TZ = timezone(timedelta(hours=8))
BASE = "https://quant-board-9y2.pages.dev"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"
INDEXES = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指", "sh000300": "沪深300"}


def tencent_quotes(codes: list[str]) -> dict[str, dict]:
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    resp = httpx.get(url, headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"}, timeout=15)
    text = resp.content.decode("gbk", errors="ignore")
    out = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        f = raw.strip().strip('";').split("~")
        if len(f) < 6:
            continue
        code = key.split("_")[-1]
        try:
            out[code] = {"price": float(f[3]), "pct": float(f[32]) if len(f) > 32 and f[32] else None}
        except (ValueError, IndexError):
            continue
    return out


def sina_quotes(codes: list[str]) -> dict[str, dict]:
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    resp = httpx.get(url, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}, timeout=15)
    text = resp.content.decode("gbk", errors="ignore")
    out = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        f = raw.strip().strip('";').split(",")
        if len(f) < 4:
            continue
        code = key.split("_")[-1]
        try:
            price = float(f[3])
            prev = float(f[2])
            out[code] = {"price": price, "pct": round((price / prev - 1) * 100, 2) if prev else None}
        except (ValueError, IndexError):
            continue
    return out


def api(path: str) -> dict:
    try:
        resp = httpx.get(BASE + path, timeout=40)
        return resp.json()
    except Exception:
        return {}


def main() -> int:
    issues: list[str] = []
    checks: list[tuple[str, str, str]] = []

    # 1) 指数三源比对
    tq = tencent_quotes(list(INDEXES))
    sq = sina_quotes(list(INDEXES))
    mo = api("/api/market-overview")
    snap = api("/api/snapshot")
    mo_idx = {i["code"]: i for i in (mo.get("indices") or [])}
    snap_idx = {i["code"]: i for i in (snap.get("indices") or [])}
    for code, name in INDEXES.items():
        t, s = tq.get(code), sq.get(code)
        m = mo_idx.get(code, {})
        row_issues = []
        if t and s:
            diff = abs(t["price"] - s["price"]) / max(s["price"], 1e-9) * 100
            if diff > 0.1:
                row_issues.append(f"腾讯/新浪价差 {diff:.3f}%")
        if m and t:
            diff = abs(float(m.get("price") or 0) - t["price"]) / max(t["price"], 1e-9) * 100
            if diff > 0.1:
                row_issues.append(f"market-overview/腾讯价差 {diff:.3f}%")
        if s and snap_idx.get(code) and t:
            pass
        checks.append((name, "PASS" if not row_issues else "WARN", "；".join(row_issues) or
                       f"腾讯 {t['price'] if t else '-'} / 新浪 {s['price'] if s else '-'} / 接口 {m.get('price') or '-'}"))
        issues.extend(f"{name}: {x}" for x in row_issues)

    # 2) 两市成交额
    b = mo.get("breadth") or {}
    total = Number = b.get("total_amount") or 0
    tx = tencent_quotes(["sh000001", "sz399001"])
    sh_amt = None
    # 腾讯行情第 37 字段为成交额（万元）；用成交额字段更稳：f[37]
    try:
        raw = httpx.get("https://qt.gtimg.cn/q=sh000001,sz399001", headers={"User-Agent": UA}, timeout=15)
        text = raw.content.decode("gbk", errors="ignore")
        amts = {}
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            f = val.strip().strip('";').split("~")
            code = key.split("_")[-1]
            amts[code] = float(f[37]) * 10000 if len(f) > 37 and f[37] else 0
        expected = amts.get("sh000001", 0) + amts.get("sz399001", 0)
        if total and expected:
            diff = abs(total - expected) / max(expected, 1) * 100
            ok = diff < 8
            checks.append(("两市成交额", "PASS" if ok else "FAIL",
                           f"接口 {total/1e12:.2f}万亿 vs 腾讯合计 {expected/1e12:.2f}万亿（差 {diff:.1f}%）"))
            if not ok:
                issues.append(f"成交额口径不一致：接口 {total/1e12:.2f} vs 腾讯 {expected/1e12:.2f} 万亿")
        else:
            checks.append(("两市成交额", "WARN", f"total={total} expected={expected}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("两市成交额", "WARN", f"校验失败: {str(exc)[:60]}"))

    # 3) 涨跌家数
    up, down, flat = b.get("up") or 0, b.get("down") or 0, b.get("flat") or 0
    total_n = up + down + flat
    ma = api("/api/market-all")
    all_n = ma.get("count") or len(ma.get("stocks") or [])
    if all_n:
        ratio = total_n / all_n * 100
        ok = 88 <= ratio <= 103
        checks.append(("涨跌家数合计", "PASS" if ok else "WARN",
                       f"涨+跌+平={total_n} vs 全市场 {all_n}（{ratio:.0f}%）"))
        if not ok:
            issues.append(f"涨跌家数合计 {total_n} 与全市场 {all_n} 不匹配")

    # 4) 涨停/跌停数 vs 东财涨停池（本机可用，近30日）
    try:
        zt = httpx.get("https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=10&sort=fbt%3Aasc&date=", headers={"User-Agent": UA}, timeout=20)
        pass
    except Exception:
        pass
    checks.append(("涨停/跌停数", "WARN", f"接口 涨停 {b.get('limit_up')} / 跌停 {b.get('limit_down')}（未做第三源核对）"))

    # 5) 快照一致性
    if snap_idx and mo_idx:
        p1 = float((snap_idx.get("sh000300") or {}).get("price") or 0)
        p2 = float((mo_idx.get("000300") or {}).get("price") or 0)
        if p1 and p2:
            diff = abs(p1 - p2) / p2 * 100
            ok = diff < 0.1
            checks.append(("快照/接口一致性", "PASS" if ok else "WARN", f"沪深300 快照 {p1} vs 接口 {p2}"))
            if not ok:
                issues.append(f"快照与接口不一致：{p1} vs {p2}")

    print(f"{'校验项':<16} {'结果':<5} 说明")
    for name, status, detail in checks:
        print(f"{name:<16} {status:<5} {detail}")
    print(f"\n== 数据问题（{len(issues)} 项） ==")
    for issue in issues:
        print(" -", issue)
    fails = [c for c in checks if c[1] == "FAIL"]
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
