#!/usr/bin/env bash
# 盘中实时更新：每 N 秒（默认 300）生成市场整体快照并推送到云端看板。
# 用法：bash scripts/market_update_loop.sh   （Ctrl-C 停止）
# 环境变量：MARKET_INTERVAL_SEC 覆盖间隔；QUANT_INGEST_TOKEN 从 ~/.quant_secrets/cloudflare.env 读取
set -uo pipefail
cd "$(dirname "$0")/.."

INTERVAL="${MARKET_INTERVAL_SEC:-300}"
if [ -f "$HOME/.quant_secrets/cloudflare.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$HOME/.quant_secrets/cloudflare.env"
  set +a
fi

echo "[market-loop] 启动，间隔 ${INTERVAL}s（Ctrl-C 停止）"
while true; do
  ts="$(date '+%H:%M:%S')"
  if python -m quant.cli market >/tmp/market_build.log 2>&1; then
    if python -m quant.cli export --target cloudflare >>/tmp/market_push.log 2>&1; then
      echo "[$ts] 市场快照已更新并推送"
    else
      echo "[$ts] 推送失败（见 /tmp/market_push.log），下轮重试"
    fi
  else
    echo "[$ts] 数据拉取失败/限流（见 /tmp/market_build.log），下轮重试"
  fi
  sleep "$INTERVAL"
done
