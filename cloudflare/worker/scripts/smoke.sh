#!/usr/bin/env bash
# 一键烟测 Worker 端点 + 人机验证流程（不联网、不需要 Cloudflare 账号）。
# 需要服务已启动：npx wrangler dev --local --port 8788 或 npm run dev:node
#
# 用法：
#   BASE=http://127.0.0.1:8788 QUANT_INGEST_TOKEN=dev-secret-change-me bash scripts/smoke.sh
#
# 说明：默认用浏览器 UA 发请求（Worker 有反爬，curl 默认 UA 会被 403）。
# 退出码 0 = 全部通过；非 0 = 有断言失败（会打印原因）。
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8788}"
TOKEN="${QUANT_INGEST_TOKEN:-dev-secret-change-me}"
UA="${BROWSER_UA:-Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36}"
TMP="$(mktemp -d)"
JAR="$TMP/cookie.jar"
trap 'rm -rf "$TMP"' EXIT
FAILED=0

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED=1; }

# curl 封装：浏览器 UA + Accept
cget() { curl -s --max-time 20 -A "$UA" -H "Accept: application/json" "$@"; }

# 断言 HTTP 状态码 + 可选响应片段
check_http() { # name want method path [body] [needle]
  local name=$1 want=$2 method=$3 path=$4 body=${5:-} needle=${6:-}
  local out="$TMP/resp.json"
  local -a args=(-s --max-time 20 -A "$UA" -H 'Accept: application/json' -o "$out" -w '%{http_code}' -X "$method" "$BASE$path")
  [ -n "$body" ] && args+=(-H 'content-type: application/json' -d "$body")
  local code
  code="$(curl "${args[@]}")"
  if [ "$code" != "$want" ]; then
    fail "$name：期望 HTTP $want，实际 $code（$(head -c 200 "$out")）"
    return 1
  fi
  if [ -n "$needle" ] && ! grep -q "$needle" "$out"; then
    fail "$name：响应缺少 \"$needle\"（$(head -c 200 "$out")）"
    return 1
  fi
  pass "$name（HTTP $code）"
}

# 端到端：verify -> cookie -> signals
echo "== 健康检查 / 配置 =="
check_http "GET /api/health" 200 GET /api/health "" '"schema":1'
cget "$BASE/api/health" -o "$TMP/health.json"
python3 - "$TMP/health.json" <<'PY' || FAILED=1
import json, sys
h = json.load(open(sys.argv[1]))
checks = [
    ("schema == 1", h.get("schema") == 1),
    ("ok is True", h.get("ok") is True),
    ("d1.status == ok", h.get("d1", {}).get("status") == "ok"),
    ("ingest.token_configured 已配置", h.get("ingest", {}).get("token_configured") is True),
    ("security.ua_filter == True", h.get("security", {}).get("ua_filter") is True),
    ("安全响应头/限流已启用", h.get("security", {}).get("rate_limit_per_min", 0) > 0),
    ("model.version 非空", bool(h.get("model", {}).get("version"))),
]
for name, ok in checks:
    print(("  \033[32m✓\033[0m " if ok else "  \033[31m✗\033[0m ") + name)
raise SystemExit(0 if all(ok for _, ok in checks) else 1)
PY

check_http "GET /api/config" 200 GET /api/config "" '"challenge":true'
echo "== 反爬 =="
CODE="$(curl -s --max-time 15 -o "$TMP/bot.json" -w '%{http_code}' "$BASE/api/health")"
[ "$CODE" = "403" ] && pass "curl 默认 UA -> 403" || fail "curl 默认 UA：期望 403，实际 $CODE"
CODE="$(curl -s --max-time 15 -A '' -H 'Accept: application/json' -o /dev/null -w '%{http_code}' "$BASE/api/health")"
[ "$CODE" = "403" ] && pass "无 User-Agent -> 403" || fail "无 User-Agent：期望 403，实际 $CODE"

echo "== 人机验证 =="
CODE="$(curl -s --max-time 15 -A "$UA" -H 'Accept: application/json' -o "$TMP/nocookie.json" -w '%{http_code}' "$BASE/api/signals")"
if [ "$CODE" = "401" ] && grep -q '"challenge":true' "$TMP/nocookie.json"; then
  pass "无 cookie 访问 /api/signals -> 401 + challenge:true"
else
  fail "无 cookie /api/signals：期望 401+challenge，实际 $CODE（$(head -c 200 "$TMP/nocookie.json")）"
fi

CODE="$(curl -s --max-time 20 -A "$UA" -H 'Accept: application/json' -H 'content-type: application/json' \
  -c "$JAR" -o "$TMP/verify.json" -w '%{http_code}' -X POST "$BASE/api/verify" -d '{"token":"XXXX.DUMMY.TOKEN.XXXX"}')"
if [ "$CODE" = "200" ] && grep -q '"ok":true' "$TMP/verify.json" && grep -q 'quant_pass' "$JAR"; then
  pass "POST /api/verify -> 200 + Set-Cookie quant_pass"
else
  fail "POST /api/verify：期望 200+cookie，实际 $CODE（$(head -c 200 "$TMP/verify.json")）"
fi

