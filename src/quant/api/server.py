"""本地 FastAPI 服务 + 静态看板挂载（契约 §4 / §9）。

端点：
- ``GET /api/health``   健康检查（信号条数、DuckDB 状态库路径、服务时间；只读连接）
- ``GET /api/signals``  契约 §4 信号 JSON 列表（按 asof 倒序，可 ?limit= / ?symbol=）
- ``GET /api/paper``    模拟盘账户快照（现金/权益/持仓/今日盈亏/累计收益/交易胜率/最近订单）
- ``GET /api/equity``   净值曲线数组（``[{ts,cash,market_value,equity}]``，可 ?limit=）
- ``GET /api/backtest`` 读取 ``data/reports/backtest.json``，不存在返回 404 + 提示
- ``GET /api/stream``   SSE：首帧推一次信号，之后每 30s 心跳
- ``GET /``             若 ``frontend/index.html`` 存在则返回看板，否则返回 404 提示
- ``/static/*``         挂载 ``frontend/`` 目录（前端由 frontend-dev 提供，本模块只挂载）

本服务只读行情/信号并操作本地模拟盘，不接券商、不下真实单。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from quant.config import PROJECT_ROOT, load_config, project_path
from quant.data import db as dbmod
from quant.paper import engine

TZ = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
SERVICE_NAME = "personal-quant-api"
FRONTEND_DIR = PROJECT_ROOT / "frontend"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_db_path(cfg: dict[str, Any]) -> Path | None:
    try:
        return engine.db_path(cfg)
    except Exception:  # 配置不完整时健康检查不应 500
        return None


def _read_signal_rows(cfg: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """从 DuckDB ``signals`` 表读原始行（**只读连接**；库/表不存在返回 []）。"""
    db_file = _safe_db_path(cfg)
    if db_file is None:
        return []
    try:
        with dbmod.connect(db_file, read_only=True) as conn:
            if not dbmod.table_exists(conn, "signals"):
                return []
            try:
                return dbmod.fetch_dicts(
                    conn,
                    "SELECT * FROM signals ORDER BY COALESCE(\"asof\", created_at, '') DESC, signal_id DESC LIMIT ?",
                    [int(limit)],
                )
            except duckdb.Error:  # 列缺失等异常时退化为无序读取
                return dbmod.fetch_dicts(
                    conn, "SELECT * FROM signals LIMIT ?", [int(limit)]
                )
    except (FileNotFoundError, dbmod.DatabaseUnavailable, duckdb.Error, OSError):
        return []


def _count_signals(cfg: dict[str, Any]) -> int:
    """signals 表行数（只读连接；库/表不存在返回 0）。"""
    db_file = _safe_db_path(cfg)
    if db_file is None:
        return 0
    try:
        with dbmod.connect(db_file, read_only=True) as conn:
            return dbmod.count_rows(conn, "signals")
    except (FileNotFoundError, dbmod.DatabaseUnavailable, duckdb.Error, OSError):
        return 0


def _parse_top_features(raw: Any) -> list[dict[str, Any]]:
    """把 top_features 解析成 [{name,value,contrib}]，按 |contrib| 降序最多 5 条。"""
    data: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
    items: list[dict[str, Any]] = []
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("name") is not None:
                items.append(
                    {
                        "name": str(entry.get("name")),
                        "value": _as_float(entry.get("value")),
                        "contrib": _as_float(entry.get("contrib")),
                    }
                )
    elif isinstance(data, dict):
        for name, value in data.items():
            items.append({"name": str(name), "value": _as_float(value), "contrib": 0.0})
    items.sort(key=lambda item: abs(item["contrib"]), reverse=True)
    return items[:5]


def _paper_context(snap: dict[str, Any]) -> dict[str, Any]:
    positions = {str(p.get("symbol")): p for p in snap.get("positions") or []}
    return {
        "equity": _as_float(snap.get("equity")),
        "today_pnl": _as_float(snap.get("today_pnl")),
        "win_rate": snap.get("win_rate"),
        "positions": positions,
    }


def _signal_payload(row: dict[str, Any], paper_ctx: dict[str, Any]) -> dict[str, Any]:
    """DB 行 → 契约 §4 信号 JSON（字段只增不改）。"""
    symbol = str(row.get("symbol") or "")
    prob = {
        "up": _as_float(row.get("prob_up")),
        "flat": _as_float(row.get("prob_flat")),
        "down": _as_float(row.get("prob_down")),
    }
    confidence = _as_float(row.get("confidence"))
    if confidence <= 0:
        confidence = max(prob.values())
    direction = str(row.get("direction") or "flat").strip().lower()
    if direction not in {"up", "flat", "down"}:
        direction = "flat"
    position = paper_ctx["positions"].get(symbol) or {}
    return {
        "schema": SCHEMA_VERSION,
        "symbol": symbol,
        "name": str(row.get("name") or ""),
        "asof": row.get("asof"),
        "trade_date": row.get("trade_date"),
        "price": _as_float(row.get("price")),
        "direction": direction,
        "prob": {
            "up": round(prob["up"], 6),
            "flat": round(prob["flat"], 6),
            "down": round(prob["down"], 6),
        },
        "confidence": round(confidence, 4),
        "horizon": str(row.get("horizon") or "1d"),
        "expires_at": row.get("expires_at"),
        "model_version": str(row.get("model_version") or ""),
        "status": str(row.get("status") or "ok"),
        "top_features": _parse_top_features(row.get("top_features")),
        # 契约 §4「字段只增不改」允许的扩展字段：来源/落库时间，便于前端溯源
        "source": str(row.get("source") or "local"),
        "created_at": row.get("created_at"),
        "paper": {
            "equity": round(paper_ctx["equity"], 2),
            "position_qty": int(_as_float(position.get("qty"))),
            "today_pnl": round(paper_ctx["today_pnl"], 2),
        },
    }


def _signals_file_path(cfg: dict[str, Any]) -> Path:
    """``python -m quant.cli signal`` 的产物位置（契约 §7：data/reports/signals.json）。"""
    data_cfg = cfg.get("data") if isinstance(cfg.get("data"), dict) else {}
    data_dir = str(data_cfg.get("dir") or "data")
    return project_path(data_dir) / "reports" / "signals.json"


def _read_signals_file(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """DB 里还没有信号时，兜底读 CLI ``signal`` 生成的 signals.json（只影响展示，不影响模拟盘）。"""
    path = _signals_file_path(cfg)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        items = [item for item in data if isinstance(item, dict)]
    elif isinstance(data, dict):
        items = [data]
    else:
        return []
    items.sort(key=lambda item: str(item.get("asof") or item.get("created_at") or ""), reverse=True)
    return items


def _file_signal_payload(item: dict[str, Any], paper_ctx: dict[str, Any]) -> dict[str, Any]:
    """把 signals.json 里的契约 §4 JSON 补上 paper 上下文后原样返回。"""
    symbol = str(item.get("symbol") or "")
    direction = str(item.get("direction") or "flat").strip().lower()
    if direction not in {"up", "flat", "down"}:
        direction = "flat"
    prob_raw = item.get("prob") if isinstance(item.get("prob"), dict) else {}
    prob = {
        "up": _as_float(prob_raw.get("up", item.get("prob_up"))),
        "flat": _as_float(prob_raw.get("flat", item.get("prob_flat"))),
        "down": _as_float(prob_raw.get("down", item.get("prob_down"))),
    }
    confidence = _as_float(item.get("confidence"))
    if confidence <= 0:
        confidence = max(prob.values())
    position = paper_ctx["positions"].get(symbol) or {}
    return {
        "schema": SCHEMA_VERSION,
        "symbol": symbol,
        "name": str(item.get("name") or ""),
        "asof": item.get("asof"),
        "trade_date": item.get("trade_date"),
        "price": _as_float(item.get("price")),
        "direction": direction,
        "prob": {key: round(value, 6) for key, value in prob.items()},
        "confidence": round(confidence, 4),
        "horizon": str(item.get("horizon") or "1d"),
        "expires_at": item.get("expires_at"),
        "model_version": str(item.get("model_version") or ""),
        "status": str(item.get("status") or "ok"),
        "top_features": _parse_top_features(item.get("top_features")),
        "source": str(item.get("source") or "local"),
        "created_at": item.get("created_at"),
        "paper": {
            "equity": round(paper_ctx["equity"], 2),
            "position_qty": int(_as_float(position.get("qty"))),
            "today_pnl": round(paper_ctx["today_pnl"], 2),
        },
    }


def _signals_payload(cfg: dict[str, Any], limit: int = 200, symbol: str | None = None) -> list[dict[str, Any]]:
    """信号列表：signals 表优先；表为空时兜底读 data/reports/signals.json（便于本地看板先看到信号）。"""
    limit = max(int(limit), 1)
    rows = _read_signal_rows(cfg, limit)
    try:
        snap = engine.snapshot(cfg)
    except Exception:
        snap = {"equity": 0.0, "today_pnl": 0.0, "positions": []}
    ctx = _paper_context(snap)
    if rows:
        payload = []
        for row in rows:
            if symbol and str(row.get("symbol") or "") != str(symbol):
                continue
            payload.append(_signal_payload(row, ctx))
        return payload[:limit]

    payload = []
    for item in _read_signals_file(cfg):
        if symbol and str(item.get("symbol") or "") != str(symbol):
            continue
        payload.append(_file_signal_payload(item, ctx))
    return payload[:limit]


def _health_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    db = _safe_db_path(cfg)
    signal_count = _count_signals(cfg)
    return {
        "status": "ok",
        "ok": True,
        "schema": SCHEMA_VERSION,
        "service": SERVICE_NAME,
        "time": _now_iso(),
        "db": str(db) if db else None,
        "signals": signal_count,
    }


def _backtest_report_path(cfg: dict[str, Any]) -> Path:
    backtest_cfg = cfg.get("backtest") if isinstance(cfg.get("backtest"), dict) else {}
    report_dir = str(backtest_cfg.get("report_dir") or "data/reports")
    return project_path(report_dir) / "backtest.json"


def _sse(event: str, payload: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# App 工厂
# ---------------------------------------------------------------------------
def create_app(cfg: dict[str, Any] | None = None) -> FastAPI:
    """按配置创建 FastAPI 应用（CLI ``serve`` 与 TestClient 共用）。"""
    cfg = cfg if cfg is not None else load_config()
    app = FastAPI(
        title="个人量化工作台 · 本地 API",
        description="A股方向信号 + 模拟盘看板（仅供研究，非投资建议）",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.cfg = cfg
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return _health_payload(cfg)

    @app.get("/api/signals")
    def signals(
        limit: int = Query(200, ge=1, le=1000, description="最多返回条数"),
        symbol: str | None = Query(None, description="只看某个代码"),
    ) -> list[dict[str, Any]]:
        return _signals_payload(cfg, limit=limit, symbol=symbol)

    @app.get("/api/paper")
    def paper() -> Any:
        try:
            return engine.snapshot(cfg)
        except Exception as exc:  # 数据库异常时返回可读错误而非裸 500 堆栈
            return JSONResponse(status_code=500, content={"error": "paper_snapshot_failed", "message": str(exc)})

    @app.get("/api/equity")
    def equity(limit: int = Query(500, ge=1, le=5000, description="最多返回净值点数")) -> Any:
        try:
            snap = engine.snapshot(cfg)
        except Exception as exc:
            return JSONResponse(status_code=500, content={"error": "equity_failed", "message": str(exc)})
        curve = list(snap.get("curve") or [])
        if limit:
            curve = curve[-limit:]
        return curve  # 契约/任务要求：净值曲线数组

    @app.get("/api/backtest")
    def backtest() -> Any:
        path = _backtest_report_path(cfg)
        if not path.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "error": "backtest_report_not_found",
                    "message": "尚未生成回测报告，请先运行: python -m quant.cli backtest",
                    "path": str(path),
                },
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "backtest_report_invalid",
                    "message": f"回测报告读取/解析失败: {exc}",
                    "path": str(path),
                },
            )

    @app.get("/api/events")
    def events(limit: int = Query(40, ge=1, le=200, description="最多返回条数")) -> Any:
        path = cfg_path(cfg, "data.dir") / "reports" / "events.json"
        if not path.is_file():
            return JSONResponse(status_code=404, content={"error": "events_not_found", "message": "暂无事件数据"})
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return JSONResponse(status_code=500, content={"error": "events_invalid", "message": f"事件数据读取失败: {exc}"})
        items = list(data.get("items") or [])[:limit]
        return {
            "schema": 1,
            "ok": True,
            "items": items,
            "counts": data.get("counts", {}),
            "event_score": data.get("event_score"),
        }

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        """SSE：首帧推最新信号，之后 30s 心跳（任务书「可选 SSE」）。"""

        async def event_gen():
            try:
                payload = _signals_payload(cfg, limit=100)
            except Exception:
                payload = []
            yield _sse("signals", payload)
            while True:
                await asyncio.sleep(30)
                yield _sse("heartbeat", {"time": _now_iso(), "schema": SCHEMA_VERSION})

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/")
    def index() -> Any:
        index_file = FRONTEND_DIR / "index.html"
        if index_file.is_file():
            return FileResponse(str(index_file), media_type="text/html")
        return JSONResponse(
            status_code=404,
            content={
                "error": "frontend_missing",
                "message": "frontend/index.html 尚未就绪（前端由 frontend-dev 提供）；API 可直接访问 /api/health",
            },
        )

    if FRONTEND_DIR.is_dir():
        # /static/<file> 与 /<file> 都能访问，前端用相对路径（./app.js）即可
        app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend-root")
    return app


def run(port: int = 8787, cfg: dict[str, Any] | None = None) -> None:
    """启动本地服务（uvicorn）。"""
    import uvicorn

    cfg = cfg if cfg is not None else load_config()
    app = create_app(cfg)
    api_cfg = cfg.get("api") if isinstance(cfg.get("api"), dict) else {}
    host = str(api_cfg.get("host") or "127.0.0.1")
    print(f"[serve] 本地 API + 看板: http://{host}:{int(port)}/  (Ctrl+C 退出)")
    uvicorn.run(app, host=host, port=int(port), log_level="info")


__all__ = ["create_app", "run", "SCHEMA_VERSION"]
