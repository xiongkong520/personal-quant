#!/usr/bin/env bash
# =============================================================================
# 每日流水线：全市场增量抓取 → 训练（LightGBM + 可选 NN）→ 导出模型 → git 提交
#
# 环境变量（都可不填）：
#   APP_DIR=       代码目录，默认 /opt/personal-quant
#   START=         历史起点，默认 2024-01-01（之后每天只增量补新数据）
#   UNIVERSE=      抓取上限，0=全市场（默认），例如 UNIVERSE=1500 只抓前 1500
#   WORKERS=       抓取并行分片数，默认 4（小机器别开太多）
#   LGBM_LIMIT=    LightGBM 训练池（流动性前 N），默认 800
#   NN_LIMIT=      NN 训练池，默认 0=不训（CPU 慢，需要时设 200）
#   PUSH=          1=训练完 git 提交并推送（需部署密钥），默认 1
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/personal-quant}"
START="${START:-2024-01-01}"
UNIVERSE="${UNIVERSE:-0}"
WORKERS="${WORKERS:-4}"
LGBM_LIMIT="${LGBM_LIMIT:-800}"
NN_LIMIT="${NN_LIMIT:-0}"
PUSH="${PUSH:-1}"

cd "$APP_DIR"
# shellcheck disable=SC1091
. .venv/bin/activate

ts() { date '+%H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*"; }

log "1/5 全市场抓取（Baostock，${WORKERS} 分片，起点 ${START}）"
FETCH_ARGS=(--workers "$WORKERS" --start "$START")
if [ "$UNIVERSE" != "0" ]; then FETCH_ARGS+=(--limit "$UNIVERSE"); fi
python training/tpu/fetch_full_market.py "${FETCH_ARGS[@]}"

log "2/5 切训练子集（流动性前 ${LGBM_LIMIT} 只 → training/data）"
python training/tpu/make_train_subset.py --src training/data_full --limit "$LGBM_LIMIT" --out training/data

log "3/5 LightGBM Walk-Forward + 导出轻量模型 JSON"
python training/train_lgbm.py \
  --daily training/data/daily.parquet \
  --index training/data/index.parquet \
  --out-dir training/out \
  --num-threads "$(nproc)"
python training/export_model.py --max-size-mb 2.0

if [ "$NN_LIMIT" != "0" ]; then
  log "4/5 神经网络（GRU，训练池 ${NN_LIMIT} 只；失败不阻塞）"
  python training/tpu/make_train_subset.py --src training/data_full --limit "$NN_LIMIT" --out training/data_nn
  python training/tpu/nn_tf.py \
    --data-dir training/data_nn --out-dir training/tpu/out \
    --epochs 12 --max-folds 4 --batch 512 --no-class-weight \
    || log "NN 训练失败，已跳过（LightGBM 模型不受影响）"
  if [ -f training/tpu/out/nn_model.json ]; then
    cp -f training/tpu/out/nn_model.json model/nn_model.json
    cp -f training/tpu/out/nn_metrics.json training/tpu/last_run.json
  fi
else
  log "4/5 跳过神经网络（NN_LIMIT=0）"
fi

log "5/5 归档 + 提交"
mkdir -p training/tpu
ls -la model/ | sed -n '1,10p'
if [ "$PUSH" = "1" ]; then
  git add model/ training/tpu/last_run.json 2>/dev/null || true
  git -c user.name="quant-bot" -c user.email="quant-bot@users.noreply.github.com" \
      commit -m "chore(train): 全市场 CPU 训练 $(date -u +%F)" || log "无变更，跳过提交"
  git push || log "推送失败：检查 deploy key / remote 是否改成 ssh"
else
  log "PUSH=0，跳过提交"
fi

log "完成 ✅  模型：$(ls -la model/latest_model.json 2>/dev/null | awk '{print $5" bytes"}' || echo '未生成')"
