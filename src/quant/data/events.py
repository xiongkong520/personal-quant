"""重大事件/新闻：财联社电报 + 东财财经新闻 + 新闻联播 → 利好/利空评分（契约 §14）。

规则版（无需 LLM）：关键词加权打分 + 股票代码提取。
- 强利好/利空 ±2 分，普通 ±1 分；score = clamp(sum/4, -1, 1)
- score >= 0.2 利好；<= -0.2 利空；其余中性
后续可接 LLM 做语义级判断（百炼充值后）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

TZ = timezone(timedelta(hours=8))

BULLISH = [
    ("回购", 2), ("增持", 2), ("中标", 2), ("业绩预增", 2), ("净利润增长", 2),
    ("分红", 2), ("重大合同", 2), ("获批", 2), ("批准", 1), ("政策支持", 2),
    ("降准", 2), ("降息", 2), ("补贴", 1), ("注入", 1), ("扭亏", 2),
    ("超预期", 1), ("上调", 1), ("合作", 1), ("签约", 1), ("突破", 1),
    ("增长", 1), ("盈利", 1), ("恢复", 1), ("利好", 1),
]
BEARISH = [
    ("减持", -2), ("亏损", -2), ("预减", -2), ("立案", -2), ("调查", -1),
    ("处罚", -2), ("罚款", -2), ("退市", -2), ("违规", -2), ("违约", -2),
    ("下调", -1), ("商誉减值", -2), ("质押", -1), ("解禁", -1), ("诉讼", -1),
    ("停产", -2), ("风险警示", -2), ("ST", -1), ("下降", -1), ("下滑", -1),
    ("中止", -1), ("终止", -1), ("利空", -1), ("延期", -1),
]
STRONG_SCORE = 3.0


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def extract_symbols(text: str) -> list[str]:
    codes = re.findall(r"(?<!\d)(\d{6})(?!\d)", text)
    seen: list[str] = []
    for code in codes:
        if code not in seen and code[0] in "036":
            seen.append(code)
    return seen[:3]


def score_text(text: str) -> dict[str, Any]:
    """关键词加权评分：返回 sentiment/score/hits。"""
    hits: list[dict[str, Any]] = []
    total = 0.0
    for word, weight in BULLISH + BEARISH:
        if word in text:
            total += weight
            hits.append({"word": word, "weight": weight})
    score = max(-1.0, min(1.0, total / STRONG_SCORE)) if total else 0.0
    if score >= 0.2:
        sentiment = "bullish"
        label = "利好"
    elif score <= -0.2:
        sentiment = "bearish"
        label = "利空"
    else:
        sentiment = "neutral"
        label = "中性"
    return {"sentiment": sentiment, "label": label, "score": round(score, 3), "hits": hits[:5]}


def _parse_time(raw: str) -> str | None:
    """解析多种时间格式为 +08:00 ISO；失败返回 None。"""
    raw = _clean(raw)
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw[: len(datetime.now().strftime(fmt))], fmt)
            return dt.replace(tzinfo=TZ).isoformat(timespec="seconds")
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T00:00:00+08:00"
    return None


def fetch_cls() -> list[dict[str, Any]]:
    """财联社电报（20 条，重大事件快讯）。"""
    import akshare as ak

    df = ak.stock_news_global_cls(symbol="全部")
    items: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        title = _clean(row.get("标题"))
        content = _clean(row.get("内容"))
        published = _parse_time(f"{row.get('发布日期', '')} {row.get('发布时间', '')}") or _parse_time(str(row.get("发布日期", "")))
        items.append({
            "id": f"cls-{abs(hash(title)) % 10**12}",
            "source": "财联社电报",
            "title": title,
            "summary": content[:180],
            "url": "",
            "published_at": published or _now(),
        })
    return items


def fetch_eastmoney() -> list[dict[str, Any]]:
    """东财财经新闻（200 条）。"""
    import akshare as ak

    df = ak.stock_news_global_em()
    items: list[dict[str, Any]] = []
    for _, row in df.head(120).iterrows():
        title = _clean(row.get("标题"))
        summary = _clean(row.get("摘要"))
        items.append({
            "id": f"em-{abs(hash(title)) % 10**12}",
            "source": "东方财富",
            "title": title,
            "summary": summary[:180],
            "url": _clean(row.get("链接")),
            "published_at": _parse_time(str(row.get("发布时间", ""))) or _now(),
        })
    return items


def fetch_cctv() -> list[dict[str, Any]]:
    """新闻联播（宏观政策）。"""
    import akshare as ak

    yesterday = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y%m%d")
    df = ak.alternative_news_cctv(date=yesterday)
    items: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        title = _clean(row.get("title"))
        items.append({
            "id": f"cctv-{abs(hash(title)) % 10**12}",
            "source": "新闻联播",
            "title": title,
            "summary": _clean(row.get("content"))[:180],
            "url": "",
            "published_at": _parse_time(str(row.get("date", ""))) or _now(),
        })
    return items


def build_events_snapshot(limit: int = 60) -> dict[str, Any]:
    """合并多源新闻 → 去重 → 利好/利空评分 → 事件快照。"""
    errors: list[str] = []
    raw: list[dict[str, Any]] = []
    for name, fn in (("cls", fetch_cls), ("eastmoney", fetch_eastmoney), ("cctv", fetch_cctv)):
        try:
            raw.extend(fn())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}")

    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for item in raw:
        key = item["title"][:40]
        if not key or key in seen:
            continue
        seen.add(key)
        scored = score_text(item["title"] + " " + item["summary"])
        item.update(scored)
        item["symbols"] = extract_symbols(item["title"] + " " + item["summary"])
        items.append(item)

    items.sort(key=lambda x: x["published_at"], reverse=True)
    items = items[:limit]
    bullish = [x for x in items if x["sentiment"] == "bullish"]
    bearish = [x for x in items if x["sentiment"] == "bearish"]
    neutral = [x for x in items if x["sentiment"] == "neutral"]
    total = max(1, len(items))
    event_score = round(100 * (len(bullish) * 1.0 + len(neutral) * 0.5) / total, 1)

    return {
        "schema": 1,
        "asof": _now(),
        "status": "ok" if items else "stale",
        "errors": errors,
        "counts": {"total": len(items), "bullish": len(bullish), "bearish": len(bearish), "neutral": len(neutral)},
        "event_score": event_score,
        "items": items,
    }
