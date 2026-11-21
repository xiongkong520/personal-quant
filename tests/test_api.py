"""本地 API 单测（契约 §4 / §9）：TestClient 打满全部端点，含 /api/backtest 404 分支。

全部离线：fixture SQLite + 临时目录，不联网、不依赖 data-dev / frontend-dev 的产物。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quant.api import server
from quant.paper import engine

FIXTURES = Path(__file__).parent / "fixtures" / "paper"
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))

import paper_seed  # noqa: E402


def _make_client(tmp_path: Path, scenario, *, days: int = 3, initial_cash: float = 100000):
    """构造 fixture 库 + 推进模拟盘 + 创建 TestClient。"""
    bars, signals = scenario()
    db = paper_seed.create_db(tmp_path / "quant.db", bars, signals)
    cfg = paper_seed.make_cfg(db, initial_cash=initial_cash)
    cfg["backtest"]["report_dir"] = str(tmp_path / "reports")
    engine.init_account(initial_cash, db)
    if days:
        engine.advance(days, cfg)
    app = server.create_app(cfg)
    return TestClient(app), cfg, db


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------
def test_health(tmp_path: Path) -> None:
    client, cfg, db = _make_client(tmp_path, paper_seed.scenario_a, days=0)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["schema"] == 1
    assert body["service"] == "personal-quant-api"
    assert body["signals"] == 2
    assert body["db"] == str(db)
    assert "time" in body


def test_health_with_missing_db(tmp_path: Path) -> None:
    cfg = paper_seed.make_cfg(tmp_path / "not_exists.db", initial_cash=100000)
    client = TestClient(server.create_app(cfg))
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["signals"] == 0


# ---------------------------------------------------------------------------
# /api/signals —— 契约 §4 字段
# ---------------------------------------------------------------------------
def test_signals_contract_json(tmp_path: Path) -> None:
    client, cfg, db = _make_client(tmp_path, paper_seed.scenario_a, days=1)
    resp = client.get("/api/signals")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and len(body) == 2

    # 按 asof 倒序
    asofs = [item["asof"] for item in body]
    assert asofs == sorted(asofs, reverse=True)

    for item in body:
        assert item["schema"] == 1
        assert item["symbol"] == "600519"
        assert isinstance(item["name"], str)
        assert item["trade_date"] in {"2026-11-16", "2026-11-17"}
        assert item["direction"] in {"up", "flat", "down"}
        assert set(item["prob"]) == {"up", "flat", "down"}
        assert abs(sum(item["prob"].values()) - 1.0) < 1e-6
        assert item["confidence"] == pytest.approx(max(item["prob"].values()), abs=5e-5)
        assert item["horizon"] == "1d"
        assert item["model_version"] == "v0.1.0"
        assert item["status"] == "ok"
        assert 1 <= len(item["top_features"]) <= 5
        contribs = [abs(f["contrib"]) for f in item["top_features"]]
        assert contribs == sorted(contribs, reverse=True), "top_features 需按 |contrib| 降序"
        for feature in item["top_features"]:
            assert set(feature) == {"name", "value", "contrib"}
        paper = item["paper"]
        assert set(paper) == {"equity", "position_qty", "today_pnl"}
        assert paper["equity"] == pytest.approx(100545.80, abs=0.01)
        assert paper["position_qty"] == 1900, "推进 1 天后持有 1900 股"


def test_signals_query_params(tmp_path: Path) -> None:
    client, cfg, db = _make_client(tmp_path, paper_seed.scenario_a, days=0)
    assert len(client.get("/api/signals?limit=1").json()) == 1
    assert client.get("/api/signals?symbol=000001").json() == []
    assert len(client.get("/api/signals?symbol=600519").json()) == 2


def test_signals_empty_when_db_missing(tmp_path: Path) -> None:
    cfg = paper_seed.make_cfg(tmp_path / "none.db", initial_cash=100000)
    client = TestClient(server.create_app(cfg))
    resp = client.get("/api/signals")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# /api/signals —— DB 尚无信号时兜底读 data/reports/signals.json（CLI signal 产物）
# ---------------------------------------------------------------------------
def test_signals_fallback_to_reports_json(tmp_path: Path) -> None:
    cfg = paper_seed.make_cfg(tmp_path / "quant.db", initial_cash=100000)
    data_dir = tmp_path / "data"
    (data_dir / "reports").mkdir(parents=True)
    contract_signal = {
        "schema": 1,
        "symbol": "600519",
        "name": "贵州茅台",
        "asof": "2026-11-21T14:35:00+08:00",
        "trade_date": "2026-11-21",
        "price": 1680.5,
        "direction": "up",
        "prob": {"up": 0.42, "flat": 0.31, "down": 0.27},
        "confidence": 0.42,
        "horizon": "1d",
        "expires_at": "2026-11-24T09:30:00+08:00",
        "model_version": "v0.1.0",
        "status": "ok",
        "top_features": [{"name": "ret_5d", "value": 0.031, "contrib": 0.08}],
        "source": "local",
    }
    (data_dir / "reports" / "signals.json").write_text(
        json.dumps([contract_signal], ensure_ascii=False), encoding="utf-8"
    )
    cfg["data"]["dir"] = str(data_dir)

    client = TestClient(server.create_app(cfg))
    body = client.get("/api/signals").json()
    assert len(body) == 1
    item = body[0]
    assert item["schema"] == 1
    assert item["symbol"] == "600519"
    assert item["name"] == "贵州茅台"
    assert item["direction"] == "up"
    assert abs(sum(item["prob"].values()) - 1.0) < 1e-6
    assert item["confidence"] == pytest.approx(0.42)
    assert item["top_features"] == [{"name": "ret_5d", "value": 0.031, "contrib": 0.08}]
    assert item["paper"]["equity"] == pytest.approx(100000.0)


def test_signals_db_takes_precedence_over_file(tmp_path: Path) -> None:
    client, cfg, db = _make_client(tmp_path, paper_seed.scenario_a, days=0)
    data_dir = tmp_path / "data2"
    (data_dir / "reports").mkdir(parents=True)
    (data_dir / "reports" / "signals.json").write_text(
        json.dumps([{"symbol": "000001", "direction": "up", "asof": "2020-01-01T00:00:00+08:00"}]),
        encoding="utf-8",
    )
    cfg["data"]["dir"] = str(data_dir)

    client2 = TestClient(server.create_app(cfg))
    body = client2.get("/api/signals").json()
    assert len(body) == 2, "DB 有信号时以 DB 为准，不读文件兜底"
    assert {item["symbol"] for item in body} == {"600519"}


# ---------------------------------------------------------------------------
# /api/paper —— 模拟盘快照（含胜率）
# ---------------------------------------------------------------------------
def test_paper_snapshot(tmp_path: Path) -> None:
    client, cfg, db = _make_client(tmp_path, paper_seed.scenario_a, days=3)
    resp = client.get("/api/paper")
    assert resp.status_code == 200
    snap = resp.json()

    for key in (
        "cash",
        "equity",
        "market_value",
        "positions",
        "today_pnl",
        "total_return",
        "win_rate",
        "closed_trades",
        "winning_trades",
        "losing_trades",
        "orders",
        "curve",
        "as_of",
    ):
        assert key in snap, f"/api/paper 缺字段 {key}"

    assert snap["cash"] == pytest.approx(100701.07, abs=0.01)
    assert snap["equity"] == pytest.approx(100701.07, abs=0.01)
    assert snap["positions"] == []
    assert snap["win_rate"] == pytest.approx(1.0)
    assert snap["closed_trades"] == 1
    assert snap["winning_trades"] == 1
    assert len(snap["curve"]) == 3
    assert snap["as_of"] == "2026-11-19"
    assert [o["status"] for o in snap["orders"]] == ["filled", "filled"]


# ---------------------------------------------------------------------------
# /api/equity —— 净值曲线数组
# ---------------------------------------------------------------------------
def test_equity_returns_array(tmp_path: Path) -> None:
    client, cfg, db = _make_client(tmp_path, paper_seed.scenario_a, days=3)
    resp = client.get("/api/equity")
    assert resp.status_code == 200
    curve = resp.json()
    assert isinstance(curve, list) and len(curve) == 3
    for row in curve:
        assert set(row) == {"ts", "cash", "market_value", "equity"}
    assert curve[-1]["ts"] == "2026-11-19"
    assert curve[-1]["equity"] == pytest.approx(100701.07, abs=0.01)

    limited = client.get("/api/equity?limit=1").json()
    assert len(limited) == 1
    assert limited[0]["ts"] == "2026-11-19"


# ---------------------------------------------------------------------------
# /api/backtest —— 报告不存在 404；存在则返回 JSON
# ---------------------------------------------------------------------------
def test_backtest_404_with_hint(tmp_path: Path) -> None:
    client, cfg, db = _make_client(tmp_path, paper_seed.scenario_a, days=0)
    resp = client.get("/api/backtest")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "backtest_report_not_found"
    assert "python -m quant.cli backtest" in body["message"]
    assert "backtest.json" in body["path"]


def test_backtest_returns_report(tmp_path: Path) -> None:
    client, cfg, db = _make_client(tmp_path, paper_seed.scenario_a, days=0)
    report_dir = Path(cfg["backtest"]["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema": 1,
        "accuracy": 0.61,
        "win_rate": 0.55,
        "n": 120,
        "equity_curve": [{"date": "2025-01-02", "equity": 1.0}],
    }
    (report_dir / "backtest.json").write_text(json.dumps(report), encoding="utf-8")

    resp = client.get("/api/backtest")
    assert resp.status_code == 200
    assert resp.json()["accuracy"] == pytest.approx(0.61)
    assert resp.json()["win_rate"] == pytest.approx(0.55)


def test_backtest_invalid_report(tmp_path: Path) -> None:
    client, cfg, db = _make_client(tmp_path, paper_seed.scenario_a, days=0)
    report_dir = Path(cfg["backtest"]["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "backtest.json").write_text("{not-json", encoding="utf-8")
    resp = client.get("/api/backtest")
    assert resp.status_code == 500
    assert resp.json()["error"] == "backtest_report_invalid"


# ---------------------------------------------------------------------------
# / 与静态挂载（frontend 由 frontend-dev 提供，本模块只负责挂载/兜底提示）
# ---------------------------------------------------------------------------
def test_root_missing_frontend_hint(tmp_path: Path, monkeypatch) -> None:
    cfg = paper_seed.make_cfg(tmp_path / "quant.db", initial_cash=100000)
    monkeypatch.setattr(server, "FRONTEND_DIR", tmp_path / "no-such-frontend")
    client = TestClient(server.create_app(cfg))
    resp = client.get("/")
    assert resp.status_code == 404
    assert resp.json()["error"] == "frontend_missing"


def test_root_serves_frontend_and_static(tmp_path: Path, monkeypatch) -> None:
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text(
        "<!doctype html><html><body>模拟盘看板 dashboard</body></html>", encoding="utf-8"
    )
    (frontend_dir / "app.js").write_text("console.log('ok')", encoding="utf-8")
    monkeypatch.setattr(server, "FRONTEND_DIR", frontend_dir)

    cfg = paper_seed.make_cfg(tmp_path / "quant.db", initial_cash=100000)
    client = TestClient(server.create_app(cfg))

    index = client.get("/")
    assert index.status_code == 200
    assert "模拟盘看板" in index.text
    assert index.headers["content-type"].startswith("text/html")

    asset = client.get("/static/app.js")
    assert asset.status_code == 200
    assert "console.log" in asset.text
    # 前端也可用相对路径引用（挂在根目录），不强制 /static 前缀
    root_asset = client.get("/app.js")
    assert root_asset.status_code == 200
    assert "console.log" in root_asset.text


# ---------------------------------------------------------------------------
# 空账户 API：快照自动初始化，净值曲线为空数组
# ---------------------------------------------------------------------------
def test_paper_snapshot_on_fresh_account(tmp_path: Path) -> None:
    cfg = paper_seed.make_cfg(tmp_path / "fresh.db", initial_cash=100000)
    client = TestClient(server.create_app(cfg))

    snap = client.get("/api/paper").json()
    assert snap["cash"] == pytest.approx(100000.0)
    assert snap["equity"] == pytest.approx(100000.0)
    assert snap["positions"] == []
    assert snap["win_rate"] is None
    assert snap["as_of"] is None

    assert client.get("/api/equity").json() == []
