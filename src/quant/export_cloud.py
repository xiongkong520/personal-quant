"""把本地研究产物推送到云端（Pages Functions / Worker）：
- 个股信号 /api/ingest
- 市场快照 /api/market/ingest
- 重大事件（利好利空） /api/events/ingest
- 回测报告 /api/backtest/ingest
- 模拟盘快照 /api/paper/ingest

用法：
    QUANT_INGEST_TOKEN=xxx python -m quant.cli export --target cloudflare

token 只从环境变量读，不落盘、不进 git。
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from quant.config import cfg_path


def _read_json(path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _latest_signals(cfg: dict) -> list[dict[str, Any]]:
    report = cfg_path(cfg, "data.dir") / "reports" / "signals.json"
    parsed = _read_json(report)
    if parsed:
        return parsed
    from quant.data.store import read_latest_signals

    return read_latest_signals(cfg_path(cfg, "data.db"), cfg["symbols"])


def export_to_cloudflare(cfg: dict, timeout: float = 20.0) -> dict[str, Any]:
    """读取本地研究产物并推送到云端。返回各模块推送统计。"""
    token = os.environ.get("QUANT_INGEST_TOKEN", "")
    if not token:
        raise SystemExit("缺少环境变量 QUANT_INGEST_TOKEN（与云端 secret 一致）")

    base = cfg["cloudflare"]["worker_url"].rstrip("/")
    headers = {"x-ingest-token": token, "User-Agent": "quant-local/1.0"}
    result: dict[str, Any] = {"worker": base}
    reports = cfg_path(cfg, "data.dir") / "reports"

    signals = _latest_signals(cfg)
    if signals:
        resp = httpx.post(f"{base}/api/ingest", json={"schema": 1, "items": signals}, headers=headers, timeout=timeout)
        resp.raise_for_status()
        result["signals"] = {"pushed": len(signals), "status": resp.status_code}
    else:
        result["signals"] = {"pushed": 0, "note": "无信号，先跑 quant.cli signal"}

    market = _read_json(reports / "market.json")
    if market:
        resp = httpx.post(f"{base}/api/market/ingest", json={"schema": 1, "snapshot": market}, headers=headers, timeout=timeout)
        resp.raise_for_status()
        result["market"] = {"pushed": 1, "status": resp.status_code,
                            "sentiment": market.get("sentiment", {}).get("label")}
    else:
        result["market"] = {"pushed": 0, "note": "无市场快照，先跑 quant.cli market"}

    events = _read_json(reports / "events.json")
    if events and events.get("items"):
        resp = httpx.post(f"{base}/api/events/ingest", json={"schema": 1, "items": events["items"]},
                          headers=headers, timeout=timeout)
        resp.raise_for_status()
        result["events"] = {"pushed": len(events["items"]), "status": resp.status_code,
                            "bullish": events.get("counts", {}).get("bullish")}
    else:
        result["events"] = {"pushed": 0, "note": "无事件快照，先跑 quant.cli events"}

    backtest = _read_json(reports / "backtest.json")
    if backtest:
        resp = httpx.post(f"{base}/api/backtest/ingest", json={"schema": 1, "snapshot": backtest},
                          headers=headers, timeout=timeout)
        resp.raise_for_status()
        result["backtest"] = {"pushed": 1, "status": resp.status_code}
    else:
        result["backtest"] = {"pushed": 0, "note": "无回测报告，先跑 quant.cli backtest"}

    try:
        from quant.paper.engine import snapshot

        paper = snapshot(cfg)
        resp = httpx.post(f"{base}/api/paper/ingest", json={"schema": 1, "snapshot": paper},
                          headers=headers, timeout=timeout)
        resp.raise_for_status()
        result["paper"] = {"pushed": 1, "status": resp.status_code, "equity": paper.get("equity")}
    except Exception as exc:  # noqa: BLE001 - 模拟盘未初始化时跳过
        result["paper"] = {"pushed": 0, "note": f"快照生成失败：{type(exc).__name__}"}

    return result
