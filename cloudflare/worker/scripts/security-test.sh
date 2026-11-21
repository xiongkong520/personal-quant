#!/usr/bin/env bash
# 安全能力验收（反爬 UA / Accept / 同源 CORS / 安全响应头 / 限流 / Turnstile 人机验证 cookie）
#
# 覆盖需求：
#   无 UA -> 403；curl UA -> 403；无 cookie 访问数据 -> 401 + challenge；
#   正确 Turnstile token（测试 key）-> 拿到 cookie -> /api/signals 200；错误 cookie -> 401；高频 -> 429 + Retry-After。
#
# 前置：服务已启动（二选一）
#   npx wrangler dev --local --port 8788     # 标准方式（本机 workerd 不可用时可跳过）
#   npm run dev:node                         # 无 workerd 的本地回退运行时，行为一致
#
# 用法：
#   BASE=http://127.0.0.1:8788 bash scripts/security-test.sh
#
# 提示：
#   - 默认用 Cloudflare 官方 Turnstile 测试密钥；dev:node 内置 siteverify mock（任何 token 通过）；
#     wrangler dev --local 会用 TURNSTILE_SECRET（.dev.vars，默认测试 secret，测试 secret 必过）。
#   - 限流是每 IP/分钟窗口，高频测试放在最后；中途已触发 429 时请等 60s 或重启服务后重跑。
#   - 本脚本用浏览器 UA 发合法请求；curl 默认 UA 只用于验证“被拦截”。
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8788}"
UA="${BROWSER_UA:-Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36}"
RATE_LIMIT="${RATE_LIMIT_PER_MIN:-60}"
TMP="$(mktemp -d)"
JAR="$TMP/cookie.jar"
trap 'rm -rf "$TMP"' EXIT
FAILED=0

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED=1; }
info() { printf '  \033[36m·\033[0m %s\n' "$1"; }

# 浏览器 UA + Accept 的请求
ok_req() { curl -s --max-time 20 -A "$UA" -H "Accept: ${2:-application/json}" "$@" ; }

echo "== 0. 服务可达（浏览器 UA）=="
CODE="$(curl -s --max-time 20 -A "$UA" -H 'Accept: application/json' -o "$TMP/health.json" -w '%{http_code}' "$BASE/api/health")"
if [ "$CODE" = "200" ]; then
  pass "GET /api/health -> 200（服务已启动）"
else
  fail "服务不可达（HTTP $CODE）。先启动：npx wrangler dev --local --port 8788 或 npm run dev:node"
  exit 1
fi

echo "== 1. 反爬：UA =="
CODE="$(curl -s --max-time 20 -A '' -H 'Accept: application/json' -o "$TMP/no-ua.json" -w '%{http_code}' "$BASE/api/health")"
if [ "$CODE" = "403" ]; then pass "无 User-Agent -> 403"; else fail "无 UA：期望 403，实际 $CODE"; fi

CODE="$(curl -s --max-time 20 -o "$TMP/curl-ua.json" -w '%{http_code}' "$BASE/api/health")" # curl 默认 UA
if [ "$CODE" = "403" ]; then pass "curl 默认 UA -> 403"; else fail "curl UA：期望 403，实际 $CODE"; fi

echo "== 2. 反爬：Accept / 同源 CORS / 安全头 =="
CODE="$(curl -s --max-time 20 -A "$UA" -H 'Accept:' -o /dev/null -w '%{http_code}' "$BASE/api/health")"
if [ "$CODE" = "403" ]; then pass "缺 Accept -> 403"; else fail "缺 Accept：期望 403，实际 $CODE"; fi

CODE="$(curl -s --max-time 20 -A "$UA" -H 'Accept: application/json' -H 'Origin: https://evil.example' -o /dev/null -w '%{http_code}' "$BASE/api/health")"
if [ "$CODE" = "403" ]; then pass "跨域 Origin -> 403"; else fail "跨域 Origin：期望 403，实际 $CODE"; fi

curl -s --max-time 20 -A "$UA" -H 'Accept: application/json' -D "$TMP/h.txt" -o /dev/null "$BASE/api/health"
MISSING=""
for h in content-security-policy x-frame-options referrer-policy x-content-type-options permissions-policy; do
  grep -qi "^$h:" "$TMP/h.txt" || MISSING="$MISSING $h"
done
if [ -z "$MISSING" ]; then pass "安全响应头齐全（CSP/X-Frame-Options/Referrer-Policy/X-Content-Type-Options/Permissions-Policy）"; else fail "缺少安全头:$MISSING"; fi