CODE="$(curl -s --max-time 15 -A "$UA" -H 'Accept: application/json' -b "$JAR" -o "$TMP/sig.json" -w '%{http_code}' "$BASE/api/signals")"
if [ "$CODE" = "200" ]; then
  python3 - "$TMP/sig.json" <<'PY' && pass "带 cookie 访问 /api/signals -> 200 契约字段" || true
import json, re, sys
d = json.load(open(sys.argv[1]))
assert d.get("schema") == 1 and isinstance(d.get("items"), list)
for it in d["items"]:
    for k in ("symbol", "asof", "trade_date", "price", "direction", "prob", "confidence",
              "horizon", "expires_at", "model_version", "status", "top_features"):
        assert k in it, f"item 缺字段 {k}"
    p = it["prob"]
    assert abs(p["up"] + p["flat"] + p["down"] - 1) <= 1e-6
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$", it["asof"])
print(f"  \033[32m✓\033[0m signals 契约字段（{len(d['items'])} 条）")
PY
else
  fail "带 cookie /api/signals：期望 200，实际 $CODE（$(head -c 200 "$TMP/sig.json")）"
fi

CODE="$(curl -s --max-time 15 -A "$UA" -H 'Accept: application/json' -b "$JAR" -o /dev/null -w '%{http_code}' -H 'Cookie: quant_pass=9999999999.zzzz' "$BASE/api/signals")"
[ "$CODE" = "401" ] && pass "错误 cookie -> 401" || fail "错误 cookie：期望 401，实际 $CODE"

echo "== 安全响应头 / CORS =="
curl -s --max-time 15 -A "$UA" -H 'Accept: application/json' -D "$TMP/headers.txt" -o /dev/null "$BASE/api/health"
for h in content-security-policy x-frame-options referrer-policy x-content-type-options permissions-policy; do
  grep -qi "^$h:" "$TMP/headers.txt" && pass "安全头 $h" || fail "缺少安全头 $h"
done
CODE="$(curl -s --max-time 15 -A "$UA" -H 'Accept: */*' -H 'Origin: http://evil.example' -H 'Access-Control-Request-Method: POST' -o /dev/null -w '%{http_code}' -X OPTIONS "$BASE/api/verify")"
[ "$CODE" = "403" ] && pass "跨域 OPTIONS -> 403" || fail "跨域 OPTIONS：期望 403，实际 $CODE"
CODE="$(curl -s --max-time 15 -A "$UA" -H 'Accept: */*' -H "Origin: $BASE" -H 'Access-Control-Request-Method: POST' -o /dev/null -w '%{http_code}' -X OPTIONS "$BASE/api/verify")"
[ "$CODE" = "204" ] && pass "同源 OPTIONS -> 204" || fail "同源 OPTIONS：期望 204，实际 $CODE"

echo "== ingest 鉴权 =="
check_http "错误 token -> 401" 401 POST /api/ingest '{"symbol":"600519"}' 'unauthorized'
CODE="$(curl -s --max-time 20 -A "$UA" -H 'Accept: application/json' -H 'content-type: application/json' -H "x-ingest-token: $TOKEN" \
  -o "$TMP/ing.json" -w '%{http_code}' -X POST "$BASE/api/ingest" -d '{
  "symbol":"600519","asof":"2026-11-21T14:35:00+08:00","trade_date":"2026-11-21","price":1680.5,
  "direction":"up","prob":{"up":0.42,"flat":0.31,"down":0.27},"confidence":0.42,"horizon":"1d",
  "expires_at":"2026-11-24T09:30:00+08:00","model_version":"v0.1.0-smoke","status":"ok",
  "top_features":[{"name":"ret_5d","value":0.031,"contrib":0.08}]}')"
if [ "$CODE" = "200" ] && grep -q '"accepted":1' "$TMP/ing.json"; then
  pass "正确 token -> 200 写入"
else
  fail "正确 token ingest：期望 200，实际 $CODE（$(head -c 200 "$TMP/ing.json")）"
fi

echo "== SSE =="
timeout 8 curl -s -N --max-time 8 -A "$UA" -H 'Accept: text/event-stream' -b "$JAR" "$BASE/api/stream" > "$TMP/sse.txt" 2>/dev/null || true
if grep -q 'event: hello' "$TMP/sse.txt" && grep -q 'event: signals' "$TMP/sse.txt" && grep -q '"heartbeat_sec":30' "$TMP/sse.txt"; then
  pass "GET /api/stream 收到 hello + signals（heartbeat_sec=30）"
else
  fail "SSE 未收到 hello/signals 事件（前 3 行：$(head -3 "$TMP/sse.txt" | tr '\n' ' ')）"
fi

echo "== 静态资源 =="
CODE="$(curl -s --max-time 15 -A "$UA" -H 'Accept: text/html' -o "$TMP/root.txt" -w '%{http_code}' "$BASE/")"
if [ "$CODE" = "200" ] && { grep -qi '<!doctype html>' "$TMP/root.txt" || grep -q '"message"' "$TMP/root.txt"; }; then
  pass "GET /（看板或目录为空提示 JSON）"
else
  fail "GET /：HTTP $CODE，内容不认识（$(head -c 160 "$TMP/root.txt")）"
fi

echo
if [ "$FAILED" -ne 0 ]; then
  echo -e "\033[31mSMOKE FAILED\033[0m"
  exit 1
fi
echo -e "\033[32mSMOKE OK\033[0m  BASE=$BASE"
