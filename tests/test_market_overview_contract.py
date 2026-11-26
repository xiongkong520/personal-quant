"""S2 market-overview 契约 + 模块一边缘缓存离线测试。

不联网：全部用 fixture / 假 D1 / 模拟 workers.dev（无 Cache API）验证。

覆盖：
1. GET /api/market-overview 的 §3.1 字段契约：
   - 5 指数（code/name/price/pct_chg/amount）+ price<=0 清洗；
   - breadth（up/down/flat/limit_up/limit_down/broken_rate/total_amount/sentiment{score,state,label,breadth_score,heat_score}）；
   - sectors industry/concept（gainers/losers Top N、领涨股清洗、排序）；
   - session（trading/lunch/closed）、quality{sources,degraded,errors}；
   - 系统提示：概念板块无源时 [] + degraded。
2. 模块一缓存（cloudflare/worker/src/cache.ts）在 workers.dev 语义下的行为：
   Cache API 不可用 -> D1 edge_cache 兜底；MISS -> HIT -> 上游失败 STALE；
   非 200（challenge 401 等）通过 PassthroughError 直接上抛、不写缓存、不拿 lkg 冒充。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare" / "worker"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 24（TS type stripping）")


def _run_node(script: str, payload: dict | None = None, timeout: int = 120) -> dict:
    """Node 直跑 TS（Node 24 原生 type stripping），返回 stdout JSON。"""
    import os

    env = {**os.environ, "NODE_NO_WARNINGS": "1"}
    if payload is not None:
        env["PAYLOAD"] = json.dumps(payload, ensure_ascii=False)
    proc = subprocess.run(
        [NODE, "--no-warnings", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"node 失败：\nstdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-3000:]}"
    # 忽略可能的 warning 行，取最后一个 JSON 行
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip().startswith("{")]
    assert lines, f"node 无 JSON 输出：{proc.stdout[-2000:]}"
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# fixture 构造
# ---------------------------------------------------------------------------

_TENCENT_FIELDS = 45


def _tencent_quote(key: str, name: str, code: str, price: float, prev: float, pct: float, amount_wan: float, stamp: int = 20261125161409) -> str:
    fields = [""] * _TENCENT_FIELDS
    fields[0] = "1"
    fields[1] = name
    fields[2] = code
    fields[3] = f"{price:.2f}"
    fields[4] = f"{prev:.2f}"
    fields[5] = f"{prev:.2f}"
    fields[30] = str(stamp)
    fields[31] = f"{price - prev:+.2f}"
    fields[32] = f"{pct:+.2f}"
    fields[36] = "1000000"
    fields[37] = f"{amount_wan:.2f}"
    return f'v_{key}="' + "~".join(fields) + '";'


def _tencent_text(**overrides: float) -> str:
    """5 大指数 + 两市成交额指数的腾讯 GBK 文本（overrides 可按 key 覆盖 price）。"""
    base = {
        "sh000300": ("沪深300", "000300", 3898.24, 3963.82, -1.65, 41098520),
        "sh000688": ("科创50", "000688", 1186.14, 1250.09, -5.12, 8228262),
        "sz399006": ("创业板指", "399006", 2183.58, 2278.99, -4.19, 35487660),
        "sh000905": ("中证500", "000905", 6100.00, 6150.00, -0.81, 20000000),
        "sh000852": ("中证1000", "000852", 6400.00, 6433.00, -0.51, 18000000),
        "sh000001": ("上证指数", "000001", 3476.88, 3476.53, 0.01, 61333281),
        "sz399001": ("深证成指", "399001", 10084.04, 10086.06, -0.02, 70404436),
    }
    lines = []
    for key, (name, code, price, prev, pct, amount) in base.items():
        actual_price = overrides.get(key, price)
        lines.append(_tencent_quote(key, name, code, actual_price, prev, pct, amount))
    return "".join(lines)


_SINA_TEXT = (
    'var S_Finance_bankuai_sinaindustry = {'
    '"new_a":"new_a,陶瓷行业,20,10.5,0.2,2.21,1000000,2000000,sh002100,9.98,12.5,1.1,天康生物",'
    '"new_b":"new_b,船舶制造,10,8.0,-0.1,-1.35,900000,800000,sz300001,-2.0,8.0,-0.1,中船科技",'
    '"new_c":"new_c,银行,40,12.0,0.05,0.62,5000000,6000000,,,"'
    "};"
)

_ACTIVITY = {
    "up": 1838,
    "down": 3267,
    "flat": 114,
    "limit_up": 64,
    "limit_down": 4,
    "active_pct": 10.0,
    "asof": "2026-11-25 15:00:00",
}

_NOW = "2026-11-26T10:23:00+08:00"


def _base_input(**overrides) -> dict:
    payload = {
        "tencentText": _tencent_text(**(overrides.pop("tencent_overrides", {}))),
        "activity": _ACTIVITY,
        "limitUpPool": 64,
        "brokenPool": 14,
        "sinaIndustryText": _SINA_TEXT,
        "errors": [],
        "now": _NOW,
        "params": {"sectorType": "industry", "sort": "gainers", "limit": 3},
        "cache": {"hit": False, "age_sec": 0, "ttl": 60, "stale": False},
    }
    payload.update(overrides)
    return payload


_ASSEMBLE_SCRIPT = f"""
const mod = await import("file://{WORKER}/src/market_overview.ts");
const input = JSON.parse(process.env.PAYLOAD);
const overview = mod.assembleMarketOverview(input);
const out = {{ overview }};
if (input.cases) {{
  out.cases = input.cases.map((c) => ({{ name: c.name, session: mod.sessionOf(new Date(c.now)) }}));
}}
if (input.param_cases) {{
  out.param_cases = input.param_cases.map((p) => ({{
    ok: p.ok,
    value: mod.parseOverviewParams(new URL("https://x.test/api/market-overview" + p.url)),
  }}));
}}
console.log(JSON.stringify(out));
"""


def _assemble(payload: dict) -> dict:
    return _run_node(_ASSEMBLE_SCRIPT, payload)


# ---------------------------------------------------------------------------
# 1) market-overview 契约
# ---------------------------------------------------------------------------


def test_market_overview_top_level_contract():
    result = _assemble(_base_input())["overview"]
    expected_keys = {"schema", "asof", "trade_date", "session", "cache", "indices", "breadth", "sectors", "quality"}
    assert set(result) == expected_keys, f"顶层字段与 §3.1 不一致：{sorted(result)}"
    assert result["schema"] == 1
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$", result["asof"])
    assert result["trade_date"] == "2026-11-25"  # 来自腾讯数据时间戳
    assert result["session"] == "trading"
    assert result["cache"] == {"hit": False, "age_sec": 0, "ttl": 60, "stale": False}


def test_market_overview_indices_contract_and_cleaning():
    result = _assemble(_base_input())["overview"]
    indices = result["indices"]
    assert [item["code"] for item in indices] == ["000300", "000688", "399006", "000905", "000852"]
    for item in indices:
        assert set(item) == {"code", "name", "price", "pct_chg", "amount"}
        assert isinstance(item["price"], (int, float)) and item["price"] > 0
        assert isinstance(item["pct_chg"], (int, float))
        assert isinstance(item["amount"], (int, float)) and item["amount"] > 0
    assert indices[0]["name"] == "沪深300" and indices[0]["pct_chg"] == -1.65

    # 停牌/零值清洗：某指数 price=0 时剔除，并标 degraded
    cleaned = _assemble(_base_input(tencent_overrides={"sh000688": 0.0}))["overview"]
    assert [i["code"] for i in cleaned["indices"]] == ["000300", "399006", "000905", "000852"]
    assert cleaned["quality"]["degraded"] is True
    assert any("tencent:sh000688" in err for err in cleaned["quality"]["errors"])


def test_market_overview_breadth_contract():
    result = _assemble(_base_input())["overview"]
    breadth = result["breadth"]
    assert set(breadth) == {"up", "down", "flat", "limit_up", "limit_down", "broken_rate", "total_amount", "sentiment"}
    assert (breadth["up"], breadth["down"], breadth["flat"]) == (1838, 3267, 114)
    assert (breadth["limit_up"], breadth["limit_down"]) == (64, 4)
    # 炸板率 = 炸板池 / (涨停池 + 炸板池) = 14 / 78
    assert breadth["broken_rate"] == pytest.approx(14 / 78, abs=1e-3)
    # 两市成交额 = 上证 + 深证（腾讯 amount 单位万元 → 元）
    assert breadth["total_amount"] == pytest.approx((61333281 + 70404436) * 10_000, rel=1e-9)
    sentiment = breadth["sentiment"]
    assert set(sentiment) == {"score", "state", "label", "breadth_score", "heat_score"}
    # 宽度 36% + 涨停结构极强 -> 分化（与本地 market.py compute_sentiment 口径一致）
    assert sentiment["state"] == "divergence" and sentiment["label"] == "分化"
    assert sentiment["breadth_score"] == pytest.approx(36.0, abs=0.2)
    assert sentiment["heat_score"] == pytest.approx(94.1, abs=0.2)
    assert 0 <= sentiment["score"] <= 100

    # 炸板池缺失 -> broken_rate=None + degraded
    no_pool = _assemble(_base_input(limitUpPool=None, brokenPool=None))["overview"]
    assert no_pool["breadth"]["broken_rate"] is None
    assert no_pool["quality"]["degraded"] is True


def test_market_overview_sentiment_ice_guard():
    result = _assemble(
        _base_input(activity={"up": 100, "down": 4700, "flat": 20, "limit_up": 5, "limit_down": 60, "active_pct": 2.0})
    )["overview"]
    sentiment = result["breadth"]["sentiment"]
    assert sentiment["state"] == "ice" and sentiment["label"] == "冰点"
    assert sentiment["breadth_score"] == pytest.approx(100 * 100 / 4800, abs=0.1)


def test_market_overview_sectors_contract_and_sorting():
    result = _assemble(_base_input())["overview"]
    sectors = result["sectors"]
    assert set(sectors) == {"industry", "concept"}
    industry = sectors["industry"]
    assert set(industry) == {"gainers", "losers"}
    gainers = industry["gainers"]
    losers = industry["losers"]
    assert len(gainers) <= 3 and len(losers) <= 3
    assert [item["pct_chg"] for item in gainers] == sorted([item["pct_chg"] for item in gainers], reverse=True)
    assert [item["pct_chg"] for item in losers] == sorted([item["pct_chg"] for item in losers])
    for item in gainers + losers:
        assert set(item) == {"name", "pct_chg", "net_inflow", "leader"}
        assert item["net_inflow"] is None  # 新浪行业无净流入字段
    leader = gainers[0]["leader"]
    assert leader == {"symbol": "002100", "name": "天康生物", "pct_chg": 9.98}
    # 概念板块无可用源：空数组 + degraded
    assert sectors["concept"] == {"gainers": [], "losers": []}
    assert result["quality"]["degraded"] is True
    assert "concept:source_unavailable" in result["quality"]["errors"]
    assert set(result["quality"]) == {"sources", "degraded", "errors"}
    assert {"tencent", "legulegu", "sina", "push2ex"} <= set(result["quality"]["sources"])


def test_market_overview_session_and_params():
    cases = [
        {"name": "trading_am", "now": "2026-11-26T10:00:00+08:00"},
        {"name": "lunch", "now": "2026-11-26T12:00:00+08:00"},
        {"name": "trading_pm", "now": "2026-11-26T14:30:00+08:00"},
        {"name": "closed_after", "now": "2026-11-26T18:00:00+08:00"},
        {"name": "weekend", "now": "2026-11-28T10:00:00+08:00"},
    ]
    params = [
        {"url": "?sector_type=industry&sort=gainers&limit=10", "ok": True},
        {"url": "?sector_type=concept&sort=losers&limit=5", "ok": True},
        {"url": "?sector_type=bank&sort=gainers", "ok": False},
        {"url": "?sort=up", "ok": False},
        {"url": "?limit=abc", "ok": False},
        {"url": "?limit=999", "ok": True},
    ]
    payload = _base_input()
    payload["cases"] = cases
    payload["param_cases"] = params
    out = _assemble(payload)
    sessions = {item["name"]: item["session"] for item in out["cases"]}
    assert sessions == {
        "trading_am": "trading",
        "lunch": "lunch",
        "trading_pm": "trading",
        "closed_after": "closed",
        "weekend": "closed",
    }
    for item in out["param_cases"]:
        if item["ok"]:
            assert item["value"] is not None and item["value"]["limit"] <= 30
        else:
            assert item["value"] is None


def test_market_overview_route_wired_with_edge_cache():
    source = (WORKER / "src" / "index.ts").read_text(encoding="utf-8")
    assert '"/api/market-overview"' in source
    assert "buildMarketOverview" in source
    # 路由必须包 cachedRoute（D1 二级缓存），60s TTL，并注入 cache 字段
    block = source[source.index('"/api/market-overview"') : source.index('"/api/market-overview"') + 2200]
    assert "cachedRoute(" in block and "60," in block
    assert "cacheDecorate(60)" in block
    assert "cacheDecorate" in source and "result.cacheState" in source
    cache_src = (WORKER / "src" / "cache.ts").read_text(encoding="utf-8")
    assert "edge_cache" in cache_src, "workers.dev 上 Cache API no-op，必须有 D1 edge_cache 兜底"


# ---------------------------------------------------------------------------
# 2) 边缘缓存：模拟 workers.dev（无 Cache API）+ 假 D1
# ---------------------------------------------------------------------------

_CACHE_SCRIPT = r"""
globalThis.caches = undefined; // 模拟 workers.dev：Cache API put/match no-op
const mod = await import("file://WORKER/src/cache.ts");
const store = new Map();
function makeDb() {
  return {
    prepare(sql) {
      let params = [];
      const stmt = {
        bind(...p) { params = p; return stmt; },
        async first() {
          if (!sql.includes("SELECT payload")) return null;
          const row = store.get(params[0]);
          if (!row) return null;
          return row.expires_at > params[1] ? row : null;
        },
        async run() {
          if (sql.includes("INSERT INTO edge_cache")) {
            const [key, payload, cached_at, expires_at] = params;
            store.set(key, { payload, cached_at, expires_at });
          }
          return { success: true };
        },
      };
      return stmt;
    },
  };
}
const db = makeDb();
const results = {};
const url = (path) => new Request("https://example.test" + path);

