#!/usr/bin/env bash
# 打印"该用什么地址打开控制页面"——手机/电脑连上小车无线网后照着打开即可。
#
#   ./scripts/show_access_url.sh
#
# 说明：服务监听 0.0.0.0:8080，所以板子所有网卡的地址都能访问；
#      小车自带热点时，常见地址是 192.168.4.1 / 10.0.0.1 / 192.168.0.1（以本脚本输出为准）。
set -u

PORT="${PORT:-8080}"

echo "======================================================"
echo " 麦轮小车控制页面 · 访问地址"
echo "======================================================"

# 1) 服务状态
if systemctl is-active --quiet demo-car-motion-control.service 2>/dev/null; then
  echo "服务状态: active（已开机自启）"
else
  echo "服务状态: 未运行 —— 先执行：sudo systemctl start demo-car-motion-control.service"
fi

# 2) 监听情况
listen="$(ss -tln 2>/dev/null | awk -v p=":${PORT}" '$4 ~ p {print $4}' | head -3 | paste -sd' ' -)"
[ -n "${listen}" ] && echo "监听端口: ${listen}" || echo "监听端口: 没看到 ${PORT} 在监听（ss -tln | grep ${PORT}）"

# 2.5) 服务启动时保存的地址文件
# 注意：脚本可能通过符号链接被调用（~/carurl -> scripts/show_access_url.sh），
# 必须先把真实路径解出来，否则 ../.access_urls 会算到 ~/ 上面去。
SELF="$(readlink -f "$0" 2>/dev/null || echo "$0")"
PROJECT_DIR="$(cd "$(dirname "$SELF")/.." 2>/dev/null && pwd)"
# 优先用 ~/.wheel_urls（工程外，覆盖式推送不会删）；兼容早期工程内 .access_urls
if [ -f "$HOME/.wheel_urls" ]; then
  URL_FILE="$HOME/.wheel_urls"
else
  URL_FILE="${PROJECT_DIR}/.access_urls"
fi
echo
if [ -f "${URL_FILE}" ]; then
  echo "服务启动时记录的地址（$(stat -c '%y' "${URL_FILE}" 2>/dev/null | cut -d. -f1)）："
  sed 's/^/  /' "${URL_FILE}"
else
  echo "（还没有地址记录 ~/.wheel_urls：服务可能没启动过，先 systemctl start demo-car-motion-control.service）"
fi

# 3) 各网卡地址
echo
echo "板子上的 IPv4 地址："
ips="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $2 "  " $4}')"
if [ -n "${ips}" ]; then
  echo "${ips}" | sed 's/^/  /'
else
  echo "  （没拿到 IPv4，检查无线/网线是否连上）"
fi

echo
echo "试着在手机/电脑浏览器打开下面任意一个："
for ip in $(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1); do
  case "${ip}" in
    127.*) continue ;;
  esac
  echo "  http://${ip}:${PORT}/            （深色主题：http://${ip}:${PORT}/dark）"
done

hn="$(hostname 2>/dev/null || true)"
if [ -n "${hn}" ] && command -v avahi-resolve >/dev/null 2>&1; then
  echo "  http://${hn}.local:${PORT}/   （装了 avahi/支持 mDNS 的话可以用主机名）"
fi

# 4) 无线热点/连接信息（有 nmcli 就顺带打印，方便告诉用户连哪个 SSID）
if command -v nmcli >/dev/null 2>&1; then
  echo
  echo "无线状态（nmcli）："
  nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null | sed 's/^/  /' || true
  if nmcli -t -f NAME,TYPE connection show 2>/dev/null | grep -qi 'wifi'; then
    echo "  提示：若小车是用 hostapd 起的纯热点，nmcli 可能看不到，用 iw dev 查 SSID。"
  fi
fi

echo
echo "连不上时按顺序查：① 手机是否连在小车那个 Wi-Fi 上（同一网段）"
echo "                  ② 上面的地址是否 ping 得通 ③ 板子是否开着无线（iw dev / nmcli）"
echo "                  ④ 服务是否 active（journalctl -u demo-car-motion-control -n 30）"
echo "======================================================"
