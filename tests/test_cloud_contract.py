"""Cloudflare Worker 契约测试（v2 · D1 瘦身 + 边缘缓存 + 市场全景）。

离线、不联网、不需要 Cloudflare 账号；只做静态/纯函数校验：
- D1 瘦身后 schema 只含 4 张轻量表 + edge_cache + stock_names（0009 迁移删除大表）；
- Worker 源码不再包含旧大表 SQL；
- index.ts 路由/鉴权/缓存/Cron 接线完整；
- 模型 §5 与 Python 双端 parity（node 直跑 TS）；
- wrangler 配置（D1/Assets/triggers）与安全默认值。

运行时行为（market-overview 字段契约、Cache API 不可用时的 D1 二级缓存 MISS→HIT→STALE）
见 tests/test_market_overview_contract.py。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare" / "worker"
NODE = shutil.which("node")

HEAVY_TABLES = [
    "signals",
    "market_snapshots",
    "events",
    "backtest_reports",
    "paper_snapshots",
    "klines",
    "regime_snapshots",
    "boards",
    "intraday_index",
    "market_summaries",
]
LIGHT_TABLES = ["account_snapshot", "positions", "order_log", "signal_log"]
KEEP_TABLES = LIGHT_TABLES + ["edge_cache", "stock_names"]


def _run_node(*args: str, timeout: int = 240) -> subprocess.CompletedProcess:
    assert NODE is not None
    return subprocess.run([NODE, *args], cwd=str(WORKER), capture_output=True, text=True, timeout=timeout)


requires_node = pytest.mark.skipif(NODE is None, reason="需要 node 24")
requires_esbuild = pytest.mark.skipif(not (WORKER / "node_modules" / "esbuild").exists(), reason="未 npm install（esbuild）")
requires_wrangler = pytest.mark.skipif(
    not (WORKER / "node_modules" / ".bin" / "wrangler").exists(), reason="未 npm install（wrangler）"
)


# ---------------------------------------------------------------------------
# D1 瘦身后 schema
# ---------------------------------------------------------------------------


def test_schema_final_state_only_light_tables():
    sql = (WORKER / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql)  # 历史 CREATE + 0009 DROP 后的最终状态
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables & set(LIGHT_TABLES) == set(LIGHT_TABLES), f"缺少轻量表：{LIGHT_TABLES}"
    for heavy in HEAVY_TABLES:
        assert heavy not in tables, f"大表 {heavy} 应在 0009 迁移后删除"
    for keep in ("edge_cache", "stock_names"):
        assert keep in tables


def test_migration_0009_drops_heavy_tables():
    path = WORKER / "migrations" / "0009_light_tables_drop_heavy.sql"
    assert path.exists(), "缺少 0009 D1 瘦身迁移"
    sql = path.read_text(encoding="utf-8")
    for heavy in HEAVY_TABLES:
        assert re.search(rf"DROP TABLE IF EXISTS {heavy}\b", sql), f"0009 未删除 {heavy}"
    for light in LIGHT_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {light}" in sql, f"0009 未创建 {light}"


def test_seed_uses_light_tables_only():
    seed = (WORKER / "seed.sql").read_text(encoding="utf-8")
    for heavy in ("INTO signals", "INTO market_snapshots", "INTO events", "INTO klines"):
        assert heavy not in seed, f"seed.sql 仍在写已删除表：{heavy}"
    assert "INTO signal_log" in seed and "INTO account_snapshot" in seed


def test_worker_src_has_no_old_table_sql():
    pattern = re.compile(
        r"\b(FROM|INTO|UPDATE|DELETE FROM)\s+(" + "|".join(HEAVY_TABLES) + r")\b", re.IGNORECASE
    )
    offenders: list[str] = []
    for path in (WORKER / "src").glob("*.ts"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            offenders.append(f"{path.name}: {match.group(0)}")
    assert offenders == [], f"Worker 源码仍读写已删除大表：{offenders}"


@requires_node
def test_schema_sql_and_migrations_in_sync():
    proc = _run_node("scripts/check-schema.mjs")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "schema OK" in proc.stdout


# ---------------------------------------------------------------------------
# 路由 / 缓存 / Cron 接线
# ---------------------------------------------------------------------------


def test_worker_routes_wired():
    source = (WORKER / "src" / "index.ts").read_text(encoding="utf-8")
    expected_routes = [
        "/api/health",
        "/api/config",
        "/api/verify",
        "/api/market-overview",
        "/api/market",
        "/api/kline",
        "/api/intraday",
        "/api/boards",
        "/api/signals",
        "/api/stream",
        "/api/search",
        "/api/paper",
        "/api/account",
        "/api/positions",
        "/api/orders",
        "/api/signal-log",
        "/api/ai/insight",
        "/api/ai/generate",
        "/api/daily-sync",
    ]
    for route in expected_routes:
        assert f'"{route}"' in source, f"index.ts 缺少路由 {route}"
    # 轻量表 ingest
    for route in ("/api/signal-log/ingest", "/api/account/ingest", "/api/positions/ingest", "/api/orders/ingest"):
        assert route in source
    # 旧接口兼容（不 500）
    for deprecated in ("/api/kline/ingest", "/api/market/ingest", "/api/regime/ingest", "/api/events/ingest", "/api/backtest/ingest"):
        assert deprecated in source, f"旧接口 {deprecated} 需要兼容响应避免 500"


def test_edge_cache_wiring():
    source = (WORKER / "src" / "index.ts").read_text(encoding="utf-8")
    cache_src = (WORKER / "src" / "cache.ts").read_text(encoding="utf-8")
    assert "cachedRoute(" in source and "withEdgeCache" in source
    assert "cacheDecorate" in source
    assert "x-cache" in cache_src.lower()
    # workers.dev 上 Cache API no-op -> D1 edge_cache 二级缓存
    assert "edge_cache" in cache_src
    assert "PassthroughError" in cache_src and "PassthroughError" in source
    # 鉴权必须在缓存之前（防止 401 被缓存/绕过）
    block = source[source.index("async function cachedRoute") : source.index("function cacheDecorate")]
    assert block.index("requireChallenge") < block.index("withEdgeCache"), "cachedRoute 必须先鉴权再读缓存"


def test_cron_and_scheduler_wired():
    cfg = tomllib.loads((WORKER / "wrangler.local.toml").read_text(encoding="utf-8")) if (WORKER / "wrangler.local.toml").exists() else {}
    tracked = tomllib.loads((WORKER / "wrangler.toml").read_text(encoding="utf-8"))
    triggers = cfg.get("triggers", tracked.get("triggers", {}))
    crons = triggers.get("crons", [])
    if cfg:
        for cron in ["*/5 1-7 * * 1-5", "35 3 * * 1-5", "5 7 * * 1-5", "40 7 * * 1-5"]:
            assert cron in crons, f"部署配置缺少 cron {cron}"
    source = (WORKER / "src" / "index.ts").read_text(encoding="utf-8")
    assert "async scheduled(" in source and "handleScheduledCron" in source


def test_wrangler_config_d1_assets_vars():
    cfg = tomllib.loads((WORKER / "wrangler.toml").read_text(encoding="utf-8"))
    assert cfg["main"] == "src/index.ts"
    d1 = cfg["d1_databases"][0]
    assert d1["binding"] == "DB" and d1["migrations_dir"] == "migrations"
    assert cfg["assets"]["directory"] == "../pages"
    local = (WORKER / "wrangler.local.toml")
    assert local.exists(), "部署配置 wrangler.local.toml 缺失"
    local_cfg = tomllib.loads(local.read_text(encoding="utf-8"))
    assert local_cfg["d1_databases"][0]["database_id"] != "00000000-0000-0000-0000-000000000000"
    assert local_cfg.get("vars", {}).get("WORKER_PUBLIC_URL", "").startswith("https://")


# ---------------------------------------------------------------------------
# 模型 / 打包 / 安全默认值
# ---------------------------------------------------------------------------


def test_model_json_contract_section_5():
    model = json.loads((WORKER / "models" / "model.json").read_text(encoding="utf-8"))
    assert model["schema"] == 1 and model["kind"] == "softmax_regression"
    assert model["classes"] == ["down", "flat", "up"]
    n_feat, n_cls = len(model["features"]), len(model["classes"])
    assert len(model["mean"]) == n_feat and len(model["scale"]) == n_feat
    assert len(model["coef"]) == n_cls and all(len(row) == n_feat for row in model["coef"])
    assert len(model["intercept"]) == n_cls


@requires_node
def test_parity_ts_vs_python_under_1e6():
    proc = _run_node("test/parity.mjs", timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PARITY OK" in proc.stdout


@requires_esbuild
@requires_node
def test_worker_bundles_as_esm():
    proc = _run_node("scripts/check-bundle.mjs", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "BUNDLE OK" in proc.stdout


def test_no_hardcoded_secrets():
    assert ".dev.vars" in (WORKER / ".gitignore").read_text(encoding="utf-8")
    dev_vars = WORKER / ".dev.vars"
    local_token = None
    if dev_vars.exists():
        match = re.search(r"QUANT_INGEST_TOKEN\s*=\s*(\S+)", dev_vars.read_text(encoding="utf-8"))
        local_token = match.group(1) if match else None
    for path in (WORKER / "src").glob("*.ts"):
        text = path.read_text(encoding="utf-8")
        assert "CLOUDFLARE_API_TOKEN=" not in text
        if local_token:
            assert local_token not in text, f"{path.name} 泄漏了本地 .dev.vars token"
    assert "local-dev-token" not in (WORKER / "src" / "index.ts").read_text(encoding="utf-8")
