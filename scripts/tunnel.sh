#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 本地数据服务公网隧道：cloudflared quick tunnel（trycloudflare 免费临时域名）
#
# 作用：把 http://127.0.0.1:8899（python -m quant.local_server）临时映射到公网
#       https://<随机词>.trycloudflare.com，手机/外网浏览器可直接访问本地服务。
#       不需要 Cloudflare 账号、不需要域名；进程退出后 URL 立即失效。
#
# 用法：
#   bash scripts/tunnel.sh                     # 前台运行，打印公网 URL + 自检
#   QUANT_PORT=8898 bash scripts/tunnel.sh     # 指向其它本地端口
#   后台常驻：
#     nohup bash scripts/tunnel.sh > /tmp/quant-tunnel.log 2>&1 &
#     grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/quant-tunnel.log | head -1
#
# 环境变量：
#   QUANT_PORT               本地数据服务端口（默认 8899）
#   QUANT_LOCAL_URL          直接指定本地服务地址（默认 http://127.0.0.1:$QUANT_PORT）
#   QUANT_CLOUDFLARED        显式指定 cloudflared 可执行文件
#   QUANT_TUNNEL_BIN_DIR     二进制缓存目录（默认 ~/.cache/quant-tunnel）
#   QUANT_TUNNEL_LOG_DIR     日志/URL 输出目录（默认 /tmp）
#   QUANT_SKIP_HEALTH=1      跳过本地服务健康检查（不推荐）
#
# 依赖：curl；下载优先 aria2c（多线程，GitHub 直连慢时显著加速），
#       兜底 Cloudflare 官方 apt 包解包（dpkg-deb），无需 root 安装。
# 说明：cloudflared 完全免费；本服务只读行情，不接收任何写操作。
# ---------------------------------------------------------------------------
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${QUANT_PORT:-8899}"
LOCAL_URL="${QUANT_LOCAL_URL:-http://127.0.0.1:${PORT}}"
BIN_DIR="${QUANT_TUNNEL_BIN_DIR:-$HOME/.cache/quant-tunnel}"
LOG_DIR="${QUANT_TUNNEL_LOG_DIR:-/tmp}"
BIN="${QUANT_CLOUDFLARED:-$BIN_DIR/cloudflared}"
LOG="$LOG_DIR/quant-cloudflared.log"
URL_FILE="$LOG_DIR/quant-tunnel-url.txt"
REPO="https://pkg.cloudflare.com/cloudflared"

mkdir -p "$BIN_DIR" "$LOG_DIR" 2>/dev/null || true

info() { printf '[tunnel] %s\n' "$*"; }
warn() { printf '[tunnel][警告] %s\n' "$*" >&2; }
die() { printf '[tunnel][错误] %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || die "需要 curl（apt-get install -y curl）"
command -v uname >/dev/null 2>&1 || die "需要 uname"

# 下载参数：交互终端显示进度条，重定向到日志时静默（避免日志被进度条刷爆）
CURL_FLAGS=(-fL --retry 2 --connect-timeout 10 --retry-delay 2)
if [ -t 2 ]; then
  CURL_FLAGS+=(--progress-bar)
else
  CURL_FLAGS+=(-sS)
fi

# ---------------------------------------------------------------------------
# 1) 准备 cloudflared（缓存 > PATH > 下载）
# ---------------------------------------------------------------------------
arch_of() {
  case "$(uname -m)" in
    x86_64 | amd64) echo "amd64" ;;
    aarch64 | arm64) echo "arm64" ;;
    armv7l | armv7) echo "arm" ;;
    *) echo "" ;;
  esac
}

download_via_github() {
  local arch="$1" url
  url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"
  info "下载 cloudflared（${arch}，约 36MB）：${url}"
  if command -v aria2c >/dev/null 2>&1; then
    # 国内直连 GitHub 单线程常 <50KB/s；aria2 多连接通常 300KB/s+（实测）
    aria2c -x16 -s16 -k1M --file-allocation=none --allow-overwrite=true \
      --auto-file-renaming=false --console-log-level=warn --summary-interval=10 \
      -d "$BIN_DIR" -o cloudflared "$url" && return 0
    warn "aria2 下载失败，改试 curl"
  fi
  curl "${CURL_FLAGS[@]}" -o "$BIN" "$url"
}

