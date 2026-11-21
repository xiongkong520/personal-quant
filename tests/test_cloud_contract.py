"""Cloudflare 侧契约测试（task-4 / CONTRACT §3.3 §4 §5 §9 §10）。

覆盖：
- D1 schema.sql：signals 表字段与契约 §3.3 一致、索引、CHECK 约束、seed.sql 幂等；
- schema.sql / migrations / src/schema.ts 三处一致；
- Worker 打包的 model.json 符合 §5；test/parity.mjs 的 Python vs TS 推理误差 < 1e-6；
- Worker 可被 esbuild 以 Workers 方式打包；wrangler 配置与 `wrangler deploy --dry-run` 通过；
- 安全层：UA/Accept 反爬 403、同源 CORS、安全响应头、按 IP 限流 429 + Retry-After、
  Turnstile /api/config + /api/verify -> HMAC signed quant_pass cookie、未验证 401 + challenge；
- 不硬编码 token、.dev.vars 已 gitignore；
- 端到端：用 scripts/dev-server.mjs（同一份 src/index.ts + node:sqlite D1）验证
  /api/health、/api/config、/api/verify、/api/signals、POST /api/ingest（正确/错误 token）、
  SSE、静态资源、错误码、限流。

全部离线、不需要 Cloudflare 账号；不需要 workerd（Android proot 等环境也能跑）。
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare" / "worker"
NODE = shutil.which("node")

E2E_TOKEN = "test-ingest-token-contract"
# 契约 §3.3 signals 表字段（顺序与 schema.sql 一致）
CONTRACT_COLUMNS = [
    "signal_id",
    "symbol",
    "asof",
    "trade_date",
    "price",
    "direction",
    "prob_up",
    "prob_flat",
    "prob_down",
    "confidence",
    "horizon",
    "expires_at",
    "model_version",
    "top_features",
    "source",
    "status",
    "created_at",
]

requires_node = pytest.mark.skipif(NODE is None, reason="需要 node 24（node test/parity.mjs / dev-server）")
requires_esbuild = pytest.mark.skipif(not (WORKER / "node_modules" / "esbuild").exists(), reason="未 npm install（esbuild）")
requires_wrangler = pytest.mark.skipif(
    not (WORKER / "node_modules" / ".bin" / "wrangler").exists(), reason="未 npm install（wrangler）"
)

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _run_node(*args: str, timeout: int = 240, env: dict | None = None) -> subprocess.CompletedProcess:
    assert NODE is not None
    return subprocess.run(
        [NODE, *args],
        cwd=str(WORKER),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _valid_row(**overrides: object) -> dict:
    """一条满足 schema 约束的合法 signals 行。"""
    row = {
        "signal_id": "600519-2026-11-21-20261121143500",
        "symbol": "600519",
        "asof": "2026-11-21T14:35:00+08:00",
        "trade_date": "2026-11-21",
        "price": 1680.5,
        "direction": "up",
        "prob_up": 0.42,
        "prob_flat": 0.31,
        "prob_down": 0.27,
        "confidence": 0.42,
        "horizon": "1d",
        "expires_at": "2026-11-24T09:30:00+08:00",
        "model_version": "v0.1.0",
        "top_features": "[]",
        "source": "local",
        "status": "ok",
        "created_at": "2026-11-21T14:35:06+08:00",
    }
    row.update(overrides)
    return row


def _insert(conn: sqlite3.Connection, row: dict) -> None:
    cols = ", ".join(row)
    ph = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO signals ({cols}) VALUES ({ph})", tuple(row.values()))


# ---------------------------------------------------------------------------
# D1 schema / seed
# ---------------------------------------------------------------------------


def test_schema_sql_matches_contract_section_3_3():
    sql = (WORKER / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql)

    info = {r[1]: r for r in conn.execute("PRAGMA table_info(signals)")}
    assert list(info) == CONTRACT_COLUMNS, "signals 字段/顺序必须与契约 §3.3 一致（少 paper_* 字段）"
    assert info["signal_id"][5] == 1, "signal_id 必须是主键"
    for not_null in ("symbol", "asof", "trade_date", "price", "direction", "confidence", "model_version", "status"):
        assert info[not_null][3] == 1, f"{not_null} 必须 NOT NULL"

    indexes = {r[1] for r in conn.execute("PRAGMA index_list(signals)")}
    assert {"idx_signals_symbol_asof", "idx_signals_created_at", "idx_signals_trade_date"} <= indexes

    # CHECK 约束：direction / source / status 枚举
    for bad in ({"direction": "sideways"}, {"source": "remote"}, {"status": "fresh"}):
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, _valid_row(**bad))

    # 幂等写入：同 signal_id 只能有一条（UPSERT 语义由 Worker 保证，表结构靠主键）
    _insert(conn, _valid_row())
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, _valid_row())


def test_seed_sql_is_idempotent_and_contract_shaped():
    sql = (WORKER / "schema.sql").read_text(encoding="utf-8")
    seed = (WORKER / "seed.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql)
    conn.executescript(seed)
    first = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert first >= 3, "seed.sql 至少给 2-3 条演示信号"
    conn.executescript(seed)  # 重复执行不报错、不翻倍（INSERT OR REPLACE）
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == first

    rows = conn.execute("SELECT symbol, direction, prob_up, prob_flat, prob_down, status, top_features FROM signals").fetchall()
    for symbol, direction, p_up, p_flat, p_down, status, top_features in rows:
        assert re.fullmatch(r"\d{6}", symbol)
        assert direction in {"up", "flat", "down"}
        assert status in {"ok", "stale", "warming_up"}
        assert abs((p_up + p_flat + p_down) - 1) <= 1e-6
        probs = {"up": p_up, "flat": p_flat, "down": p_down}
        assert max(probs, key=probs.get) == direction, "direction 必须等于 prob 的 argmax"
        features = json.loads(top_features)
        assert isinstance(features, list) and len(features) <= 5
        contribs = [abs(f["contrib"]) for f in features]
        assert contribs == sorted(contribs, reverse=True), "top_features 必须按 |contrib| 降序"


@requires_node
def test_schema_sql_and_ts_schema_in_sync():
    proc = _run_node("scripts/check-schema.mjs")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "schema OK" in proc.stdout


# ---------------------------------------------------------------------------
# §5 模型 / §10 parity
# ---------------------------------------------------------------------------


def test_worker_model_json_matches_contract_section_5():
    model = json.loads((WORKER / "models" / "model.json").read_text(encoding="utf-8"))
    assert model["schema"] == 1
    assert model["kind"] == "softmax_regression"
    assert sorted(model["classes"]) == ["down", "flat", "up"]
    assert model["classes"] == ["down", "flat", "up"], "§5 约定 class 顺序为 down/flat/up"
    n_feat, n_cls = len(model["features"]), len(model["classes"])
    assert n_feat > 0
    assert len(model["mean"]) == n_feat and len(model["scale"]) == n_feat
    assert all(isinstance(s, (int, float)) and s not in (0, 0.0) for s in model["scale"]), "scale 不能为 0"
    assert len(model["coef"]) == n_cls and all(len(row) == n_feat for row in model["coef"])
    assert len(model["intercept"]) == n_cls
    for key in ("version", "trained_at", "train_range", "horizon", "label_rule", "metrics"):
        assert key in model, f"model.json 缺 §5 字段 {key}"


@requires_node
def test_parity_ts_vs_python_under_1e6():
    proc = _run_node("test/parity.mjs", timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PARITY OK" in proc.stdout
    assert "model-dev fixture" in proc.stdout or "trained model" in proc.stdout or "self fixture" in proc.stdout
    # 机器可读性：parity 输出里报告 max|Δ|
    assert re.search(r"max\|Δ\|=\d", proc.stdout)


@requires_esbuild
@requires_node
def test_worker_bundles_as_esm():
    proc = _run_node("scripts/check-bundle.mjs", timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "BUNDLE OK" in proc.stdout


# ---------------------------------------------------------------------------
# wrangler 配置 / 端点声明 / 安全
# ---------------------------------------------------------------------------


def test_wrangler_config_has_d1_and_assets():
    cfg = tomllib.loads((WORKER / "wrangler.toml").read_text(encoding="utf-8"))
    assert cfg["main"] == "src/index.ts"
    assert "compatibility_date" in cfg
    d1 = cfg["d1_databases"][0]
    assert d1["binding"] == "DB"
    assert d1["database_name"] and d1["migrations_dir"]
    assert cfg["assets"]["directory"] == "../pages", "Worker 通过 Assets 挂载 cloudflare/pages 产物"
    assert (WORKER / "migrations").is_dir() and list((WORKER / "migrations").glob("*.sql"))
    assert "QUANT_INGEST_TOKEN" not in json.dumps(cfg), "wrangler.toml 不允许出现 token"


@requires_wrangler
@requires_node
def test_wrangler_deploy_dry_run_builds(tmp_path: Path):
    """`wrangler deploy --dry-run` 验证 wrangler.toml + D1 binding + Assets 配置可构建（不部署、不需要账号）。"""
    wrangler = WORKER / "node_modules" / ".bin" / "wrangler"
    proc = subprocess.run(
        [str(wrangler), "deploy", "--dry-run", "--outdir", str(tmp_path / "dist")],
        cwd=str(WORKER),
        capture_output=True,
        text=True,
        timeout=240,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-3000:]
    assert "D1 Database" in out, out[-3000:]
    assert "Assets" in out, out[-3000:]
    assert "--dry-run: exiting now" in out


def test_worker_source_declares_contract_endpoints():
    source = (WORKER / "src" / "index.ts").read_text(encoding="utf-8")
    for endpoint in ("/api/health", "/api/signals", "/api/ingest", "/api/stream", "/api/config", "/api/verify"):
        assert endpoint in source, f"Worker 未实现端点 {endpoint}"
    assert "text/event-stream" in source
    assert "QUANT_INGEST_TOKEN" in source, "token 必须从环境变量读取"
    # SSE 心跳 30s（task-4 更新后的要求）
    assert "SSE_HEARTBEAT_MS = 30_000" in source
    # 安全层接线
    for needle in ("securityGate", "checkUserAgent", "checkAcceptHeader", "checkSameOrigin", "checkRateLimit", "requireChallenge", "verifyTurnstileToken", "applySecurityHeaders"):
        assert needle in source, f"index.ts 未接安全层 {needle}"


def test_security_module_covers_required_controls():
    security = (WORKER / "src" / "security.ts").read_text(encoding="utf-8")
    # 反爬 UA 黑名单（需求点名的 UA 都要覆盖）
    for needle in ("curl", "wget", "python-requests", "scrapy", "httpie", "node-fetch", "go-http-client", "headlesschrome"):
        assert needle.lower() in security.lower(), f"UA 黑名单缺 {needle}"
    # 安全响应头
    for header in ("content-security-policy", "x-frame-options", "referrer-policy", "x-content-type-options", "permissions-policy"):
        assert header in security, f"安全响应头缺 {header}"
    # 限流：默认 60/分钟 + Retry-After 语义 + KV/内存两种实现
    assert "DEFAULT_RATE_LIMIT_PER_MIN = 60" in security
    assert "RATE_LIMIT_KV" in security and "checkMemoryRateLimit" in security
    # Turnstile + HMAC cookie
    assert "challenges.cloudflare.com/turnstile/v0/siteverify" in security
    assert "1x00000000000000000000AA" in security and "1x0000000000000000000000000000000AA" in security
    assert "crypto.subtle" in security and "quant_pass" in security
    assert 'readCookieKey' in security and "COOKIE_KEY" in security


def test_no_hardcoded_secrets():
    # .dev.vars 必须被 gitignore
    assert ".dev.vars" in (WORKER / ".gitignore").read_text(encoding="utf-8")

    # 本地 .dev.vars 里的真实 token 不能出现在仓库其他文件里
    dev_vars = WORKER / ".dev.vars"
    local_token = None
    if dev_vars.exists():
        m = re.search(r"QUANT_INGEST_TOKEN\s*=\s*(\S+)", dev_vars.read_text(encoding="utf-8"))
        local_token = m.group(1) if m else None

    scan_roots = [WORKER, ROOT / "tests"]
    skip_dirs = {"node_modules", ".wrangler", "dist", "__pycache__", ".git"}
    scanned = 0
    for root in scan_roots:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in skip_dirs for part in path.parts):
                continue
            if path.name == ".dev.vars" or path.name.startswith(".dev.vars."):
                continue
            if path.suffix in {".pyc", ".sqlite", ".db", ".sqlite-wal", ".sqlite-shm", ".png", ".jpg", ".woff2"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            scanned += 1
            if local_token:
                assert local_token not in text, f"{path} 泄漏了本地 .dev.vars token"
            if path.suffix in {".ts", ".mjs", ".js", ".toml", ".json", ".sh", ".yaml", ".yml"}:
                assert "CLOUDFLARE_API_TOKEN=" not in text, f"{path} 出现硬编码 Cloudflare API token"
                assert not re.search(r"Authorization:\s*Bearer\s+[A-Za-z0-9._-]{20,}", text), f"{path} 出现硬编码 Bearer token"
    assert scanned > 5

    # Worker 运行时代码不允许内置 token 兜底（只有本地 dev-server 可以有 local-dev-token）
    for name in ("src/index.ts", "src/signals.ts", "src/db.ts", "src/types.ts"):
        text = (WORKER / name).read_text(encoding="utf-8")
        assert "local-dev-token" not in text


def test_readme_documents_local_and_deploy_paths():
    readme = (WORKER / "README.md").read_text(encoding="utf-8")
    for needle in (
        "npx wrangler dev --local --port 8788",
        "QUANT_INGEST_TOKEN",
        "test/parity.mjs",
        "db:seed:local",
        "wrangler login",
        "CLOUDFLARE_API_TOKEN",
        "dev:node",
        "security-test.sh",
        "TURNSTILE_SECRET",
        "TURNSTILE_SITEKEY",
        "COOKIE_KEY",
        "d1 create",
        "migrations apply",
        "wrangler deploy",
        "Turnstile",
    ):
        assert needle in readme, f"README 缺少 {needle}"
    assert (WORKER / ".dev.vars.example").exists()
    dev_vars_example = (WORKER / ".dev.vars.example").read_text(encoding="utf-8")
    for key in ("QUANT_INGEST_TOKEN", "COOKIE_KEY", "TURNSTILE_SITEKEY", "TURNSTILE_SECRET"):
        assert key in dev_vars_example, f".dev.vars.example 缺 {key}"


# ---------------------------------------------------------------------------
# 端到端（node 回退运行时，与云端执行同一份 src/index.ts）
# ---------------------------------------------------------------------------

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
E2E_TOKEN = "test-ingest-token-contract"
E2E_COOKIE_KEY = "test-cookie-key-contract"
TURNSTILE_TEST_SITEKEY = "1x00000000000000000000AA"
TURNSTILE_TEST_TOKEN = "XXXX.DUMMY.TOKEN.XXXX"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: object | str | None = None,
    timeout: float = 15,
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
    user_agent: str | None = BROWSER_UA,
    accept: str | None = "application/json",
) -> tuple[int, object]:
    """带浏览器 UA/Accept 的 JSON 请求（Worker 有反爬），返回 (status, 解析后的 body)。"""
    raw: bytes | None = None
    if body is not None:
        raw = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method=method)
    if raw is not None:
        req.add_header("content-type", "application/json")
    if user_agent is not None:
        req.add_header("user-agent", user_agent)
    if accept is not None:
        req.add_header("accept", accept)
    if cookie:
        req.add_header("cookie", cookie)
    if token:
        req.add_header("authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            try:
                return resp.status, json.loads(payload)
            except json.JSONDecodeError:
                return resp.status, payload.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, payload.decode("utf-8", "replace")


def _http_headers(url: str, *, cookie: str | None = None, accept: str = "application/json") -> tuple[int, dict[str, str]]:
    req = urllib.request.Request(url, method="GET")
    req.add_header("user-agent", BROWSER_UA)
    req.add_header("accept", accept)
    if cookie:
        req.add_header("cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}


def _raw_http(base: str, method: str, path: str, headers: dict[str, str]) -> tuple[int, dict[str, str]]:
    """完全手写请求头（用于测试“无 UA / 无 Accept”这类 urllib 默认会补头的场景）。"""
    parsed = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    try:
        conn.putrequest(method, path)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}
    finally:
        conn.close()


def _post_verify(base: str, token: object | None) -> tuple[int, object, str]:
    body = {} if token is None else {"token": token}
    req = urllib.request.Request(
        f"{base}/api/verify",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"content-type": "application/json", "accept": "application/json", "user-agent": BROWSER_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
            return resp.status, payload, resp.headers.get("set-cookie") or ""
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", "replace")
        return exc.code, payload, exc.headers.get("set-cookie") or ""


@contextlib.contextmanager
def _running_server(
    db_path: Path,
    *,
    extra_args: tuple[str, ...] = (),
    env_extra: dict | None = None,
):
    if NODE is None:
        pytest.skip("node 未安装，跳过 Cloudflare 端到端测试")
    port = _free_port()
    env = {
        **os.environ,
        "QUANT_INGEST_TOKEN": E2E_TOKEN,
        "COOKIE_KEY": E2E_COOKIE_KEY,
        "RATE_LIMIT_PER_MIN": "100000",  # 功能测试不受限流干扰；限流单独起低配额服务测试
        **(env_extra or {}),
    }
    proc = subprocess.Popen(
        [NODE, "scripts/dev-server.mjs", "--port", str(port), "--db", str(db_path), *extra_args],
        cwd=str(WORKER),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 40
        last_error: Exception | None = None
        while time.time() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"dev-server 提前退出（exit={proc.returncode}）：\n{output[-3000:]}")
            try:
                status, payload = _http("GET", f"{base}/api/health", timeout=2)
                if status == 200 and isinstance(payload, dict) and payload.get("ok") is True:
                    break
            except Exception as exc:  # noqa: BLE001 - 启动期连接失败是预期情况
                last_error = exc
                time.sleep(0.2)
        else:
            pytest.fail(f"dev-server 40s 内未就绪：{last_error}")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> dict:
    db_path = tmp_path_factory.mktemp("cloud_contract_d1") / "signals.sqlite"
    with _running_server(db_path, extra_args=("--no-seed",)) as base:
        # 人机验证：拿 quant_pass cookie（dev-server 内置 Turnstile mock，任意 token 通过）
        status, payload, set_cookie = _post_verify(base, TURNSTILE_TEST_TOKEN)
        assert status == 200 and payload.get("ok") is True, payload
        cookie = set_cookie.split(";")[0]
        assert cookie.startswith("quant_pass=")
        # 空库先灌一条确定信号，避免测试依赖 seed.sql 内容
        status, payload = _http(
            "POST",
            f"{base}/api/ingest",
            token=E2E_TOKEN,
            body={
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
                "model_version": "v0.1.0-e2e",
                "status": "ok",
                "top_features": [
                    {"name": "ma5_bias", "value": 0.012, "contrib": -0.02},
                    {"name": "ret_5d", "value": 0.031, "contrib": 0.08},
                ],
                "source": "local",
            },
        )
        assert status == 200 and payload["accepted"] == 1, payload
        yield {"base": base, "cookie": cookie, "set_cookie": set_cookie, "ingest_token": E2E_TOKEN}


def _items(server: dict, query: str = "") -> list[dict]:
    status, payload = _http("GET", f"{server['base']}/api/signals{query}", cookie=server["cookie"])
    assert status == 200, payload
    assert isinstance(payload, dict)
    assert payload["schema"] == 1
    assert isinstance(payload["items"], list)
    return payload["items"]


def test_e2e_health_contract(server: dict):
    status, health = _http("GET", f"{server['base']}/api/health")
    assert status == 200
    assert health["schema"] == 1 and health["ok"] is True
    assert health["d1"]["status"] == "ok"
    assert health["d1"]["signals"] >= 1
    assert health["ingest"]["token_configured"] is True
    assert health["model"]["kind"] == "softmax_regression"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00", health["now"])
    security = health["security"]
    assert security["ua_filter"] is True and security["accept_check"] is True and security["same_origin_cors"] is True
    assert security["rate_limit_per_min"] == 100000
    assert security["rate_limit_backend"] == "memory"
    assert set(security["challenge_required_for"]) == {"/api/signals", "/api/stream", "/api/model"}
    assert security["cookie"]["name"] == "quant_pass" and security["cookie"]["cookie_key_configured"] is True


def test_e2e_config_contract(server: dict):
    status, config = _http("GET", f"{server['base']}/api/config", cookie=server["cookie"])
    assert status == 200
    assert config["schema"] == 1 and config["ok"] is True
    assert config["challenge"] is True, "Worker 未验证时始终返回 challenge=true"
    assert config["sitekey"] == TURNSTILE_TEST_SITEKEY
    assert config["turnstile_sitekey"] == config["sitekey"] == config["turnstileSitekey"]
    assert config["verify_path"] == "/api/verify"
    assert config["cookie_name"] == "quant_pass"
    assert config["cookie_ttl_seconds"] == 3600
    assert config["turnstile"]["sitekey"] == TURNSTILE_TEST_SITEKEY
    assert config["rate_limit"]["requests_per_minute"] == 100000


def test_e2e_anti_crawl(server: dict):
    base = server["base"]
    # curl / wget / python-requests / 无头浏览器 / 抓取库 UA -> 403
    for bad_ua in (
        "curl/8.5.0",
        "Wget/1.21",
        "python-requests/2.31.0",
        "python-urllib/3.12",
        "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/131.0.0.0 Safari/537.36",
        "Scrapy/2.11 (+https://scrapy.org)",
        "Go-http-client/1.1",
        "node-fetch/1.0 (+https://github.com/bitinn/node-fetch)",
    ):
        status, payload = _http("GET", f"{base}/api/health", user_agent=bad_ua)
        assert status == 403, f"{bad_ua} 应被 403，实际 {status}"
        assert isinstance(payload, dict) and payload["error"] == "forbidden_client"

    # 无 UA / 无 Accept -> 403（用裸 http.client，避免 urllib 补默认头）
    status, _ = _raw_http(base, "GET", "/api/health", {"accept": "application/json"})
    assert status == 403, "无 UA 应 403"
    status, _ = _raw_http(base, "GET", "/api/health", {"user-agent": BROWSER_UA})
    assert status == 403, "无 Accept 应 403"
    # 正常浏览器请求 -> 200（公开端点 health）
    status, _ = _raw_http(base, "GET", "/api/health", {"user-agent": BROWSER_UA, "accept": "application/json"})
    assert status == 200


def test_e2e_challenge_required(server: dict):
    base = server["base"]
    for path in ("/api/signals", "/api/stream", "/api/model"):
        status, payload = _http("GET", f"{base}{path}")
        assert status == 401, f"{path} 未验证应 401，实际 {status}"
        assert isinstance(payload, dict)
        assert payload.get("challenge") is True and payload["error"] == "challenge_required"

    # 错误 / 伪造 cookie -> 401
    status, payload = _http("GET", f"{base}/api/signals", cookie="quant_pass=9999999999.zzzz")
    assert status == 401 and payload.get("challenge") is True


def test_e2e_verify_sets_httponly_cookie(server: dict):
    assert server["cookie"].startswith("quant_pass=")
    assert "HttpOnly" in server["set_cookie"]
    assert "Secure" in server["set_cookie"]
    assert "SameSite=Lax" in server["set_cookie"]
    assert "Max-Age=3600" in server["set_cookie"]
    assert "Path=/" in server["set_cookie"]

    status, payload, _ = _post_verify(server["base"], TURNSTILE_TEST_TOKEN)
    assert status == 200
    assert payload["schema"] == 1 and payload["ok"] is True
    assert payload["expires_in"] == 3600
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00", payload["expires_at"])


def test_e2e_verify_failure_and_missing_token(server: dict):
    base = server["base"]
    # dev-server 内置 siteverify mock：token=fail 模拟 Turnstile 校验失败
    status, payload, set_cookie = _post_verify(base, "fail")
    assert status == 403, payload
    assert payload["error"] == "challenge_failed"
    assert not set_cookie
    # 缺 token -> 400
    status, payload, _ = _post_verify(base, None)
    assert status == 400 and payload["error"] == "invalid_body"
    # 非 POST -> 405
    status, payload = _http("GET", f"{base}/api/verify")
    assert status == 405 and payload["error"] == "method_not_allowed"


def test_e2e_same_origin_cors(server: dict):
    base = server["base"]
    origin = base
    # 跨域 Origin -> 403（同源 CORS 限制）
    status, payload = _http("GET", f"{base}/api/health", headers={"origin": "https://evil.example"})
    assert status == 403 and payload["error"] == "cross_origin_forbidden"
    # 同源 Origin -> 200
    status, _ = _http("GET", f"{base}/api/health", headers={"origin": origin})
    assert status == 200
    # 同源预检 -> 204 + 回显 ACAO
    parsed = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    try:
        conn.request(
            "OPTIONS",
            "/api/verify",
            headers={
                "user-agent": BROWSER_UA,
                "accept": "*/*",
                "origin": origin,
                "access-control-request-method": "POST",
            },
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 204
        assert resp.getheader("access-control-allow-origin") == origin
    finally:
        conn.close()
    # 跨域预检 -> 403
    status, payload = _http(
        "OPTIONS",
        f"{base}/api/verify",
        headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
        accept="*/*",
    )
    assert status == 403 and payload["error"] == "cross_origin_forbidden"


def test_e2e_security_headers(server: dict):
    status, headers = _http_headers(f"{server['base']}/api/health", cookie=server["cookie"])
    assert status == 200
    assert "default-src" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["x-content-type-options"] == "nosniff"
    assert "camera=()" in headers["permissions-policy"]


def test_e2e_signals_contract_fields(server: dict):
    items = _items(server)
    assert items, "至少应有一条信号"
    for item in items:
        assert re.fullmatch(r"\d{6}", item["symbol"])
        assert item["direction"] in {"up", "flat", "down"}
        assert item["status"] in {"ok", "stale", "warming_up"}
        assert item["source"] in {"local", "cloud"}
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00", item["asof"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["trade_date"])
        prob = item["prob"]
        assert abs(prob["up"] + prob["flat"] + prob["down"] - 1) <= 1e-6
        assert abs(item["confidence"] - max(prob.values())) <= 5e-5, "confidence 应 = max(prob) 保留 4 位"
        assert len(item["top_features"]) <= 5
        contribs = [abs(f["contrib"]) for f in item["top_features"]]
        assert contribs == sorted(contribs, reverse=True)
        assert "name" not in item, "§3.3 无 name 列：name 不落库（前端用 symbol 映射）"
        assert "paper" not in item or item["paper"] is None
    status, payload = _http("GET", f"{server['base']}/api/signals", cookie=server["cookie"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00", payload["updated_at"])


def test_e2e_signals_filters(server: dict):
    assert {item["symbol"] for item in _items(server, "?symbols=600519")} == {"600519"}
    assert _items(server, "?symbols=600519,000001") == [
        item for item in _items(server) if item["symbol"] in {"600519", "000001"}
    ]
    assert len(_items(server, "?limit=1")) <= 1
    latest = _items(server, "?latest=1")
    assert len({item["symbol"] for item in latest}) == len(latest)

    cookie = server["cookie"]
    status, payload = _http("GET", f"{server['base']}/api/signals?symbols=abc", cookie=cookie)
    assert status == 400 and payload["error"] == "invalid_symbols"
    status, payload = _http("GET", f"{server['base']}/api/signals?status=fresh", cookie=cookie)
    assert status == 400 and payload["error"] == "invalid_status"
    status, payload = _http("POST", f"{server['base']}/api/signals", cookie=cookie)
    assert status == 405 and payload["error"] == "method_not_allowed"


def test_e2e_ingest_auth(server: dict):
    base = server["base"]
    status, payload = _http("POST", f"{base}/api/ingest", token="wrong-token", body={"symbol": "600519"})
    assert status == 401 and payload["error"] == "unauthorized"
    status, payload = _http("POST", f"{base}/api/ingest", body={"symbol": "600519"})
    assert status == 401 and payload["error"] == "unauthorized"
    status, payload = _http("POST", f"{base}/api/ingest", token=E2E_TOKEN, body="{not-json")
    assert status == 400 and payload["error"] == "invalid_json"
    # 机器接口：Python httpx 直接推送不受 UA/Accept 反爬限制（export_cloud.py 依赖这一点）
    status, payload = _http(
        "POST",
        f"{base}/api/ingest",
        token="wrong-token",
        body={"symbol": "600519"},
        user_agent="python-httpx/0.27.0",
        accept=None,
    )
    assert status == 401 and payload["error"] == "unauthorized", "ingest 应只认 token，不被 UA 反爬挡成 403"


def test_e2e_ingest_valid_and_idempotent(server: dict):
    base = server["base"]
    signal = {
        "schema": 1,
        "symbol": "601398",
        "asof": "2026-11-21T06:35:00Z",  # 非 +08:00 也应被归一化为 +08:00
        "trade_date": "2026-11-21",
        "price": 6.12,
        "direction": "flat",
        "prob": {"up": 0.30, "flat": 0.41, "down": 0.29},
        "confidence": 0.41,
        "horizon": "1d",
        "expires_at": "2026-11-24T09:30:00+08:00",
        "model_version": "v0.1.0-e2e",
        "status": "ok",
        "top_features": [{"name": "ret_1d", "value": 0.002, "contrib": 0.003}],
    }
    before = _http("GET", f"{base}/api/health")[1]["d1"]["signals"]
    status, payload = _http("POST", f"{base}/api/ingest", token=E2E_TOKEN, body=signal, user_agent="python-httpx/0.27.0")
    assert status == 200 and payload["accepted"] == 1, payload
    after_first = _http("GET", f"{base}/api/health")[1]["d1"]["signals"]
    assert after_first == before + 1

    status, payload = _http("POST", f"{base}/api/ingest", token=E2E_TOKEN, body=signal, user_agent="python-httpx/0.27.0")
    assert status == 200 and payload["accepted"] == 1
    after_second = _http("GET", f"{base}/api/health")[1]["d1"]["signals"]
    assert after_second == after_first, "同一 signal_id 重复推送必须幂等（UPSERT）"

    stored = [item for item in _items(server, "?symbols=601398") if item["symbol"] == "601398"]
    assert stored, "推送后应能查到 601398 信号"
    item = stored[0]
    assert item["asof"] == "2026-11-21T14:35:00+08:00", "asof 必须归一化为 +08:00"
    assert item["source"] == "local", "source 缺省为 local"
    assert item["signal_id"].startswith("601398-2026-11-21-")


def test_e2e_ingest_batch_partial_reject(server: dict):
    good = {
        "symbol": "000858",
        "asof": "2026-11-21T14:40:00+08:00",
        "trade_date": "2026-11-21",
        "price": 138.2,
        "direction": "down",
        "prob": {"up": 0.2, "flat": 0.3, "down": 0.5},
        "confidence": 0.5,
        "horizon": "1d",
        "expires_at": "2026-11-24T09:30:00+08:00",
        "model_version": "v0.1.0-e2e",
        "status": "stale",
    }
    bad = {**good, "symbol": "000000", "prob": {"up": 0.5, "flat": 0.5, "down": 0.5}}
    status, payload = _http("POST", f"{server['base']}/api/ingest", token=E2E_TOKEN, body={"schema": 1, "items": [good, bad]})
    assert status == 200
    assert payload["accepted"] == 1 and len(payload["rejected"]) == 1
    assert any("prob 和" in err for err in payload["rejected"][0]["errors"])


def test_e2e_stream_sse(server: dict):
    req = urllib.request.Request(f"{server['base']}/api/stream")
    req.add_header("user-agent", BROWSER_UA)
    req.add_header("accept", "text/event-stream")
    req.add_header("cookie", server["cookie"])
    events: dict[str, list[str]] = {}
    current = None
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        deadline = time.time() + 15
        while time.time() < deadline and not (events.get("hello") and events.get("signals")):
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8").strip()
            if text.startswith("event: "):
                current = text[len("event: ") :]
                events.setdefault(current, [])
            elif text.startswith("data: ") and current:
                events[current].append(text[len("data: ") :])
    assert "hello" in events, "SSE 缺少 hello 事件"
    assert "signals" in events, "SSE 缺少 signals 事件（应有最新信号快照）"
    hello = json.loads(events["hello"][0])
    assert hello["schema"] == 1 and hello["heartbeat_sec"] == 30
    snapshot = json.loads(events["signals"][0])
    assert snapshot["schema"] == 1 and isinstance(snapshot["items"], list)


def test_e2e_model_endpoint(server: dict):
    status, payload = _http("GET", f"{server['base']}/api/model", cookie=server["cookie"])
    assert status == 200 and payload["ok"] is True
    assert payload["model"]["kind"] == "softmax_regression"
    probe = payload["parity_probe"]["zero_features"]
    assert probe is not None
    assert abs(sum(probe["prob"].values()) - 1) <= 1e-6
    assert probe["direction"] in {"up", "flat", "down"}


def test_e2e_static_and_errors(server: dict):
    base = server["base"]
    status, payload = _http("GET", f"{base}/", accept="text/html")
    assert status == 200
    if isinstance(payload, dict):
        # pages/ 目录暂无产物时返回提示 JSON（frontend-dev 产出 index.html 后则是 HTML）
        assert payload.get("schema") == 1 and "api" in payload
    else:
        assert "<html" in payload.lower() or "<!doctype" in payload.lower()

    status, payload = _http("GET", f"{base}/api/unknown", cookie=server["cookie"])
    assert status == 404 and payload["error"] == "not_found"

    # 兜底 404 的静态资源也应有安全响应头
    status, headers = _http_headers(f"{base}/definitely-not-exists.html", cookie=server["cookie"], accept="text/html")
    assert status == 404 and headers.get("x-frame-options") == "DENY"


def test_e2e_rate_limit(tmp_path: Path):
    """低配额服务：第 6 次请求开始 429 + Retry-After（限流按 cf-connecting-ip / 本地 fallback）。"""
    with _running_server(
        tmp_path / "ratelimit.sqlite",
        extra_args=("--no-seed",),
        env_extra={"RATE_LIMIT_PER_MIN": "5", "COOKIE_KEY": E2E_COOKIE_KEY},
    ) as base:
        # 启动探活已经消耗 1 次；再打到第 5 次以内应正常，之后 429
        saw_429 = False
        status = None
        headers: dict[str, str] = {}
        for _ in range(10):
            status, headers = _http_headers(f"{base}/api/health")
            if status == 429:
                saw_429 = True
                break
        assert saw_429, f"限流未触发（最后状态 {status}）"
        assert headers.get("retry-after", "").isdigit(), headers
        assert headers.get("x-ratelimit-limit") == "5"
        status, payload = _http("GET", f"{base}/api/health")
        assert status == 429
        assert isinstance(payload, dict) and payload["error"] == "rate_limited"
        assert isinstance(payload.get("retry_after"), int) and payload["retry_after"] >= 1


def test_e2e_ingest_503_without_token(tmp_path: Path):
    with _running_server(tmp_path / "no_token.sqlite", extra_args=("--no-token",)) as base:
        status, health = _http("GET", f"{base}/api/health")
        assert status == 200 and health["ingest"]["token_configured"] is False
        status, payload = _http("POST", f"{base}/api/ingest", token="whatever", body={"symbol": "600519"})
        assert status == 503 and payload["error"] == "ingest_token_not_configured"
