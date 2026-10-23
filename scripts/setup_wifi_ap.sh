#!/usr/bin/env bash
# 把 M2 的 Wi-Fi 配成热点，供手机/电脑连上后访问控制页面。
#
#   sudo ./scripts/setup_wifi_ap.sh                  # 默认 manual 模式（推荐，不惹交换机）
#   sudo MODE=shared ./scripts/setup_wifi_ap.sh      # 旧模式：NM 自带 DHCP + NAT
#   sudo SSID=QuectelPi PASS=<至少8位> ./scripts/setup_wifi_ap.sh
#
# 两种模式的差别（为什么默认 manual）：
#   shared : NetworkManager 起 dnsmasq 做 DHCP + NAT。有的企业交换机看到"私接 DHCP"
#            会把端口 err-disable，表现是"开了热点之后有线也断了"。
#   manual : 只把 wlan0 设成 192.168.4.1/24，另起一个**只绑定 wlan0**的 dnsmasq 发地址；
#            never-default + except-interface=eth0，绝不碰有线/内网。
#
# 幂等：可重复执行。
set -u

SSID="${SSID:-QuectelPi}"
# 未指定密码时随机生成一个（每次重建热点都会变，脚本会把它打印出来）
if [ -z "${PASS:-}" ]; then
  PASS="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 12)"
  echo "[AP] 未指定 PASS，已生成随机密码: ${PASS}"
fi
AP_IP="${AP_IP:-192.168.4.1}"
CON_NAME="${CON_NAME:-car-ap}"
MODE="${MODE:-manual}"
DHCP_FROM="${DHCP_FROM:-192.168.4.10}"
DHCP_TO="${DHCP_TO:-192.168.4.100}"
DHCP_UNIT="car-ap-dhcp.service"
DHCP_CONF="/etc/dnsmasq.d/car-ap.conf"