download_via_deb() {
  local deb_arch arch pkgs filename tmpdir
  arch="$(arch_of)"
  case "$arch" in
    amd64) deb_arch="amd64" ;;
    arm64) deb_arch="arm64" ;;
    *) return 1 ;;
  esac
  command -v dpkg-deb >/dev/null 2>&1 || {
    warn "无 dpkg-deb，无法解包 .deb 兜底"
    return 1
  }
  info "改用 Cloudflare 官方 apt 仓库下载 .deb 并解包（不安装系统包）..."
  pkgs="$(curl -fsSL --connect-timeout 10 "$REPO/dists/any/main/binary-${deb_arch}/Packages" 2>/dev/null)" || return 1
  filename="$(printf '%s\n' "$pkgs" | awk -F': ' '/^Filename:/{print $2; exit}')"
  [ -n "$filename" ] || return 1
  tmpdir="$(mktemp -d)"
  if ! curl "${CURL_FLAGS[@]}" -o "$tmpdir/cf.deb" "$REPO/$filename"; then
    rm -rf "$tmpdir"
    return 1
  fi
  if dpkg-deb -x "$tmpdir/cf.deb" "$tmpdir/out" 2>/dev/null; then
    local found
    found="$(find "$tmpdir/out" -type f -name cloudflared | head -1 || true)"
    if [ -n "$found" ]; then
      cp -f "$found" "$BIN" && chmod +x "$BIN"
      rm -rf "$tmpdir"
      return 0
    fi
  fi
  rm -rf "$tmpdir"
  return 1
}

ensure_cloudflared() {
  if [ -n "${QUANT_CLOUDFLARED:-}" ] && [ -x "$QUANT_CLOUDFLARED" ]; then
    BIN="$QUANT_CLOUDFLARED"
    info "使用指定 cloudflared: $BIN"
    return 0
  fi
  if command -v cloudflared >/dev/null 2>&1; then
    BIN="$(command -v cloudflared)"
    info "使用 PATH 中的 cloudflared: $BIN"
    return 0
  fi
  if [ -x "$BIN" ]; then
    info "使用缓存二进制: $BIN"
    return 0
  fi

  local arch
  arch="$(arch_of)"
  [ -n "$arch" ] || die "不支持的 CPU 架构: $(uname -m)（仅支持 x86_64/arm64/armv7）"

  rm -f "$BIN" 2>/dev/null || true
  # 下载策略：aria2 多线程 GitHub（最快）→ Cloudflare 官方 apt 包解包（快且稳定）
  # → curl 单线程 GitHub（最后兜底，国内可能很慢）。实测：aria2 约 300KB/s+，
  # 官方 .deb 约 1MB/s，curl 直连 GitHub 约 20KB/s。
  if command -v aria2c >/dev/null 2>&1; then
    download_via_github "$arch" || warn "aria2 下载 GitHub 失败"
  fi
  if [ ! -x "$BIN" ] || ! "$BIN" --version >/dev/null 2>&1; then
    download_via_deb || warn "官方 apt 包下载失败"
  fi
  if [ ! -x "$BIN" ] || ! "$BIN" --version >/dev/null 2>&1; then
    warn "改用 curl 单线程下载 GitHub（可能较慢）"
    download_via_github "$arch" || true
  fi
  if [ ! -x "$BIN" ] || ! "$BIN" --version >/dev/null 2>&1; then
    die "cloudflared 下载失败。可手动下载后放到 $BIN 并 chmod +x，或用 QUANT_CLOUDFLARED=/path/to/cloudflared 指定"
  fi
  chmod +x "$BIN" 2>/dev/null || true
  [ -x "$BIN" ] || die "cloudflared 不可执行: $BIN"
  "$BIN" --version >/dev/null 2>&1 || die "cloudflared 无法运行（架构不匹配？）: $BIN"
  info "cloudflared 就绪: $("$BIN" --version 2>/dev/null | head -1)"
}

