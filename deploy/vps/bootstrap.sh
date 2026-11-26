#!/usr/bin/env bash
# =============================================================================
# 全市场数据 + CPU 训练 · VPS 一键安装（Ubuntu 22.04 / 24.04）
#
# 用法（第一次）：
#   curl -fsSL https://raw.githubusercontent.com/xiongkong520/personal-quant/main/deploy/vps/bootstrap.sh | bash
# 或者：
#   git clone https://github.com/xiongkong520/personal-quant && cd personal-quant
#   sudo bash deploy/vps/bootstrap.sh
#
# 想装到别处：APP_DIR=/data/quant bash deploy/vps/bootstrap.sh
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/xiongkong520/personal-quant.git}"
APP_DIR="${APP_DIR:-/opt/personal-quant}"
PYTHON="${PYTHON:-python3}"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

step "1/6 系统依赖"
$SUDO apt-get update -y
$SUDO apt-get install -y git curl ca-certificates "${PYTHON}" "${PYTHON}-venv" "${PYTHON}-dev"

step "2/6 拉代码到 ${APP_DIR}"
$SUDO mkdir -p "$(dirname "$APP_DIR")"
if [ ! -d "$APP_DIR/.git" ]; then
  $SUDO git clone --depth 1 "$REPO_URL" "$APP_DIR"
else
  (cd "$APP_DIR" && $SUDO git pull --ff-only) || true
fi
$SUDO chown -R "$(id -u):$(id -g)" "$APP_DIR"
cd "$APP_DIR"

step "3/6 Python 虚拟环境"
[ -d .venv ] || "$PYTHON" -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install -U pip wheel

step "4/6 依赖（数据源 + LightGBM + TensorFlow）"
# 先装 TF：它的 numpy 上限比 akshare/pandas 更紧，先装能让 pip 选出兼容版本
# GPU=1 时装 CUDA 版（AutoDL/云 GPU 机器用；需要 NVIDIA 驱动）
if [ "${GPU:-0}" = "1" ]; then
  pip install "tensorflow[and-cuda]>=2.18,<2.22"
else
  pip install "tensorflow-cpu>=2.18,<2.22"
fi
pip install lightgbm pyarrow baostock
pip install -r training/requirements.txt
python - <<'PY'
import numpy, pandas, pyarrow, lightgbm, baostock
print("numpy", numpy.__version__, "| pandas", pandas.__version__, "| pyarrow", pyarrow.__version__,
      "| lightgbm", lightgbm.__version__)
try:
    import tensorflow as tf
    print("tensorflow", tf.__version__)
except Exception as exc:  # noqa: BLE001
    print("tensorflow 不可用（NN 训练会跳过）：", exc)
PY

step "5/6 首次全市场抓取（可跳过：SKIP_FETCH=1）"
if [ "${SKIP_FETCH:-0}" != "1" ]; then
  bash deploy/vps/daily_train.sh || echo "首次抓取/训练有报错，先看日志，下面仍会装好定时任务"
fi

step "6/6 装 systemd 定时任务（每个交易日 17:40 北京时间跑增量）"
$SUDO install -m 644 deploy/vps/quant-train.service /etc/systemd/system/quant-train.service
$SUDO install -m 644 deploy/vps/quant-train.timer /etc/systemd/system/quant-train.timer
$SUDO sed -i "s|__APP_DIR__|${APP_DIR}|g" /etc/systemd/system/quant-train.service
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now quant-train.timer

cat <<EOF

安装完成 ✅

  代码目录 : ${APP_DIR}
  虚拟环境 : ${APP_DIR}/.venv
  全市场数据: ${APP_DIR}/training/data_full/daily.parquet
  定时任务 : systemctl list-timers quant-train.timer
  看日志   : journalctl -u quant-train.service -f
  手动跑   : bash ${APP_DIR}/deploy/vps/daily_train.sh

下一步（把训练结果自动推回仓库）：
  1) 在这台机器上生成部署密钥：ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
  2) 把 ~/.ssh/id_ed25519.pub 加到 GitHub 仓库 Settings → Deploy keys（勾选 Allow write access）
  3) git remote set-url origin git@github.com:xiongkong520/personal-quant.git
EOF
