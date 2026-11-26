"""把本地研究产物推送到云端（Pages Functions / Worker）：
- 个股信号 /api/ingest
- 市场快照 /api/market/ingest
- 重大事件（利好利空） /api/events/ingest
- 回测报告 /api/backtest/ingest
- 模拟盘快照 /api/paper/ingest
- 历史 K 线（股票池 × 最近 N 天日线） /api/kline/ingest
- 大盘状态/主线/仓位快照 /api/regime/ingest

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


def _zfill6(value: Any) -> str:
    return str(value).strip().zfill(6)


def _kline_symbols(cfg: dict) -> list[str]:
    """K 线推送股票池。

    - 优先 ``cloudflare.kline_symbols``（显式列表）；
    - 否则用 ``cfg.symbols``；若它比目标池小（当前 config 仍是 3 只），
      用本地 Parquet 已采集的代码补足到 ``cloudflare.kline_pool_size``（默认 40）；
    - 目标池不含数据的代码会在推送时自动跳过，不阻塞其它标的。
    """
    cf = cfg.get("cloudflare") if isinstance(cfg.get("cloudflare"), dict) else {}
    explicit = cf.get("kline_symbols")
    if isinstance(explicit, list) and explicit:
        return list(dict.fromkeys(_zfill6(s) for s in explicit if str(s).strip()))

    symbols: list[str] = []
    for raw in cfg.get("symbols") or []:
        sym = _zfill6(raw)
        if sym not in symbols:
            symbols.append(sym)

    try:
        target = max(1, int(cf.get("kline_pool_size") or 40))
    except (TypeError, ValueError):
        target = 40
    target = min(target, 100)
    if len(symbols) < target:
        from quant.config import cfg_path
        from quant.data import store as bar_store

        daily_dir = bar_store.parquet_root(cfg_path(cfg, "data.db")) / "daily_bars"
        try:
            available = sorted(
                p.name.split("=", 1)[-1]
                for p in daily_dir.glob("symbol=*")
                if (p / "daily_bars.parquet").is_file()
            )
        except OSError:
            available = []
        for sym in available:
            if sym not in symbols:
                symbols.append(sym)
            if len(symbols) >= target:
                break
    return symbols[:target]


def push_klines(
    cfg: dict,
    *,
    base: str,
    headers: dict[str, str],
    days: int = 120,
    batch_rows: int = 200,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """把股票池最近 ``days`` 天日线分批推到云端 D1（每批 ≤ ``batch_rows`` 行）。

    返回 ``{"symbols": n, "rows": n, "batches": n, "failed": [...]}``；
    单只标的读取失败不影响其它标的（云端为幂等 upsert）。
    """
    from quant.config import cfg_path
    from quant.data import store as bar_store

    db = cfg_path(cfg, "data.db")
    days = min(max(int(days), 20), 250)
    batch_rows = min(max(int(batch_rows), 1), 500)
    symbols = _kline_symbols(cfg)
    pending: list[dict[str, Any]] = []
    pushed_rows = 0
    pushed_symbols = 0
    batches = 0
    failed: list[dict[str, str]] = []

    def flush(rows: list[dict[str, Any]]) -> None:
        nonlocal pushed_rows, batches
        if not rows:
            return
        resp = httpx.post(
            f"{base}/api/kline/ingest",
            json={"schema": 1, "items": rows},
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        pushed_rows += len(rows)
        batches += 1

    for symbol in symbols:
        try:
            frame = bar_store.read_daily(symbol, db)
        except Exception as exc:  # noqa: BLE001 - 单票失败不阻塞整体
            failed.append({"symbol": symbol, "reason": type(exc).__name__})
            continue
        if frame is None or len(frame) == 0:
            failed.append({"symbol": symbol, "reason": "no_data"})
            continue
        frame = frame.reset_index(drop=True).sort_values("trade_date").tail(days)
        rows = []
        for row in frame.to_dict(orient="records"):
            close = row.get("close")
            if close is None or close != close:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": str(row.get("trade_date") or ""),
                    "open": None if row.get("open") != row.get("open") else round(float(row.get("open")), 3),
                    "high": None if row.get("high") != row.get("high") else round(float(row.get("high")), 3),
                    "low": None if row.get("low") != row.get("low") else round(float(row.get("low")), 3),
                    "close": round(float(close), 3),
                    "volume": int(row.get("volume") or 0),
                    "amount": None if row.get("amount") != row.get("amount") else round(float(row.get("amount")), 2),
                }
            )
        if not rows:
            failed.append({"symbol": symbol, "reason": "no_rows"})
            continue
        pushed_symbols += 1
        pending.extend(rows)
        while len(pending) >= batch_rows:
            chunk = pending[:batch_rows]
            pending = pending[batch_rows:]
            try:
                flush(chunk)
            except Exception as exc:  # noqa: BLE001 - 该批失败记录下来继续其它标的
                failed.append({"symbol": "*", "reason": f"batch: {type(exc).__name__}: {exc}"})
                break

    try:
        flush(pending)
    except Exception as exc:  # noqa: BLE001 - 最后一批失败记入结果
        failed.append({"symbol": "*", "reason": f"{type(exc).__name__}: {exc}"})

    return {
        "symbols": pushed_symbols,
        "rows": pushed_rows,
        "batches": batches,
        "days": days,
        "failed": failed[:10],
    }


def push_regime(cfg: dict, *, base: str, headers: dict[str, str], timeout: float = 20.0) -> dict[str, Any]:
    """推送 data/reports/regime.json（大盘状态/情绪/主线/仓位建议）。"""
    from quant.config import cfg_path

    path = cfg_path(cfg, "data.dir") / "reports" / "regime.json"
    data = _read_json(path)
    if not data:
        return {"pushed": 0, "note": "无大盘状态快照，先跑 scripts/regime_report.py"}
    resp = httpx.post(
        f"{base}/api/regime/ingest",
        json={"schema": 1, "snapshot": data},
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    state = ((data.get("market_state") or {}).get("label")) or data.get("status")
    return {"pushed": 1, "status": resp.status_code, "state": state, "asof": data.get("asof")}


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

    try:
        result["klines"] = push_klines(cfg, base=base, headers=headers, timeout=max(timeout, 30.0))
    except Exception as exc:  # noqa: BLE001 - K 线推送失败不阻塞其它产物
        result["klines"] = {"pushed": 0, "error": f"{type(exc).__name__}: {exc}"}

    try:
        result["regime"] = push_regime(cfg, base=base, headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        result["regime"] = {"pushed": 0, "error": f"{type(exc).__name__}: {exc}"}

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

    ai = _read_json(reports / "ai_reports.json")
    if ai:
        resp = httpx.post(f"{base}/api/ai/ingest", json={"schema": 1, "insight": ai}, headers=headers, timeout=timeout)
        resp.raise_for_status()
        result["ai"] = {"pushed": 1, "status": resp.status_code}
    else:
        result["ai"] = {"pushed": 0, "note": "无 AI 报告，先跑 python -m quant.data.ai_analysis"}

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