# ---------------------------------------------------------------------------
# 2) 检查本地数据服务
# ---------------------------------------------------------------------------
if [ "${QUANT_SKIP_HEALTH:-0}" != "1" ]; then
  if ! curl -sf -m 3 "$LOCAL_URL/api/health" >/dev/null 2>&1; then
    warn "本地服务未就绪：$LOCAL_URL/api/health"
    echo "  先启动本地数据服务：" >&2
    echo "    cd $ROOT_DIR && python -m quant.local_server --port $PORT" >&2
    die "本地服务未启动，隧道无意义"
  fi
  info "本地服务健康检查通过: $LOCAL_URL/api/health"
fi

ensure_cloudflared

# ---------------------------------------------------------------------------
# 3) 启动隧道并提取公网 URL
# ---------------------------------------------------------------------------
rm -f "$LOG" "$URL_FILE" 2>/dev/null || true
info "启动 cloudflared quick tunnel → $LOCAL_URL （Ctrl-C 停止）"
"$BIN" tunnel --url "$LOCAL_URL" --no-autoupdate >"$LOG" 2>&1 &
CF_PID=$!

TAIL_PID=""
cleanup() {
  [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null || true
  if [ -n "${CF_PID:-}" ] && kill -0 "$CF_PID" 2>/dev/null; then
    kill "$CF_PID" 2>/dev/null || true
    wait "$CF_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# 实时转播 cloudflared 日志（用户能在前台看到连接状态）
tail -n +1 -f "$LOG" &
TAIL_PID=$!

PUBLIC_URL=""
for _ in $(seq 1 120); do
  if ! kill -0 "$CF_PID" 2>/dev/null; then
    warn "cloudflared 提前退出，日志如下：" >&2
    tail -n 20 "$LOG" >&2 || true
    die "隧道启动失败（常见原因：网络不可达 / 免费隧道被限流）"
  fi
  PUBLIC_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1 || true)"
  [ -n "$PUBLIC_URL" ] && break
  sleep 0.5
done
if [ -z "$PUBLIC_URL" ]; then
  warn "60 秒内未拿到公网 URL，日志：$LOG"
  die "隧道未就绪"
fi
printf '%s\n' "$PUBLIC_URL" >"$URL_FILE" 2>/dev/null || true

echo
echo "=================================================================="
echo " 本地数据服务公网隧道已建立（trycloudflare 免费临时域名）"
echo "   本地服务 : $LOCAL_URL"
echo "   公网地址 : $PUBLIC_URL"
echo "   健康检查 : $PUBLIC_URL/api/health"
echo "   行情示例 : $PUBLIC_URL/api/quote?codes=sh000001,sz399001"
echo "   URL 已写入: $URL_FILE"
echo "   注意：免费临时域名每次启动都会变；Ctrl-C 或进程退出后立即失效。"
echo "=================================================================="
echo

# ---------------------------------------------------------------------------
# 4) 公网自检（DNS/边缘生效可能要几秒）
# ---------------------------------------------------------------------------
info "公网自检（最多约 50s；实测 trycloudflare 首次 DNS/边缘生效约需 10–40s）..."
PUBLIC_OK=0
for _ in $(seq 1 25); do
  if curl -sf -m 8 "$PUBLIC_URL/api/health" >/dev/null 2>&1; then
    PUBLIC_OK=1
    break
  fi
  sleep 2
done
if [ "$PUBLIC_OK" = "1" ]; then
  info "公网自检通过: $PUBLIC_URL/api/health"
else
  warn "公网自检暂未通过（隧道可能仍在建立/被限流）：稍后手动 curl $PUBLIC_URL/api/health"
  warn "日志：$LOG"
fi

info "隧道运行中，按 Ctrl-C 停止（后台运行参考文件头注释）"
wait "$CF_PID"