let loads = 0;
const reqA = url("/api/a");
results.miss = await mod.withEdgeCache(reqA, 60, async () => { loads += 1; return { n: loads }; }, db);
results.hit = await mod.withEdgeCache(reqA, 60, async () => { loads += 1; return { n: loads }; }, db);
results.loads = loads;

// 全新 key 且 loader 抛错、无 lkg -> 抛出错误（不能凭空造数据）
let threw = false;
try {
  await mod.withEdgeCache(url("/api/b"), 60, async () => { throw new Error("upstream down"); }, db);
} catch { threw = true; }
results.uncached_failure_threw = threw;

// 成功写入后过期再失败 -> STALE（lkg 24h）
const reqC = url("/api/c");
await mod.withEdgeCache(reqC, 1, async () => ({ ok: 1 }), db);
await new Promise((r) => setTimeout(r, 1200));
results.stale = await mod.withEdgeCache(reqC, 1, async () => { throw new Error("upstream down"); }, db);

// 非 200（challenge 401）：PassthroughError 直接上抛，不写缓存、不拿 lkg 冒充
const reqD = url("/api/d");
let passthrough = null;
try {
  await mod.withEdgeCache(reqD, 60, async () => {
    throw new mod.PassthroughError(new Response(JSON.stringify({ challenge: true }), { status: 401 }));
  }, db);
} catch (err) {
  passthrough = { isPassthrough: err instanceof mod.PassthroughError, status: err instanceof mod.PassthroughError ? err.response.status : null };
}
results.passthrough = passthrough;
results.cache_rows = store.size;
console.log(JSON.stringify(results));
"""


def test_edge_cache_workers_dev_semantics():
    script = textwrap.dedent(_CACHE_SCRIPT).replace("WORKER", str(WORKER))
    result = _run_node(script)
    assert result["miss"]["cacheState"] == "MISS" and result["miss"]["data"] == {"n": 1}
    assert result["hit"]["cacheState"] == "HIT" and result["hit"]["data"] == {"n": 1}
    assert result["loads"] == 1, "第二次请求必须命中 D1 缓存，不穿透 loader"
    assert result["uncached_failure_threw"] is True, "无 lkg 时上游失败必须抛出，不能返回伪造数据"
    assert result["stale"]["cacheState"] == "STALE" and result["stale"]["data"] == {"ok": 1}
    assert result["passthrough"]["isPassthrough"] is True and result["passthrough"]["status"] == 401
    # 401 不写缓存（只有 a、c 两个 URL 的成功数据行：fresh+lkg = 4 行）
    assert result["cache_rows"] == 4