say() { printf '%s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 sudo 运行：sudo $0"
command -v nmcli >/dev/null 2>&1 || die "系统里没有 nmcli（NetworkManager）。见 docs/无线热点.md 的 hostapd 方案。"

# ---- 1. 找无线网卡 ----
IFACE="${IFACE:-$(nmcli -t -f DEVICE,TYPE device 2>/dev/null | awk -F: '$2=="wifi"{print $1; exit}')}"
[ -n "${IFACE}" ] || IFACE="$(ip -br link 2>/dev/null | awk '/^wl/{print $1; exit}')"
[ -n "${IFACE}" ] || die "没找到无线网卡（ip -br link 看有没有 wlan*）"
say "无线网卡: ${IFACE}    模式: ${MODE}"

# ---- 2. 打开射频 ----
command -v rfkill >/dev/null 2>&1 && rfkill unblock wifi || true
nmcli radio wifi on >/dev/null 2>&1 || true

# ---- 3. 建/改 AP 连接 ----
if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "${CON_NAME}"; then
  say "已存在连接 ${CON_NAME}，按当前参数更新"
else
  nmcli connection add type wifi ifname "${IFACE}" con-name "${CON_NAME}" autoconnect yes ssid "${SSID}" \
    || die "创建连接失败"
fi

nmcli connection modify "${CON_NAME}" \
  connection.interface-name "${IFACE}" \
  connection.autoconnect yes \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "${PASS}" \
  ipv4.addresses "${AP_IP}/24" \
  ipv4.never-default yes \
  ipv6.method ignore || die "配置 AP 参数失败"

if [ "${MODE}" = "shared" ]; then
  nmcli connection modify "${CON_NAME}" ipv4.method shared
  say "shared 模式：NM 自带 DHCP+NAT（可能触发交换机对私接 DHCP 的防护）"
  systemctl disable --now "${DHCP_UNIT}" >/dev/null 2>&1 || true
else
  nmcli connection modify "${CON_NAME}" ipv4.method manual
  say "manual 模式：独立 dnsmasq，只绑定 ${IFACE}"
fi

nmcli connection up "${CON_NAME}" || die "热点启动失败（看 journalctl -u NetworkManager -n 50）"

# ---- 4. manual 模式：只为 wlan0 起一个 DHCP 服务 ----
if [ "${MODE}" != "shared" ]; then
  command -v dnsmasq >/dev/null 2>&1 || die "manual 模式需要 dnsmasq：apt install -y dnsmasq"

  mkdir -p /etc/dnsmasq.d
  cat > "${DHCP_CONF}" <<EOF
# 由 scripts/setup_wifi_ap.sh 生成：只服务热点，绝不碰其它网卡
interface=${IFACE}
bind-interfaces
except-interface=eth0
except-interface=lo
dhcp-range=${DHCP_FROM},${DHCP_TO},12h
dhcp-option=3,${AP_IP}
dhcp-option=6,${AP_IP}
log-dhcp
EOF

  cat > "/etc/systemd/system/${DHCP_UNIT}" <<EOF
[Unit]
Description=car AP DHCP (dnsmasq, bound to ${IFACE} only)
After=network.target

[Service]
Type=simple
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file=${DHCP_CONF}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  # 发行版自带的 dnsmasq 服务通常默认 disabled；避免两个实例抢 67 端口
  systemctl disable --now dnsmasq >/dev/null 2>&1 || true
  systemctl daemon-reload
  systemctl enable "${DHCP_UNIT}" >/dev/null 2>&1 || true
  systemctl restart "${DHCP_UNIT}" || die "DHCP 服务启动失败（journalctl -u ${DHCP_UNIT}）"
  sleep 1
  systemctl is-active "${DHCP_UNIT}" >/dev/null 2>&1 || die "DHCP 服务没起来（journalctl -u ${DHCP_UNIT}）"
fi

# ---- 5. NAT：让手机"感觉有网"，否则 Android 会优先用移动数据，请求就会几十秒才回 ----
WAN_IF="${WAN_IF:-$(ip route 2>/dev/null | awk '/^default/ {print $5; exit}')}"
NET_PREFIX="$(echo "${AP_IP}" | cut -d. -f1-3).0/24"
if [ -n "${WAN_IF}" ] && [ "${NAT:-1}" = "1" ]; then
  cat > /usr/local/sbin/car-ap-nat.sh <<EOF
#!/bin/sh
# 由 scripts/setup_wifi_ap.sh 生成：把热点网段 NAT 到有线出口（幂等）
sysctl -q -w net.ipv4.ip_forward=1
iptables -t nat -C POSTROUTING -s ${NET_PREFIX} -o ${WAN_IF} -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -s ${NET_PREFIX} -o ${WAN_IF} -j MASQUERADE
iptables -C FORWARD -i ${IFACE} -o ${WAN_IF} -j ACCEPT 2>/dev/null || \
  iptables -A FORWARD -i ${IFACE} -o ${WAN_IF} -j ACCEPT
iptables -C FORWARD -i ${WAN_IF} -o ${IFACE} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
  iptables -A FORWARD -i ${WAN_IF} -o ${IFACE} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
EOF
  chmod +x /usr/local/sbin/car-ap-nat.sh

  cat > /etc/systemd/system/car-ap-nat.service <<EOF
[Unit]
Description=car AP NAT (${IFACE} hotspot -> ${WAN_IF} uplink)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/car-ap-nat.sh

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable car-ap-nat.service >/dev/null 2>&1 || true
  systemctl restart car-ap-nat.service
  say "NAT 已启用：${NET_PREFIX} → ${WAN_IF}（手机会认为有网，不会抢移动数据）"
else
  say "[WARN] 没找到有线出口或 NAT=0，热点内的手机将没有外网（可能被 Android 判为无网而切移动数据）"
fi

say ""
say "======================================================"
say " Wi-Fi 热点已启动（${MODE} 模式）"
say "   SSID     : ${SSID}"
say "   密码     : ${PASS}"
say "   板子地址 : ${AP_IP}"
say "   页面     : http://${AP_IP}:8080/"
say "======================================================"
say ""
say "当前状态："
nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null | sed 's/^/  /'
ip -4 -o addr show dev "${IFACE}" 2>/dev/null | awk '{print "  " $2 " " $4}'
if [ "${MODE}" != "shared" ]; then
  say "  DHCP 服务: $(systemctl is-active ${DHCP_UNIT} 2>/dev/null)"
  say "  排除规则 : $(grep -c 'except-interface' ${DHCP_CONF} 2>/dev/null) 条（eth0 / lo）"
fi
say ""
say "提示：手机连上后提示「无法上网」是正常的；要让手机上网请让它走别的网络。"
