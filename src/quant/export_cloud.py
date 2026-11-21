"""把本地最新信号与市场整体快照推送到云端（Pages Functions / Worker）。

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


def export_to_cloudflare(cfg: dict, timeout: float = 15.0) -> dict[str, Any]:
    """读取本地信号 + 市场快照并推送到云端。返回推送统计。"""
    token = os.environ.get("QUANT_INGEST_TOKEN", "")
    if not token:
        raise SystemExit("缺少环境变量 QUANT_INGEST_TOKEN（与云端 .dev.vars / secret 一致）")

    base = cfg["cloudflare"]["worker_url"].rstrip("/")
    headers = {"x-ingest-token": token}
    result: dict[str, Any] = {"worker": base}

    signals = _latest_signals(cfg)
    if signals:
        resp = httpx.post(f"{base}/api/ingest", json={"schema": 1, "items": signals},
                          headers=headers, timeout=timeout)
        resp.raise_for_status()
        result["signals"] = {"pushed": len(signals), "status": resp.status_code}
    else:
        result["signals"] = {"pushed": 0, "note": "无信号，先跑 quant.cli signal"}

    market = _read_json(cfg_path(cfg, "data.dir") / "reports" / "market.json")
    if market:
        resp = httpx.post(f"{base}/api/market/ingest", json={"schema": 1, "snapshot": market},
                          headers=headers, timeout=timeout)
        resp.raise_for_status()
        result["market"] = {"pushed": 1, "status": resp.status_code,
                            "sentiment": market.get("sentiment", {}).get("label")}
    else:
        result["market"] = {"pushed": 0, "note": "无市场快照，先跑 quant.cli market"}

    return result
