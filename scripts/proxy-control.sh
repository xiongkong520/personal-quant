#!/usr/bin/env bash
# 本机代理控制：检测手机 V2VPN（v2ray 客户端）的本地代理端口并应用
# 用法：
#   source scripts/proxy-control.sh        # 检测并应用
#   source scripts/proxy-control.sh off    # 关闭
#   source scripts/proxy-control.sh status # 查看
# 说明：V2VPN 在 Android 上通常监听 127.0.0.1:10808(socks)/10809(http)；
#       proot 与本机共享 loopback，可直接使用。

detect_proxy_port() {
  for port in 10809 10808 7890 7891 1080 8888 8118; do
    if timeout 1 bash -c "echo > /dev/tcp/127.0.0.1/$port" 2>/dev/null; then
      echo "$port"; return 0
    fi
  done
  return 1
}

case "${1:-on}" in
  off|unset)
    unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
    echo "代理已关闭"
    ;;
  status)
    echo "http_proxy=${http_proxy:-未设置}"
    echo "https_proxy=${https_proxy:-未设置}"
    echo "出口IP: $(timeout 10 curl -s -m 8 https://api.ipify.org 2>/dev/null || echo '获取失败')"
    echo "Google: $(timeout 10 curl -s -m 8 -o /dev/null -w '%{http_code}' https://www.google.com 2>/dev/null || echo '不可达')"
    ;;
  on|*)
    PORT="$(detect_proxy_port)"
    if [ -z "$PORT" ]; then
      echo "未检测到本地代理端口（请先在手机上打开 V2VPN 并连接节点）"
      return 1 2>/dev/null || exit 1
    fi
    export http_proxy="http://127.0.0.1:$PORT"
    export https_proxy="$http_proxy"
    export all_proxy="socks5://127.0.0.1:$PORT"
    export HTTP_PROXY="$http_proxy" HTTPS_PROXY="$https_proxy" ALL_PROXY="$all_proxy"
    echo "代理已启用：127.0.0.1:$PORT"
    echo "测试 Google: $(timeout 12 curl -s -m 10 -o /dev/null -w '%{http_code}' https://www.google.com 2>/dev/null || echo '不可达')"
    echo "出口IP: $(timeout 12 curl -s -m 10 https://api.ipify.org 2>/dev/null || echo '获取失败')"
    ;;
esac
