#!/usr/bin/env bash
# 一键部署到 Cloudflare（Worker + D1 + Pages assets）
# 前置：npx wrangler login 或 CLOUDFLARE_API_TOKEN；本地密钥在 ~/.quant_secrets/cloudflare.env
set -euo pipefail
cd "$(dirname "$0")/../cloudflare/worker"

# 1) 本地真实配置（含真实 D1 id / sitekey），不入库
if [ -f "$HOME/.quant_secrets/cloudflare.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$HOME/.quant_secrets/cloudflare.env"
  set +a
fi
if [ -f wrangler.local.toml ]; then
  cp wrangler.local.toml wrangler.toml
  echo "[deploy] 使用 wrangler.local.toml（真实 D1 id）"
else
  echo "[deploy] 警告：无 wrangler.local.toml，使用仓库版 wrangler.toml（database_id 为占位符，需先创建 D1）"
fi

# 2) 远端 D1 迁移
npx wrangler d1 migrations apply personal_quant --remote

# 3) 部署（Worker + Assets）
npx wrangler deploy

echo "[deploy] 完成。公网地址见上方输出。"
echo "[deploy] 验证：BASE=<公网地址> bash scripts/security-test.sh"