echo "== 3. 人机验证（Turnstile）=="
CODE="$(curl -s --max-time 20 -A "$UA" -H 'Accept: application/json' -o "$TMP/config.json" -w '%{http_code}' "$BASE/api/config")"
if [ "$CODE" = "200" ] && grep -q '"challenge":true' "$TMP/config.json" && grep -q 'sitekey' "$TMP/config.json"; then
  pass "GET /api/config -> 200 challenge:true + sitekey"
else
  fail "GET /api/config：期望 200+challenge+sitekey，实际 $CODE（$(head -c 200 "$TMP/config.json")）"
fi

CODE="$(curl -s --max-time 20 -A "$UA" -H 'Accept: application/json' -o "$TMP/nocookie.json" -w '%{http_code}' "$BASE/api/signals")"
if [ "$CODE" = "401" ] && grep -q '"challenge":true' "$TMP/nocookie.json"; then
  pass "无 cookie 访问 /api/signals -> 401 + challenge:true"
else
  fail "无 cookie /api/signals：期望 401+challenge，实际 $CODE（$(head -c 200 "$TMP/nocookie.json")）"
fi

CODE="$(curl -s --max-time 25 -A "$UA" -H 'Accept: application/json' -H 'content-type: application/json' \
  -c "$JAR" -o "$TMP/verify.json" -w '%{http_code}' -X POST "$BASE/api/verify" -d '{"token":"XXXX.DUMMY.TOKEN.XXXX"}')"
if [ "$CODE" = "200" ] && grep -q '"ok":true' "$TMP/verify.json" && grep -q 'quant_pass' "$JAR"; then
  pass "POST /api/verify（测试 Turnstile token）-> 200 + Set-Cookie quant_pass"
else
  fail "POST /api/verify：期望 200+cookie，实际 $CODE（$(head -c 300 "$TMP/verify.json")）"
fi

CODE="$(curl -s --max-time 20 -A "$UA" -H 'Accept: application/json' -b "$JAR" -o "$TMP/sig.json" -w '%{http_code}' "$BASE/api/signals")"
if [ "$CODE" = "200" ] && grep -q '"schema":1' "$TMP/sig.json" && grep -q '"items"' "$TMP/sig.json"; then
  pass "带 cookie 访问 /api/signals -> 200 契约 JSON"
else
  fail "带 cookie /api/signals：期望 200，实际 $CODE（$(head -c 200 "$TMP/sig.json")）"
fi

CODE="$(curl -s --max-time 20 -A "$UA" -H 'Accept: application/json' -H 'Cookie: quant_pass=9999999999.zzzz' -o /dev/null -w '%{http_code}' "$BASE/api/signals")"
if [ "$CODE" = "401" ]; then pass "错误/伪造 cookie -> 401"; else fail "错误 cookie：期望 401，实际 $CODE"; fi

CODE="$(curl -s --max-time 20 -A "$UA" -H 'Accept: */*' -H "Origin: $BASE" -H 'Access-Control-Request-Method: POST' -o /dev/null -w '%{http_code}' -X OPTIONS "$BASE/api/verify")"
if [ "$CODE" = "204" ]; then pass "同源 OPTIONS 预检 -> 204"; else fail "同源 OPTIONS：期望 204，实际 $CODE"; fi

echo "== 4. 限流（按 IP ${RATE_LIMIT}/分钟；高频测试）=="
GOT_429=0
for i in $(seq 1 130); do
  CODE="$(curl -s --max-time 10 -A "$UA" -H 'Accept: application/json' -D "$TMP/rate-h.txt" -o "$TMP/rate.json" -w '%{http_code}' "$BASE/api/health")"
  if [ "$CODE" = "429" ]; then GOT_429=1; break; fi
done
if [ "$GOT_429" = "1" ]; then
  pass "高频请求触发 429（第 ${i} 次）"
  if grep -qi '^retry-after: *[0-9]' "$TMP/rate-h.txt"; then pass "429 带 Retry-After 响应头"; else fail "429 缺少 Retry-After 响应头"; fi
  grep -q '"retry_after"' "$TMP/rate.json" && pass "429 body 含 retry_after" || fail "429 body 缺少 retry_after"
  grep -qi '^x-ratelimit-limit:' "$TMP/rate-h.txt" && pass "429 带 X-RateLimit-Limit" || fail "429 缺少 X-RateLimit-Limit"
else
  fail "130 次高频请求未触发 429（检查 RATE_LIMIT_PER_MIN / 是否命中同一 IP 桶）"
fi

echo
if [ "$FAILED" -ne 0 ]; then
  echo -e "\033[31mSECURITY TEST FAILED\033[0m  BASE=$BASE"
  exit 1
fi
echo -e "\033[32mSECURITY TEST OK\033[0m  BASE=$BASE"
