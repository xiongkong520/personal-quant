#!/usr/bin/env bash
# 全链路冒烟：fetch → train → backtest → signal → paper → serve
# 用法：bash scripts/smoke.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src

SYMBOLS="${SMOKE_SYMBOLS:-600519,000001}"
START="${SMOKE_START:-2024-01-01}"
END="${SMOKE_END:-2026-11-20}"

echo "== 1/6 抓取数据 =="
python -m quant.cli fetch --symbols "$SYMBOLS" --start "$START" --end "$END"

echo "== 2/6 训练模型 =="
python -m quant.cli train

echo "== 3/6 历史回测 / 胜率 =="
python -m quant.cli backtest

echo "== 4/6 生成信号 =="
python -m quant.cli signal --symbols "$SYMBOLS"

echo "== 5/6 模拟盘推进 =="
python -m quant.cli paper --init-cash 1000000 --advance 5

echo "== 6/6 本地服务健康检查 =="
python -m quant.cli serve --port 8787 &
SERVER_PID=$!
cleanup() { kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT
for i in $(seq 1 20); do
  if curl -sf -m 2 http://127.0.0.1:8787/api/health >/dev/null 2>&1; then break; fi
  sleep 0.5
done
curl -sf -m 5 http://127.0.0.1:8787/api/health
echo
curl -sf -m 5 http://127.0.0.1:8787/api/signals | head -c 600
echo
echo "== 冒烟通过 =="
