#!/usr/bin/env bash
# 车轮控制 Demo · 板端部署脚本（幂等：重复执行只补缺失部分）
# 用法： ./install.sh
set -euo pipefail

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
PY="${PYTHON:-python3}"

echo "======================================================"
echo " 车轮控制 Demo · 部署"
echo " 目录: ${PROJECT_DIR}"
echo "======================================================"

if ! command -v "${PY}" >/dev/null 2>&1; then
  echo "[ERROR] 未找到 ${PY}。Debian/Ubuntu 安装：sudo apt install python3" >&2
  exit 1
fi
echo "[1/4] Python: $(${PY} -V 2>&1)"

# ---- 系统依赖体检：缺了只提示，不中断（只有 python3 是硬性要求）----
_pkg_hint() {  # 命令 -> 需要安装的包
  case "$1" in
    python3|python)      echo "python3" ;;
    venv|ensurepip)      echo "python3-venv" ;;
    curl)                echo "curl" ;;
    ip|ss)               echo "iproute2" ;;
    nmcli)               echo "network-manager" ;;
    dnsmasq)             echo "dnsmasq" ;;
    rfkill)              echo "rfkill" ;;
    iw)                  echo "iw" ;;
    *)                   echo "$1" ;;
  esac
}
_need_apt=""
for c in curl ip ss nmcli dnsmasq rfkill iw; do
  command -v "$c" >/dev/null 2>&1 || _need_apt="${_need_apt} $(_pkg_hint "$c")"
done
if ! "${PY}" -c "import venv, ensurepip" >/dev/null 2>&1; then
  _need_apt="${_need_apt} python3-venv"
fi
if [ -n "${_need_apt}" ]; then
  echo "[依赖] 以下系统包未安装（缺少时对应功能不可用，不影响仿真与网页控制）："
  echo "        sudo apt install$(echo "${_need_apt}" | tr ' ' '\n' | sort -u | tr '\n' ' ')"
else
  echo "[依赖] 系统依赖齐全"
fi

# 依赖隔离环境（保留 .venv，避免每次推送都重装依赖）
if [ ! -d .venv ]; then
  echo "[2/4] 创建隔离环境 .venv ..."
  if ! "${PY}" -m venv .venv; then
    echo "[WARN] 创建 .venv 失败（Debian 需要：sudo apt install python3-venv）"
    echo "       改用系统 ${PY} 继续；装好后重跑本脚本即可获得隔离环境"
  fi
else
  echo "[2/4] 复用已有隔离环境 .venv"
fi

# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || true

# 自愈：工程目录改过名时，venv 内（activate / pip shebang）还是旧绝对路径，
# 表现是"激活后找不到 python"。这里从 pip 的 shebang 反推 venv 记录的路径并自动改写。
if ! command -v python >/dev/null 2>&1 && [ -x .venv/bin/python3 ]; then
  _recorded="$(head -1 .venv/bin/pip 2>/dev/null | sed -n 's|^#!\(.*\)/bin/python.*|\1|p')"
  _want="${PROJECT_DIR}/.venv"
  if [ -n "${_recorded}" ] && [ "${_recorded}" != "${_want}" ]; then
    echo "[fix] venv 记录的是旧目录 ${_recorded}，自动改写为 ${_want}"
    for f in $(grep -rl -- "${_recorded}" .venv/bin 2>/dev/null || true); do
      sed -i "s|${_recorded}|${_want}|g" "$f" || true
    done
    [ -f .venv/pyvenv.cfg ] && sed -i "s|${_recorded}|${_want}|g" .venv/pyvenv.cfg || true
    # shellcheck disable=SC1091
    source .venv/bin/activate 2>/dev/null || true
  fi
fi

PYEXE="$(command -v python 2>/dev/null || true)"
if [ -z "${PYEXE}" ]; then
  echo "[WARN] .venv 不可用，退回系统 ${PY}（没有 pyserial 时程序用内置 termios 实现）"
  PYEXE="${PY}"
fi

echo "[3/4] 安装依赖（失败不影响运行，程序对 pyserial 有 termios 回退）"
"${PYEXE}" -m pip install --upgrade pip >/dev/null 2>&1 || true
if ! "${PYEXE}" -m pip install -r requirements.txt; then
  echo "[WARN] 依赖安装失败（可能无外网）。Web 服务与仿真演示不依赖第三方库，仍可运行。"
fi

# 执行位：从压缩包/Windows 拷过来的工程可能没有 +x，补上（否则 ./run.sh 会 Permission denied）
chmod +x install.sh run.sh 2>/dev/null || true
chmod +x scripts/*.sh 2>/dev/null || true

# 快捷命令：~/carurl -> scripts/show_access_url.sh（忘了板子 IP 时一条命令查访问地址）
if [ -f scripts/show_access_url.sh ] && [ -n "${HOME:-}" ]; then
  chmod +x scripts/show_access_url.sh 2>/dev/null || true
  ln -sf "${PROJECT_DIR}/scripts/show_access_url.sh" "$HOME/carurl" 2>/dev/null || true
  echo "  快捷命令: ~/carurl -> scripts/show_access_url.sh"
fi

echo "[4/4] 检查运行环境"
"${PYEXE}" - <<'PY' || true
import sys
sys.path.insert(0, "src")
try:
    import serial
    print("  pyserial: 已安装", getattr(serial, "__version__", ""))
except Exception:
    print("  pyserial: 未安装（Linux 下将使用内置 termios 串口实现）")
try:
    import termios  # noqa: F401
    print("  termios : 可用")
except Exception:
    print("  termios : 不可用（Windows 需安装 pyserial 才能连真实串口）")
from wheelctl import kinematics, demo, protocol
print("  运动学与演示序列: 加载正常，序列数 =", len(demo.DemoRunner().list_sequences()))
pct = kinematics.wheel_speeds(0, 55, 0)
duties = [protocol.apply_deadzone(v, 15.0) for v in pct]
print("  前进 55% 占空   :", duties)
print("  电机帧示例     :", protocol.cmd_motor_duty_all(duties).hex())
print("  CRC8 表长度    :", len(protocol.CRC8_TABLE))
PY

echo "======================================================"
echo "Deployment complete"
echo "启动: ./run.sh   （没有可执行位时用 bash run.sh；等价: .venv/bin/python src/main.py）"
echo "无硬件演示: ./run.sh --sim"
echo "======================================================"
