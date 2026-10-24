#!/usr/bin/env bash
# 实地跑之前的检查清单（只读，不动车）。
#
#   ./scripts/preflight.sh
#   ~/carpreflight              （如果建了软链）
#
# 输出：供电 / 串口 / 链路 / 网络 / 服务 / 当前状态 / 演示清单，并在有问题时给 WARN。
set -u
PORT="${PORT:-8080}"
BASE="http://127.0.0.1:${PORT}"
WARN=0

warn() { echo "  [WARN] $*"; WARN=$((WARN + 1)); }
ok()   { echo "  [ OK ] $*"; }
info() { echo "         $*"; }

json() { curl -s --max-time 4 "${BASE}/api/status" 2>/dev/null; }
field() { python3 -c "import json,sys
d=json.load(sys.stdin)['status']
print(eval(\"d$1\"))" 2>/dev/null; }

ST="$(json)"
if [ -z "${ST}" ]; then
  echo "[ERROR] 服务没有响应 ${BASE}/api/status —— 先确认服务是否在跑。"
  exit 2
fi

echo "=============================================="
echo " 跑前检查  $(date '+%m-%d %H:%M:%S')"
echo "=============================================="

# ---------------- 服务 ----------------
echo
echo "1) 服务"
ACT="$(systemctl is-active demo-car-motion-control.service 2>/dev/null)"
if [ "${ACT}" = "active" ]; then ok "demo-car-motion-control.service 已运行"; else warn "服务状态: ${ACT}"; fi
UP="$(echo "${ST}" | field '["uptime_s"]')"
BOOTS="$(journalctl --list-boots --no-pager 2>/dev/null | wc -l)"
info "运行 $(printf '%.0f' "${UP}" 2>/dev/null || echo '?') 秒 · 历史启动 ${BOOTS} 次"
if [ "${UP%.*}" -lt 60 ] 2>/dev/null; then warn "刚启动不到 1 分钟，等它稳定再跑（串口握手需要几秒）"; fi

# ---------------- 链路 ----------------
echo
echo "2) 串口链路"
HS="$(echo "${ST}" | field '["link"]["handshake"]')"
PORTNAME="$(echo "${ST}" | field '["link"]["port"]')"
if [ "${HS}" = "battery" ]; then ok "握手正常（收到 SYS 电池帧）"; else warn "握手状态: ${HS}（板子没通电/没接电池/串口没连？）"; fi
info "${PORTNAME}"
ERR="$(echo "${ST}" | field '["link"]["error"]')"
[ "${ERR}" != "None" ] && [ -n "${ERR}" ] && warn "链路错误: ${ERR}"

# ---------------- 电池 ----------------
echo
echo "3) 供电"
V="$(echo "${ST}" | field '["battery"]["voltage"]')"
MV="$(echo "${ST}" | field '["battery"]["millivolt"]')"
LOWV="$(echo "${ST}" | field '["battery"]["low_v"]')"
if [ -z "${V}" ] || [ "${V}" = "None" ]; then
  warn "读不到电池电压"
else
  info "电池 ${V} V（${MV} mV）· 告警阈值 ${LOWV} V"
  MORE=$(python3 -c "print(1 if ${V} < 7.20 else 0)" 2>/dev/null)
  if [ "${MORE}" = "1" ]; then warn "电池低于 7.2 V：速度会变慢、开环距离会偏短，建议先充电"; else ok "电压够用"; fi
fi

# ---------------- 网络 ----------------
echo
echo "4) 网络（用哪个地址控制）"
for ifc in eth0 wlan0; do
  addr="$(ip -4 -o addr show "${ifc}" 2>/dev/null | awk '{print $4}')"
  [ -n "${addr}" ] || continue
  info "${ifc}: ${addr}"
done
I="$(echo "${ST}" | field '["net"]["addresses"]')"
echo "${ST}" | python3 -c "
import json,sys
d=json.load(sys.stdin)['status']['net']['addresses']
for a in d: print('         可用: ' + a['url'] + '  (' + a['iface'] + ')')
" 2>/dev/null
if ip neigh show dev wlan0 2>/dev/null | grep -q REACHABLE; then info "手机已连热点"; fi

# ---------------- 当前状态 ----------------
echo
echo "5) 当前车辆状态（应该是停着的）"
KIND="$(echo "${ST}" | field '["applied"]["kind"]')"
TGT="$(echo "${ST}" | field '["target"]')"
ESTOP="$(echo "${ST}" | field '["safety"]["estop"]')"
RUN="$(echo "${ST}" | field '["demo"]["running"]')"
info "动作=${KIND} 目标=${TGT}"
if [ "${RUN}" = "True" ]; then warn "有演示正在跑，先 Stop"; else ok "没有演示在跑"; fi
if [ "${ESTOP}" = "True" ]; then warn "处于急停状态（下一条运动指令或开始演示会解除）"; fi
SENT="$(echo "${ST}" | field '["cmd"]["sent"]')"
ERRS="$(echo "${ST}" | field '["cmd"]["errors"]')"
HZ="$(echo "${ST}" | field '["cmd"]["hz"]')"
info "已下发 ${SENT} 帧 · 错误 ${ERRS} · ${HZ} Hz"
[ "${ERRS}" != "0" ] && warn "串口下发有错误（${ERRS}）"

# ---------------- 演示清单 ----------------
echo
echo "6) 演示清单"
curl -s --max-time 4 "${BASE}/api/demo/list" 2>/dev/null | python3 -c "
import json,sys
for x in json.load(sys.stdin)['sequences']:
    print('         %-11s %-26s %6.2fs  %2d 步' % (x['name'], x['title'], x['duration'], x['step_count']))
" 2>/dev/null

# ---------------- 结论 ----------------
echo
echo "=============================================="
if [ "${WARN}" -eq 0 ]; then
  echo " 结论: 可以开跑"
  echo " 提醒: 手边留电源开关；急停=手机长按 Stop / 桌面 Shift+Esc"
else
  echo " 结论: 有 ${WARN} 项需要注意（见上面 WARN）"
fi
echo "=============================================="
exit 0
