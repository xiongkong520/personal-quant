"""百炼 AI 分析（qwen4-max / kimi-k3.1-pro）：事件利好利空语义分析 + 每日收评文案。

用途：
- 替代/补充关键词规则版「重大事件」：对事件做语义判断（利好/利空/影响板块/强度）
- 生成更专业的午评/收评文案（含判断与关注点，不构成投资建议）
- 云端展示：结果写 data/reports/ai_reports.json → export_cloud 推送 → 页面展示

配置：/root/.bailian/config.json 的 api_key（或环境变量 DASHSCOPE_API_KEY）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

CONFIG_PATH = Path("/root/.bailian/config.json")
API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_MODEL = "qwen4-max"
FALLBACK_MODEL = "kimi-k3.1-pro"


def _api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if key:
        return key
    if CONFIG_PATH.is_file():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        key = str(cfg.get("api_key") or "").strip()
        if key:
            return key
    raise RuntimeError("缺少百炼 api_key（DASHSCOPE_API_KEY 或 /root/.bailian/config.json）")


def chat(prompt: str, model: str = DEFAULT_MODEL, max_tokens: int = 1200, temperature: float = 0.3) -> str:
    """调用百炼 OpenAI 兼容接口（带重试与备用模型），返回文本。"""
    import time as _time

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            resp = httpx.post(
                API_URL,
                headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=120,
            )
            if resp.status_code == 200:
                payload = resp.json()
                return str(payload["choices"][0]["message"]["content"]).strip()
            last_err = RuntimeError(f"百炼调用失败 {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        _time.sleep(1.5)
    if model != FALLBACK_MODEL:
        return chat(prompt, model=FALLBACK_MODEL, max_tokens=max_tokens, temperature=temperature)
    raise RuntimeError(f"百炼调用失败（已重试）：{last_err}")


def analyze_events(events: list[dict[str, Any]], model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """事件语义分析：返回利好/利空/影响板块/一句话判断。"""
    top = []
    for item in events[:12]:
        top.append({
            "title": str(item.get("title", ""))[:80],
            "source": item.get("source"),
            "rule_sentiment": item.get("label"),
        })
    prompt = (
        "你是 A 股事件驱动分析助手。下面是今日重要财经事件（标题与规则初判）。"
        "输出严格 JSON（不要 markdown、不要多余文字），字段精简：\n"
        '{"bullish":[{"title":"事件简称","reason":"不超过25字","sectors":["板块"]}],'
        '"bearish":[...],"summary":"60字内事件情绪总结","watch":["关注点1","关注点2"]}'
        "\n要求：bullish/bearish 各最多 3 条；只挑影响最大的；不确定归中性；不构成投资建议。"
        "\n事件列表：" + json.dumps(top, ensure_ascii=False)
    )
    text = chat(prompt, model=model, max_tokens=1200)
    parsed = _parse_json(text)
    if "raw" in parsed:
        # 兜底：再要一次纯 JSON
        text2 = chat(prompt + "\n只输出JSON，不要任何解释。", model=model, max_tokens=1200, temperature=0.1)
        parsed = _parse_json(text2)
    return parsed


def daily_summary(market: dict[str, Any], regime: dict[str, Any] | None = None, model: str = DEFAULT_MODEL) -> str:
    """生成收评文案（150-250 字，专业、有判断、带免责）。"""
    snapshot = (market or {}).get("snapshot") or market or {}
    sentiment = snapshot.get("sentiment") or {}
    activity = snapshot.get("activity") or {}
    indices = snapshot.get("indices") or []
    idx_text = "；".join(
        f"{i.get('name')} {i.get('price')}（{i.get('pct_chg'):+.2f}%）" for i in indices[:4]
    )
    emotion = ""
    if regime:
        em = regime.get("emotion") or {}
        emotion = f"情绪 {em.get('score')}（{em.get('label')}），赚钱效应 {em.get('breadth_score')}% · 投机热度 {em.get('heat_score')}%"
    prompt = (
        "你是 A 股盘后复盘分析师。基于以下数据写一段收评（180-260 字），要求：\n"
        "1) 先说指数与成交额量能；2) 说涨跌家数/涨停跌停/情绪（用给定数据，不要编造）；"
        "3) 说主线与资金方向（可参考大盘状态数据）；4) 给一句明确判断（进攻/防守/分化/退潮）与明日关注点；"
        "5) 语言专业简洁，不模棱两可；6) 结尾不需要免责声明（前端会加）。\n"
        f"数据：\n指数：{idx_text}\n"
        f"涨跌家数：涨 {activity.get('up')} / 跌 {activity.get('down')}，涨停 {activity.get('limit_up')} / 跌停 {activity.get('limit_down')}\n"
        f"情绪：{sentiment.get('label')}（{sentiment.get('score')}）{emotion}\n"
        f"大盘状态：{json.dumps((regime or {}).get('market_state') or {}, ensure_ascii=False)[:300]}\n"
        f"主线：{json.dumps((regime or {}).get('leaders') or [], ensure_ascii=False)[:400]}\n"
    )
    return chat(prompt, model=model, max_tokens=800)


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {"raw": text}


if __name__ == "__main__":
    # 自检：用真实报告数据生成一条 AI 分析
    reports = Path("data/reports")
    events = []
    if (reports / "events.json").is_file():
        events = json.loads((reports / "events.json").read_text(encoding="utf-8")).get("items") or []
    market = json.loads((reports / "market.json").read_text(encoding="utf-8")) if (reports / "market.json").is_file() else {}
    regime = json.loads((reports / "regime.json").read_text(encoding="utf-8")) if (reports / "regime.json").is_file() else {}
    print("== AI 事件分析 ==")
    print(json.dumps(analyze_events(events), ensure_ascii=False, indent=1)[:1200])
    print("== AI 收评 ==")
    print(daily_summary(market, regime)[:600])
