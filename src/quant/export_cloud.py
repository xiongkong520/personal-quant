"""把本地最新信号推送到 Cloudflare Worker（POST /api/ingest）。

用法：
    QUANT_INGEST_TOKEN=xxx python -m quant.cli export --target cloudflare

token 只从环境变量读，不落盘、不进 git。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from quant.config import cfg_path


def _latest_signals_from_db(cfg: dict) -> list[dict[str, Any]]:
    """优先读 data/reports/signals.json；否则从 DB 读每个 symbol 的最新信号。"""
    report = cfg_path(cfg, "data.dir") / "reports" / "signals.json"
    if report.exists():
        return json.loads(report.read_text(encoding="utf-8"))

    from quant.data.store import read_latest_signals

    return read_latest_signals(cfg_path(cfg, "data.db"), cfg["symbols"])


def export_to_cloudflare(cfg: dict, timeout: float = 10.0) -> dict[str, Any]:
    """读取本地信号并推送到 Worker。返回 {"pushed": n, "worker": url}。"""
    token = os.environ.get("QUANT_INGEST_TOKEN", "")
    if not token:
        raise SystemExit("缺少环境变量 QUANT_INGEST_TOKEN（与 Worker 端 .dev.vars 一致）")

    worker_url = cfg["cloudflare"]["worker_url"].rstrip("/")
    signals = _latest_signals_from_db(cfg)
    if not signals:
        return {"pushed": 0, "worker": worker_url, "note": "本地没有信号，先跑 quant.cli signal"}

    resp = httpx.post(
        f"{worker_url}/api/ingest",
        json={"schema": 1, "items": signals},
        headers={"x-ingest-token": token},
        timeout=timeout,
    )
    resp.raise_for_status()
    return {"pushed": len(signals), "worker": worker_url, "status": resp.status_code}
